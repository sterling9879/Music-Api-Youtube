"""
Celery tasks for video generation pipeline.
"""

import asyncio
import logging
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

from celery import states
from celery.exceptions import SoftTimeLimitExceeded

from app.workers.celery_app import celery_app
from app.services.kie_ai import KieAIService, KieAIError
from app.services.audio import AudioProcessor, AudioProcessingError, format_duration
from app.services.video import VideoProcessor, VideoProcessingError
from app.config import (
    UPLOAD_DIR,
    OUTPUT_DIR,
    TARGET_DURATION_MINUTES,
    MAX_DURATION_MINUTES,
)

logger = logging.getLogger(__name__)


class JobLogger:
    """Logger that writes to both console and job-specific log file."""

    def __init__(self, job_dir: Path, job_id: str):
        self.log_file = job_dir / "logs.txt"
        self.job_id = job_id
        self.logs = []

    def _write(self, level: str, message: str):
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}"
        self.logs.append(log_entry)

        # Write to file
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")

        # Also log to console
        getattr(logger, level.lower(), logger.info)(f"[{self.job_id}] {message}")

    def info(self, message: str):
        self._write("INFO", message)

    def warning(self, message: str):
        self._write("WARNING", message)

    def error(self, message: str):
        self._write("ERROR", message)

    def debug(self, message: str):
        self._write("DEBUG", message)


def run_async(coro):
    """Helper to run async code in sync context."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=3)
def generate_music_video(
    self,
    job_id: str,
    prompt: str,
    api_key: str,
    video_filename: str,
    custom_mode: bool = False,
    instrumental: bool = True,
    model: str = "V4",
    style: Optional[str] = None,
    title: Optional[str] = None,
    concurrent_tracks: int = 1,
    channel_id: Optional[str] = None,
    channel_name: Optional[str] = None
):
    """
    Main task for generating music video.

    This task:
    1. Generates music tracks using Kie AI until target duration is reached
    2. Concatenates all audio tracks
    3. Loops the video to match audio duration
    4. Combines video and audio into final output
    """
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    tracks_dir = job_dir / "tracks"
    tracks_dir.mkdir(exist_ok=True)

    video_path = UPLOAD_DIR / video_filename
    final_video_path = job_dir / "final_video.mp4"
    final_audio_path = job_dir / "final_audio.mp3"
    tracklist_path = job_dir / "tracklist.json"

    # Initialize job logger
    job_logger = JobLogger(job_dir, job_id)
    job_logger.info(f"Starting job with prompt: {prompt[:100]}...")
    job_logger.info(f"Settings: custom_mode={custom_mode}, instrumental={instrumental}, model={model}")
    if channel_id:
        job_logger.info(f"Channel: {channel_name} ({channel_id})")

    # Initialize progress
    progress = {
        "current_track": 0,
        "total_duration_seconds": 0.0,
        "total_duration_formatted": "00:00:00",
        "stage": "initializing",
        "message": "Starting generation...",
        "tracks": []
    }

    # Save initial metadata
    metadata = {
        "job_id": job_id,
        "prompt": prompt,
        "custom_mode": custom_mode,
        "instrumental": instrumental,
        "model": model,
        "style": style,
        "title": title,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "status": "processing",
        "created_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "total_tracks": 0,
        "total_duration": "00:00:00",
        "total_duration_seconds": 0,
        "video_file": None,
        "audio_file": None,
        "tracklist_file": None,
        "tracks_info": []
    }

    with open(job_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    def update_progress(stage: str, message: str, **kwargs):
        """Update task progress."""
        progress["stage"] = stage
        progress["message"] = message
        progress.update(kwargs)
        self.update_state(state="PROGRESS", meta=progress)
        job_logger.info(f"[{stage}] {message}")

    def save_metadata():
        """Save current metadata to file."""
        with open(job_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

    try:
        # Validate video exists
        if not video_path.exists():
            raise VideoProcessingError(f"Video file not found: {video_path}")

        job_logger.info(f"Video file validated: {video_filename}")

        # Initialize services
        kie_service = KieAIService(api_key)
        audio_processor = AudioProcessor(job_dir / "audio_work")
        video_processor = VideoProcessor(job_dir / "video_work", log_callback=job_logger.info)

        target_duration_seconds = TARGET_DURATION_MINUTES * 60
        max_duration_seconds = MAX_DURATION_MINUTES * 60

        job_logger.info(f"Target duration: {TARGET_DURATION_MINUTES} minutes")
        job_logger.info(f"Max duration: {MAX_DURATION_MINUTES} minutes")
        job_logger.info(f"Concurrent tracks per batch: {concurrent_tracks}")

        generated_tracks = []
        tracks_info = []
        total_duration = 0.0
        track_number = 0

        update_progress(
            "generating_audio",
            "Starting music generation...",
            current_track=0,
            total_duration_seconds=0
        )

        async def generate_single_track(kie_svc, track_num):
            """Generate a single track asynchronously."""
            try:
                task_id = await kie_svc.generate_music(
                    prompt=prompt,
                    custom_mode=custom_mode,
                    instrumental=instrumental,
                    model=model,
                    style=style,
                    title=f"{title or 'Track'} {track_num}" if title else None
                )
                job_logger.info(f"Track {track_num} - Kie AI task ID: {task_id}")

                tracks_data = await kie_svc.wait_for_completion(task_id)

                if tracks_data:
                    return {"track_num": track_num, "data": tracks_data[0], "success": True}
                return {"track_num": track_num, "success": False, "error": "No tracks returned"}
            except Exception as e:
                return {"track_num": track_num, "success": False, "error": str(e)}

        async def generate_batch(kie_svc, start_track_num, batch_size):
            """Generate a batch of tracks in parallel."""
            tasks = []
            for i in range(batch_size):
                tasks.append(generate_single_track(kie_svc, start_track_num + i))
            return await asyncio.gather(*tasks)

        # Generate music until we reach target duration
        while total_duration < target_duration_seconds:
            # Determine batch size (don't exceed what we need)
            remaining_estimate = (target_duration_seconds - total_duration) / 180  # ~3 min per track
            batch_size = min(concurrent_tracks, max(1, int(remaining_estimate) + 1))

            start_track = track_number + 1

            update_progress(
                "generating_audio",
                f"Generating tracks {start_track} to {start_track + batch_size - 1} ({batch_size} in parallel)...",
                current_track=track_number
            )

            job_logger.info(f"Starting batch generation: tracks {start_track}-{start_track + batch_size - 1}")

            # Generate batch in parallel
            batch_results = run_async(generate_batch(kie_service, start_track, batch_size))

            # Process results
            successful_tracks = 0
            for result in sorted(batch_results, key=lambda x: x["track_num"]):
                track_num = result["track_num"]

                if not result["success"]:
                    job_logger.error(f"Track {track_num} failed: {result.get('error', 'Unknown error')}")
                    if "Insufficient credits" in str(result.get("error", "")) or "Invalid API key" in str(result.get("error", "")):
                        raise KieAIError(result["error"])
                    continue

                track_data = result["data"]
                audio_url = track_data.get("audioUrl") or track_data.get("audio_url")
                track_duration = track_data.get("duration", 0)
                track_title = track_data.get("title", f"Track {track_num}")
                track_id = track_data.get("id", "")

                if not audio_url:
                    job_logger.warning(f"Track {track_num} has no audio URL, skipping")
                    continue

                # Check if adding this track would exceed max duration
                if total_duration + track_duration > max_duration_seconds:
                    job_logger.info(f"Reached max duration, stopping generation")
                    break

                # Download the track
                track_file = tracks_dir / f"track_{track_num:03d}.mp3"
                job_logger.info(f"Downloading track {track_num}: {audio_url[:50]}...")
                run_async(kie_service.download_audio(audio_url, track_file))

                generated_tracks.append((track_file, track_title, track_id))
                successful_tracks += 1
                track_number = track_num

                # Store track info for metadata
                track_info_item = {
                    "track_number": track_num,
                    "filename": f"track_{track_num:03d}.mp3",
                    "title": track_title,
                    "kie_track_id": track_id,
                    "duration_seconds": track_duration,
                    "duration_formatted": format_duration(track_duration),
                    "downloaded_at": datetime.utcnow().isoformat()
                }
                tracks_info.append(track_info_item)
                metadata["tracks_info"] = tracks_info
                save_metadata()

                total_duration += track_duration

                job_logger.info(
                    f"Track {track_num} saved: {track_title} "
                    f"(duration: {format_duration(track_duration)}, "
                    f"total: {format_duration(total_duration)})"
                )

                # Check if we've reached target
                if total_duration >= target_duration_seconds:
                    job_logger.info("Target duration reached!")
                    break

            update_progress(
                "generating_audio",
                f"Batch complete: {successful_tracks} tracks downloaded. Total: {format_duration(total_duration)}",
                current_track=track_number,
                total_duration_seconds=total_duration,
                total_duration_formatted=format_duration(total_duration)
            )

            # If no tracks were successful in this batch, we might have an issue
            if successful_tracks == 0:
                job_logger.warning("No tracks succeeded in this batch, trying again...")
                # Small delay before retry
                run_async(asyncio.sleep(5))

        if not generated_tracks:
            raise KieAIError("No tracks were successfully generated")

        job_logger.info(f"Audio generation complete: {len(generated_tracks)} tracks, {format_duration(total_duration)}")

        # Concatenate audio
        update_progress(
            "concatenating_audio",
            f"Concatenating {len(generated_tracks)} tracks...",
            current_track=len(generated_tracks),
            total_duration_seconds=total_duration,
            total_duration_formatted=format_duration(total_duration)
        )

        job_logger.info("Starting audio concatenation...")
        audio_result = audio_processor.concatenate_audio_files(
            generated_tracks,
            final_audio_path
        )
        job_logger.info(f"Audio concatenated: {audio_result.total_duration_formatted}")

        # Generate tracklist
        audio_processor.generate_tracklist_json(audio_result, tracklist_path)
        job_logger.info("Tracklist generated")

        # Process video
        update_progress(
            "processing_video",
            "Looping video to match audio duration...",
            current_track=len(generated_tracks),
            total_duration_seconds=audio_result.total_duration_seconds,
            total_duration_formatted=audio_result.total_duration_formatted
        )

        job_logger.info(f"Starting video processing (target: {audio_result.total_duration_formatted})...")
        video_processor.process_video_with_audio(
            video_path,
            final_audio_path,
            audio_result.total_duration_seconds,
            final_video_path
        )
        job_logger.info("Video processing complete")

        # Update final metadata
        metadata.update({
            "status": "completed",
            "completed_at": datetime.utcnow().isoformat(),
            "total_tracks": len(generated_tracks),
            "total_duration": audio_result.total_duration_formatted,
            "total_duration_seconds": audio_result.total_duration_seconds,
            "video_file": "final_video.mp4",
            "audio_file": "final_audio.mp3",
            "tracklist_file": "tracklist.json"
        })
        save_metadata()

        # Cleanup work directories (but keep tracks!)
        shutil.rmtree(job_dir / "audio_work", ignore_errors=True)
        shutil.rmtree(job_dir / "video_work", ignore_errors=True)

        job_logger.info("=" * 50)
        job_logger.info("JOB COMPLETED SUCCESSFULLY")
        job_logger.info(f"Total tracks: {len(generated_tracks)}")
        job_logger.info(f"Total duration: {audio_result.total_duration_formatted}")
        job_logger.info(f"Output: {final_video_path}")
        job_logger.info("=" * 50)

        # Update final progress
        final_progress = {
            "current_track": len(generated_tracks),
            "total_duration_seconds": audio_result.total_duration_seconds,
            "total_duration_formatted": audio_result.total_duration_formatted,
            "stage": "completed",
            "message": "Video generation completed!",
            "video_file": "final_video.mp4",
            "audio_file": "final_audio.mp3",
            "tracklist_file": "tracklist.json"
        }

        return final_progress

    except SoftTimeLimitExceeded:
        job_logger.error("Task exceeded time limit")
        metadata["status"] = "failed"
        metadata["error"] = "Task exceeded time limit"
        save_metadata()
        update_progress("failed", "Task exceeded time limit")
        raise

    except (KieAIError, AudioProcessingError, VideoProcessingError) as e:
        job_logger.error(f"Task failed: {e}")
        metadata["status"] = "failed"
        metadata["error"] = str(e)
        save_metadata()
        progress["stage"] = "failed"
        progress["message"] = str(e)
        self.update_state(state=states.FAILURE, meta=progress)
        raise

    except Exception as e:
        job_logger.error(f"Unexpected error: {str(e)}")
        metadata["status"] = "failed"
        metadata["error"] = f"Unexpected error: {str(e)}"
        save_metadata()
        progress["stage"] = "failed"
        progress["message"] = f"Unexpected error: {str(e)}"
        self.update_state(state=states.FAILURE, meta=progress)
        raise


@celery_app.task
def cleanup_old_files():
    """
    Cleanup task to remove files older than retention period.
    Runs periodically via Celery beat.
    """
    from app.config import FILE_RETENTION_HOURS
    import time

    retention_seconds = FILE_RETENTION_HOURS * 3600
    current_time = time.time()

    # Clean upload directory
    for file_path in UPLOAD_DIR.iterdir():
        if file_path.is_file() and file_path.name != ".gitkeep":
            file_age = current_time - file_path.stat().st_mtime
            if file_age > retention_seconds:
                try:
                    file_path.unlink()
                    logger.info(f"Deleted old upload: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to delete {file_path}: {e}")

    # Clean output directory
    for job_dir in OUTPUT_DIR.iterdir():
        if job_dir.is_dir():
            metadata_file = job_dir / "metadata.json"
            if metadata_file.exists():
                file_age = current_time - metadata_file.stat().st_mtime
                if file_age > retention_seconds:
                    try:
                        shutil.rmtree(job_dir)
                        logger.info(f"Deleted old job directory: {job_dir}")
                    except Exception as e:
                        logger.error(f"Failed to delete {job_dir}: {e}")


@celery_app.task(bind=True, max_retries=3)
def create_mix_video(
    self,
    job_id: str,
    source_job_ids: list,
    video_filename: str,
    target_duration_minutes: int = 120,
    channel_id: Optional[str] = None,
    channel_name: Optional[str] = None
):
    """
    Create a mix video from existing tracks.

    This task:
    1. Collects all tracks from the specified source jobs
    2. Randomly selects tracks until target duration is reached
    3. Concatenates audio
    4. Loops video to match audio
    5. Creates final mixed video
    """
    import random

    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    tracks_dir = job_dir / "tracks"
    tracks_dir.mkdir(exist_ok=True)

    video_path = UPLOAD_DIR / video_filename
    final_video_path = job_dir / "final_video.mp4"
    final_audio_path = job_dir / "final_audio.mp3"
    tracklist_path = job_dir / "tracklist.json"

    # Initialize job logger
    job_logger = JobLogger(job_dir, job_id)
    job_logger.info(f"Starting mix job from {len(source_job_ids)} source jobs")
    job_logger.info(f"Target duration: {target_duration_minutes} minutes")
    if channel_id:
        job_logger.info(f"Channel: {channel_name} ({channel_id})")

    # Initialize progress
    progress = {
        "current_track": 0,
        "total_duration_seconds": 0.0,
        "total_duration_formatted": "00:00:00",
        "stage": "initializing",
        "message": "Starting mix creation...",
        "tracks": []
    }

    # Save initial metadata
    metadata = {
        "job_id": job_id,
        "job_type": "mix",
        "source_jobs": source_job_ids,
        "target_duration_minutes": target_duration_minutes,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "status": "processing",
        "created_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "total_tracks": 0,
        "total_duration": "00:00:00",
        "total_duration_seconds": 0,
        "video_file": None,
        "audio_file": None,
        "tracklist_file": None,
        "tracks_info": []
    }

    with open(job_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    def update_progress(stage: str, message: str, **kwargs):
        """Update task progress."""
        progress["stage"] = stage
        progress["message"] = message
        progress.update(kwargs)
        self.update_state(state="PROGRESS", meta=progress)
        job_logger.info(f"[{stage}] {message}")

    def save_metadata():
        """Save current metadata to file."""
        with open(job_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

    try:
        # Validate video exists
        if not video_path.exists():
            raise VideoProcessingError(f"Video file not found: {video_path}")

        job_logger.info(f"Video file validated: {video_filename}")

        # Collect all available tracks from source jobs
        update_progress("collecting", "Collecting tracks from source jobs...")

        available_tracks = []
        for source_job_id in source_job_ids:
            source_job_dir = OUTPUT_DIR / source_job_id
            source_tracks_dir = source_job_dir / "tracks"
            source_metadata_path = source_job_dir / "metadata.json"

            if not source_tracks_dir.exists():
                job_logger.warning(f"Source job {source_job_id} has no tracks directory")
                continue

            # Load track info from metadata if available
            tracks_info_map = {}
            if source_metadata_path.exists():
                try:
                    with open(source_metadata_path) as f:
                        source_metadata = json.load(f)
                        for track in source_metadata.get("tracks_info", []):
                            tracks_info_map[track.get("filename")] = track
                except Exception as e:
                    job_logger.warning(f"Could not read metadata for {source_job_id}: {e}")

            # Collect track files
            for track_file in source_tracks_dir.glob("*.mp3"):
                track_info = tracks_info_map.get(track_file.name, {})
                available_tracks.append({
                    "source_job": source_job_id,
                    "file_path": track_file,
                    "filename": track_file.name,
                    "title": track_info.get("title", track_file.stem),
                    "duration_seconds": track_info.get("duration_seconds", 180),  # Default 3 min
                    "duration_formatted": track_info.get("duration_formatted", "00:03:00"),
                    "kie_track_id": track_info.get("kie_track_id", "")
                })

        if not available_tracks:
            raise AudioProcessingError("No tracks found in source jobs")

        job_logger.info(f"Found {len(available_tracks)} tracks from source jobs")

        # Shuffle tracks for randomization
        random.shuffle(available_tracks)

        # Select tracks until we reach target duration
        update_progress("selecting", "Selecting tracks for mix...")

        target_duration_seconds = target_duration_minutes * 60
        max_duration_seconds = (target_duration_minutes + 10) * 60  # Allow 10 min overflow

        selected_tracks = []
        total_duration = 0.0
        track_number = 0

        # Keep selecting tracks until we reach target
        track_index = 0
        while total_duration < target_duration_seconds:
            if track_index >= len(available_tracks):
                # If we've gone through all tracks, reshuffle and start over
                if total_duration >= target_duration_seconds * 0.9:  # 90% is enough
                    break
                random.shuffle(available_tracks)
                track_index = 0
                job_logger.info("Reshuffling tracks for more content...")

            track = available_tracks[track_index]
            track_duration = track["duration_seconds"]

            # Check if adding this track exceeds max
            if total_duration + track_duration > max_duration_seconds:
                track_index += 1
                continue

            track_number += 1

            # Copy track to mix job's tracks folder
            dest_track_file = tracks_dir / f"track_{track_number:03d}.mp3"
            shutil.copy2(track["file_path"], dest_track_file)

            selected_tracks.append((dest_track_file, track["title"], track["kie_track_id"]))

            # Store track info
            track_info_item = {
                "track_number": track_number,
                "filename": f"track_{track_number:03d}.mp3",
                "title": track["title"],
                "original_source": track["source_job"],
                "original_filename": track["filename"],
                "kie_track_id": track["kie_track_id"],
                "duration_seconds": track_duration,
                "duration_formatted": track["duration_formatted"]
            }
            metadata["tracks_info"].append(track_info_item)
            save_metadata()

            total_duration += track_duration
            job_logger.info(
                f"Track {track_number}: {track['title']} "
                f"(duration: {track['duration_formatted']}, "
                f"total: {format_duration(total_duration)})"
            )

            track_index += 1

            update_progress(
                "selecting",
                f"Selected {track_number} tracks. Total: {format_duration(total_duration)}",
                current_track=track_number,
                total_duration_seconds=total_duration,
                total_duration_formatted=format_duration(total_duration)
            )

        if not selected_tracks:
            raise AudioProcessingError("No tracks could be selected for mix")

        job_logger.info(f"Selection complete: {len(selected_tracks)} tracks, {format_duration(total_duration)}")

        # Initialize processors
        audio_processor = AudioProcessor(job_dir / "audio_work")
        video_processor = VideoProcessor(job_dir / "video_work", log_callback=job_logger.info)

        # Concatenate audio
        update_progress(
            "concatenating_audio",
            f"Concatenating {len(selected_tracks)} tracks...",
            current_track=len(selected_tracks),
            total_duration_seconds=total_duration,
            total_duration_formatted=format_duration(total_duration)
        )

        job_logger.info("Starting audio concatenation...")
        audio_result = audio_processor.concatenate_audio_files(
            selected_tracks,
            final_audio_path
        )
        job_logger.info(f"Audio concatenated: {audio_result.total_duration_formatted}")

        # Generate tracklist
        audio_processor.generate_tracklist_json(audio_result, tracklist_path)
        job_logger.info("Tracklist generated")

        # Process video
        update_progress(
            "processing_video",
            "Looping video to match audio duration...",
            current_track=len(selected_tracks),
            total_duration_seconds=audio_result.total_duration_seconds,
            total_duration_formatted=audio_result.total_duration_formatted
        )

        job_logger.info(f"Starting video processing (target: {audio_result.total_duration_formatted})...")
        video_processor.process_video_with_audio(
            video_path,
            final_audio_path,
            audio_result.total_duration_seconds,
            final_video_path
        )
        job_logger.info("Video processing complete")

        # Update final metadata
        metadata.update({
            "status": "completed",
            "completed_at": datetime.utcnow().isoformat(),
            "total_tracks": len(selected_tracks),
            "total_duration": audio_result.total_duration_formatted,
            "total_duration_seconds": audio_result.total_duration_seconds,
            "video_file": "final_video.mp4",
            "audio_file": "final_audio.mp3",
            "tracklist_file": "tracklist.json"
        })
        save_metadata()

        # Cleanup work directories
        shutil.rmtree(job_dir / "audio_work", ignore_errors=True)
        shutil.rmtree(job_dir / "video_work", ignore_errors=True)

        job_logger.info("=" * 50)
        job_logger.info("MIX JOB COMPLETED SUCCESSFULLY")
        job_logger.info(f"Total tracks: {len(selected_tracks)}")
        job_logger.info(f"Total duration: {audio_result.total_duration_formatted}")
        job_logger.info(f"Output: {final_video_path}")
        job_logger.info("=" * 50)

        # Final progress
        final_progress = {
            "current_track": len(selected_tracks),
            "total_duration_seconds": audio_result.total_duration_seconds,
            "total_duration_formatted": audio_result.total_duration_formatted,
            "stage": "completed",
            "message": "Mix video completed!",
            "video_file": "final_video.mp4",
            "audio_file": "final_audio.mp3",
            "tracklist_file": "tracklist.json"
        }

        return final_progress

    except SoftTimeLimitExceeded:
        job_logger.error("Task exceeded time limit")
        metadata["status"] = "failed"
        metadata["error"] = "Task exceeded time limit"
        save_metadata()
        update_progress("failed", "Task exceeded time limit")
        raise

    except (AudioProcessingError, VideoProcessingError) as e:
        job_logger.error(f"Task failed: {e}")
        metadata["status"] = "failed"
        metadata["error"] = str(e)
        save_metadata()
        progress["stage"] = "failed"
        progress["message"] = str(e)
        self.update_state(state=states.FAILURE, meta=progress)
        raise

    except Exception as e:
        job_logger.error(f"Unexpected error: {str(e)}")
        metadata["status"] = "failed"
        metadata["error"] = f"Unexpected error: {str(e)}"
        save_metadata()
        progress["stage"] = "failed"
        progress["message"] = f"Unexpected error: {str(e)}"
        self.update_state(state=states.FAILURE, meta=progress)
        raise


# Celery beat schedule for cleanup
celery_app.conf.beat_schedule = {
    "cleanup-old-files": {
        "task": "app.workers.tasks.cleanup_old_files",
        "schedule": 3600.0,  # Run every hour
    },
}

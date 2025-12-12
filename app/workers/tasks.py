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
    title: Optional[str] = None
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

    # Initialize progress
    progress = {
        "current_track": 0,
        "total_duration_seconds": 0.0,
        "total_duration_formatted": "00:00:00",
        "stage": "initializing",
        "message": "Starting generation...",
        "tracks": []
    }

    def update_progress(stage: str, message: str, **kwargs):
        """Update task progress."""
        progress["stage"] = stage
        progress["message"] = message
        progress.update(kwargs)
        self.update_state(state="PROGRESS", meta=progress)

    try:
        # Validate video exists
        if not video_path.exists():
            raise VideoProcessingError(f"Video file not found: {video_path}")

        # Initialize services
        kie_service = KieAIService(api_key)
        audio_processor = AudioProcessor(job_dir / "audio_work")
        video_processor = VideoProcessor(job_dir / "video_work")

        target_duration_seconds = TARGET_DURATION_MINUTES * 60
        max_duration_seconds = MAX_DURATION_MINUTES * 60

        generated_tracks = []
        total_duration = 0.0
        track_number = 0

        update_progress(
            "generating_audio",
            "Starting music generation...",
            current_track=0,
            total_duration_seconds=0
        )

        # Generate music until we reach target duration
        while total_duration < target_duration_seconds:
            track_number += 1

            update_progress(
                "generating_audio",
                f"Generating track {track_number}...",
                current_track=track_number
            )

            try:
                # Generate music
                task_id = run_async(
                    kie_service.generate_music(
                        prompt=prompt,
                        custom_mode=custom_mode,
                        instrumental=instrumental,
                        model=model,
                        style=style,
                        title=f"{title or 'Track'} {track_number}" if title else None
                    )
                )

                update_progress(
                    "generating_audio",
                    f"Waiting for track {track_number} to complete...",
                    current_track=track_number
                )

                # Wait for completion
                tracks_data = run_async(kie_service.wait_for_completion(task_id))

                if not tracks_data:
                    raise KieAIError("No tracks returned from API")

                # Process each track from the response (API returns multiple variations)
                for track_data in tracks_data:
                    audio_url = track_data.get("audioUrl") or track_data.get("audio_url")
                    track_duration = track_data.get("duration", 0)
                    track_title = track_data.get("title", f"Track {track_number}")
                    track_id = track_data.get("id", "")

                    if not audio_url:
                        continue

                    # Check if adding this track would exceed max duration
                    if total_duration + track_duration > max_duration_seconds:
                        logger.info(f"Reached max duration, stopping generation")
                        break

                    # Download the track
                    track_file = tracks_dir / f"track_{track_number:03d}.mp3"
                    run_async(kie_service.download_audio(audio_url, track_file))

                    generated_tracks.append((track_file, track_title, track_id))
                    total_duration += track_duration

                    update_progress(
                        "generating_audio",
                        f"Downloaded track {track_number} ({format_duration(track_duration)})",
                        current_track=track_number,
                        total_duration_seconds=total_duration,
                        total_duration_formatted=format_duration(total_duration)
                    )

                    logger.info(
                        f"Track {track_number} downloaded. "
                        f"Total duration: {format_duration(total_duration)}"
                    )

                    # Check if we've reached target
                    if total_duration >= target_duration_seconds:
                        break

                    # Only use first track from each generation
                    break

            except KieAIError as e:
                logger.error(f"Kie AI error on track {track_number}: {e}")
                if "Insufficient credits" in str(e) or "Invalid API key" in str(e):
                    raise
                # Continue with next track for other errors
                continue

        if not generated_tracks:
            raise KieAIError("No tracks were successfully generated")

        logger.info(f"Generated {len(generated_tracks)} tracks, total duration: {format_duration(total_duration)}")

        # Concatenate audio
        update_progress(
            "concatenating_audio",
            f"Concatenating {len(generated_tracks)} tracks...",
            current_track=len(generated_tracks),
            total_duration_seconds=total_duration,
            total_duration_formatted=format_duration(total_duration)
        )

        audio_result = audio_processor.concatenate_audio_files(
            generated_tracks,
            final_audio_path
        )

        # Generate tracklist
        audio_processor.generate_tracklist_json(audio_result, tracklist_path)

        # Process video
        update_progress(
            "processing_video",
            "Looping video to match audio duration...",
            current_track=len(generated_tracks),
            total_duration_seconds=audio_result.total_duration_seconds,
            total_duration_formatted=audio_result.total_duration_formatted
        )

        video_processor.process_video_with_audio(
            video_path,
            final_audio_path,
            audio_result.total_duration_seconds,
            final_video_path
        )

        # Save job metadata
        metadata = {
            "job_id": job_id,
            "prompt": prompt,
            "created_at": datetime.utcnow().isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "total_tracks": len(generated_tracks),
            "total_duration": audio_result.total_duration_formatted,
            "total_duration_seconds": audio_result.total_duration_seconds,
            "video_file": str(final_video_path),
            "audio_file": str(final_audio_path),
            "tracklist_file": str(tracklist_path)
        }

        with open(job_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        # Cleanup tracks directory
        shutil.rmtree(tracks_dir, ignore_errors=True)
        shutil.rmtree(job_dir / "audio_work", ignore_errors=True)
        shutil.rmtree(job_dir / "video_work", ignore_errors=True)

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
        logger.error(f"Task {job_id} exceeded time limit")
        update_progress("failed", "Task exceeded time limit")
        raise

    except (KieAIError, AudioProcessingError, VideoProcessingError) as e:
        logger.error(f"Task {job_id} failed: {e}")
        progress["stage"] = "failed"
        progress["message"] = str(e)
        self.update_state(state=states.FAILURE, meta=progress)
        raise

    except Exception as e:
        logger.exception(f"Unexpected error in task {job_id}")
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
        if file_path.is_file():
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


# Celery beat schedule for cleanup
celery_app.conf.beat_schedule = {
    "cleanup-old-files": {
        "task": "app.workers.tasks.cleanup_old_files",
        "schedule": 3600.0,  # Run every hour
    },
}

"""
FastAPI application for AI Music Video Generator.
"""

import uuid
import logging
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.models import (
    GenerateResponse,
    StatusResponse,
    JobStatus,
    JobProgress,
    Tracklist,
    TrackInfo,
    ErrorResponse,
)
from app.workers.tasks import generate_music_video, create_mix_video
from app.workers.celery_app import celery_app
from app.config import (
    UPLOAD_DIR,
    OUTPUT_DIR,
    CHANNELS_FILE,
    ALLOWED_VIDEO_EXTENSIONS,
    MAX_UPLOAD_SIZE_BYTES,
    HOST,
    PORT,
)
from app.services.video import validate_video_format, VideoProcessingError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="AI Music Video Generator",
    description="Generate 2-hour music videos with AI-generated music",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ==================== CHANNELS MANAGEMENT ====================

def load_channels():
    """Load channels from JSON file."""
    if not CHANNELS_FILE.exists():
        return []
    try:
        with open(CHANNELS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_channels(channels: list):
    """Save channels to JSON file."""
    with open(CHANNELS_FILE, "w") as f:
        json.dump(channels, f, indent=2)


def get_channel_by_id(channel_id: str):
    """Get a channel by its ID."""
    channels = load_channels()
    for channel in channels:
        if channel["id"] == channel_id:
            return channel
    return None


@app.get("/channels")
async def list_channels():
    """List all channels."""
    channels = load_channels()
    return {"channels": channels}


@app.post("/channels")
async def create_channel(
    name: str = Form(...),
    description: str = Form("")
):
    """Create a new channel."""
    channels = load_channels()

    # Generate channel ID (slug from name)
    import re
    channel_id = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

    # Check if already exists
    for ch in channels:
        if ch["id"] == channel_id:
            raise HTTPException(status_code=400, detail="Channel with this name already exists")

    new_channel = {
        "id": channel_id,
        "name": name,
        "description": description,
        "created_at": datetime.utcnow().isoformat()
    }

    channels.append(new_channel)
    save_channels(channels)

    logger.info(f"Created channel: {channel_id}")
    return {"message": "Channel created", "channel": new_channel}


@app.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    """Delete a channel (does not delete jobs)."""
    channels = load_channels()

    # Find and remove channel
    new_channels = [ch for ch in channels if ch["id"] != channel_id]

    if len(new_channels) == len(channels):
        raise HTTPException(status_code=404, detail="Channel not found")

    save_channels(new_channels)
    logger.info(f"Deleted channel: {channel_id}")

    return {"message": f"Channel '{channel_id}' deleted"}


# ==================== ROUTES ====================

@app.get("/")
async def root():
    """Serve the frontend."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/jobs")
async def list_jobs(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    channel: Optional[str] = Query(None, description="Filter by channel ID")
):
    """
    List all jobs with their metadata (history).
    Optionally filter by channel.
    """
    jobs = []

    if not OUTPUT_DIR.exists():
        return {"jobs": [], "total": 0}

    # Get all job directories
    job_dirs = sorted(
        [d for d in OUTPUT_DIR.iterdir() if d.is_dir()],
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )

    # Filter by channel if specified
    filtered_job_dirs = []
    for job_dir in job_dirs:
        metadata_path = job_dir / "metadata.json"
        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    metadata = json.load(f)

                job_channel = metadata.get("channel_id")

                # If channel filter is set, only include matching jobs
                if channel is not None:
                    if job_channel != channel:
                        continue

                filtered_job_dirs.append((job_dir, metadata))
            except Exception as e:
                logger.warning(f"Failed to read metadata for {job_dir}: {e}")
                if channel is None:  # Include broken metadata jobs only if no filter
                    filtered_job_dirs.append((job_dir, None))
        else:
            # No metadata file - include only if no channel filter
            if channel is None:
                filtered_job_dirs.append((job_dir, None))

    total = len(filtered_job_dirs)

    # Apply pagination
    filtered_job_dirs = filtered_job_dirs[offset:offset + limit]

    for job_dir, metadata in filtered_job_dirs:
        if metadata:
            try:
                # Check for tracks directory
                tracks_dir = job_dir / "tracks"
                tracks_count = len(list(tracks_dir.glob("*.mp3"))) if tracks_dir.exists() else 0

                # Check for logs
                has_logs = (job_dir / "logs.txt").exists()

                # Check for output files
                has_video = (job_dir / "final_video.mp4").exists()
                has_audio = (job_dir / "final_audio.mp3").exists()

                jobs.append({
                    "job_id": metadata.get("job_id", job_dir.name),
                    "job_type": metadata.get("job_type", "generate"),
                    "prompt": metadata.get("prompt", "")[:100] if metadata.get("prompt") else "",
                    "status": metadata.get("status", "unknown"),
                    "created_at": metadata.get("created_at"),
                    "completed_at": metadata.get("completed_at"),
                    "total_tracks": metadata.get("total_tracks", tracks_count),
                    "total_duration": metadata.get("total_duration", "00:00:00"),
                    "model": metadata.get("model", "V4"),
                    "channel_id": metadata.get("channel_id"),
                    "channel_name": metadata.get("channel_name"),
                    "has_video": has_video,
                    "has_audio": has_audio,
                    "has_logs": has_logs,
                    "tracks_available": tracks_count
                })
            except Exception as e:
                logger.error(f"Error reading metadata for {job_dir}: {e}")
                jobs.append({
                    "job_id": job_dir.name,
                    "status": "unknown",
                    "error": str(e)
                })
        else:
            jobs.append({
                "job_id": job_dir.name,
                "status": "unknown",
                "error": "Failed to read metadata"
            })

    return {"jobs": jobs, "total": total, "limit": limit, "offset": offset, "channel": channel}


@app.get("/jobs/{job_id}")
async def get_job_details(job_id: str):
    """
    Get detailed information about a specific job.
    """
    job_dir = OUTPUT_DIR / job_id
    metadata_path = job_dir / "metadata.json"

    if not metadata_path.exists():
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        with open(metadata_path) as f:
            metadata = json.load(f)

        # Add tracks list
        tracks_dir = job_dir / "tracks"
        tracks = []
        if tracks_dir.exists():
            for track_file in sorted(tracks_dir.glob("*.mp3")):
                tracks.append({
                    "filename": track_file.name,
                    "size_bytes": track_file.stat().st_size,
                    "size_mb": round(track_file.stat().st_size / (1024 * 1024), 2)
                })

        metadata["tracks_files"] = tracks

        # Check for output files
        metadata["has_video"] = (job_dir / "final_video.mp4").exists()
        metadata["has_audio"] = (job_dir / "final_audio.mp3").exists()
        metadata["has_logs"] = (job_dir / "logs.txt").exists()

        # Get file sizes
        if metadata["has_video"]:
            video_size = (job_dir / "final_video.mp4").stat().st_size
            metadata["video_size_mb"] = round(video_size / (1024 * 1024), 2)

        if metadata["has_audio"]:
            audio_size = (job_dir / "final_audio.mp3").stat().st_size
            metadata["audio_size_mb"] = round(audio_size / (1024 * 1024), 2)

        return metadata

    except Exception as e:
        logger.error(f"Error reading job details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jobs/{job_id}/logs")
async def get_job_logs(
    job_id: str,
    tail: int = Query(None, ge=1, le=1000, description="Get last N lines")
):
    """
    Get logs for a specific job.
    """
    job_dir = OUTPUT_DIR / job_id
    logs_path = job_dir / "logs.txt"

    if not logs_path.exists():
        raise HTTPException(status_code=404, detail="Logs not found")

    try:
        with open(logs_path, "r", encoding="utf-8") as f:
            if tail:
                # Read last N lines
                lines = f.readlines()
                content = "".join(lines[-tail:])
            else:
                content = f.read()

        return PlainTextResponse(content)

    except Exception as e:
        logger.error(f"Error reading logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jobs/{job_id}/tracks")
async def list_job_tracks(job_id: str):
    """
    List all individual tracks for a job.
    """
    job_dir = OUTPUT_DIR / job_id
    tracks_dir = job_dir / "tracks"
    metadata_path = job_dir / "metadata.json"

    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found")

    tracks = []

    # Get track info from metadata
    tracks_info = {}
    if metadata_path.exists():
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
                for track in metadata.get("tracks_info", []):
                    tracks_info[track.get("filename")] = track
        except Exception:
            pass

    # List track files
    if tracks_dir.exists():
        for track_file in sorted(tracks_dir.glob("*.mp3")):
            info = tracks_info.get(track_file.name, {})
            tracks.append({
                "filename": track_file.name,
                "track_number": info.get("track_number", 0),
                "title": info.get("title", track_file.stem),
                "duration_seconds": info.get("duration_seconds", 0),
                "duration_formatted": info.get("duration_formatted", "00:00:00"),
                "size_bytes": track_file.stat().st_size,
                "size_mb": round(track_file.stat().st_size / (1024 * 1024), 2),
                "download_url": f"/jobs/{job_id}/tracks/{track_file.name}"
            })

    return {"job_id": job_id, "tracks": tracks, "total": len(tracks)}


@app.get("/jobs/{job_id}/tracks/{filename}")
async def download_track(job_id: str, filename: str):
    """
    Download a specific track from a job.
    """
    # Sanitize filename
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    track_path = OUTPUT_DIR / job_id / "tracks" / filename

    if not track_path.exists():
        raise HTTPException(status_code=404, detail="Track not found")

    return FileResponse(
        path=str(track_path),
        media_type="audio/mpeg",
        filename=filename
    )


@app.get("/jobs-with-tracks")
async def list_jobs_with_tracks(
    channel: Optional[str] = Query(None, description="Filter by channel ID")
):
    """
    List all jobs that have tracks available for mixing.
    Optionally filter by channel.
    """
    jobs = []

    if not OUTPUT_DIR.exists():
        return {"jobs": []}

    # Get all job directories
    job_dirs = sorted(
        [d for d in OUTPUT_DIR.iterdir() if d.is_dir()],
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )

    for job_dir in job_dirs:
        tracks_dir = job_dir / "tracks"
        if not tracks_dir.exists():
            continue

        track_files = list(tracks_dir.glob("*.mp3"))
        if not track_files:
            continue

        metadata_path = job_dir / "metadata.json"
        job_info = {
            "job_id": job_dir.name,
            "tracks_count": len(track_files),
            "prompt": "",
            "created_at": None,
            "total_duration": "00:00:00",
            "channel_id": None,
            "channel_name": None
        }

        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    metadata = json.load(f)

                    # Filter by channel if specified
                    job_channel = metadata.get("channel_id")
                    if channel is not None and job_channel != channel:
                        continue

                    job_info["prompt"] = metadata.get("prompt", "")[:100]
                    job_info["created_at"] = metadata.get("created_at")
                    job_info["total_duration"] = metadata.get("total_duration", "00:00:00")
                    job_info["job_type"] = metadata.get("job_type", "generate")
                    job_info["channel_id"] = job_channel
                    job_info["channel_name"] = metadata.get("channel_name")
            except Exception:
                if channel is not None:  # Skip jobs with broken metadata when filtering
                    continue

        jobs.append(job_info)

    return {"jobs": jobs, "channel": channel}


@app.post("/mix")
async def create_mix(
    video: UploadFile = File(...),
    job_ids: str = Form(...),  # Comma-separated job IDs
    target_duration: int = Form(120),  # Target duration in minutes
    channel: Optional[str] = Form(None),  # Channel ID for the mix output
):
    """
    Create a mix video from tracks of selected jobs.

    - Upload a video file
    - Provide comma-separated job IDs to source tracks from
    - Optionally specify target duration in minutes (default: 120)
    - Optionally specify channel for the output
    """
    # Parse job IDs
    source_job_ids = [jid.strip() for jid in job_ids.split(",") if jid.strip()]

    if not source_job_ids:
        raise HTTPException(status_code=400, detail="No job IDs provided")

    # Get channel info if provided
    channel_name = None
    if channel:
        channel_info = get_channel_by_id(channel)
        if channel_info:
            channel_name = channel_info.get("name")

    # Validate at least one job has tracks
    valid_jobs = []
    total_available_tracks = 0
    for job_id in source_job_ids:
        tracks_dir = OUTPUT_DIR / job_id / "tracks"
        if tracks_dir.exists():
            track_count = len(list(tracks_dir.glob("*.mp3")))
            if track_count > 0:
                valid_jobs.append(job_id)
                total_available_tracks += track_count

    if not valid_jobs:
        raise HTTPException(
            status_code=400,
            detail="None of the selected jobs have tracks available"
        )

    # Validate video file
    if not video.filename:
        raise HTTPException(status_code=400, detail="No video file provided")

    file_ext = Path(video.filename).suffix.lower()
    if file_ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid video format. Allowed: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"
        )

    # Validate target duration
    target_duration = max(5, min(180, target_duration))  # 5 min to 3 hours

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Save uploaded video
    video_filename = f"{job_id}{file_ext}"
    video_path = UPLOAD_DIR / video_filename

    try:
        content = await video.read()
        if len(content) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Video file too large. Maximum size: {MAX_UPLOAD_SIZE_BYTES // (1024*1024)}MB"
            )

        video_path.write_bytes(content)
        logger.info(f"Saved video for mix to {video_path}")

        # Validate video file
        try:
            validate_video_format(video_path)
        except VideoProcessingError as e:
            video_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(e))

    except HTTPException:
        raise
    except Exception as e:
        video_path.unlink(missing_ok=True)
        logger.error(f"Failed to save video: {e}")
        raise HTTPException(status_code=500, detail="Failed to save video file")

    # Start Celery task
    try:
        task = create_mix_video.delay(
            job_id=job_id,
            source_job_ids=valid_jobs,
            video_filename=video_filename,
            target_duration_minutes=target_duration,
            channel_id=channel,
            channel_name=channel_name
        )

        logger.info(f"Started mix job {job_id} from {len(valid_jobs)} sources with task {task.id}")

        return {
            "job_id": job_id,
            "message": f"Mix creation started from {len(valid_jobs)} jobs ({total_available_tracks} tracks available). Use /status/{job_id} to check progress.",
            "status": "pending",
            "source_jobs": valid_jobs,
            "target_duration_minutes": target_duration,
            "channel_id": channel,
            "channel_name": channel_name
        }

    except Exception as e:
        video_path.unlink(missing_ok=True)
        logger.error(f"Failed to start mix task: {e}")
        raise HTTPException(status_code=500, detail="Failed to start mix task")


@app.post("/generate", response_model=GenerateResponse)
async def start_generation(
    video: UploadFile = File(...),
    prompt: str = Form(...),
    api_key: str = Form(...),
    custom_mode: bool = Form(False),
    instrumental: bool = Form(True),
    model: str = Form("V4"),
    style: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    concurrent_tracks: int = Form(1),
    target_duration: int = Form(120),  # Target duration in minutes
    channel: Optional[str] = Form(None),  # Channel ID for the generated content
):
    """
    Start a new music video generation job.

    - Upload a video file (mp4, mov, avi, mkv)
    - Provide a prompt for music generation
    - Provide your Kie AI API key
    - Optional: customize generation settings
    - Optional: specify channel for the output
    """
    # Validate API key
    if not api_key or len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key")

    # Get channel info if provided
    channel_name = None
    if channel:
        channel_info = get_channel_by_id(channel)
        if channel_info:
            channel_name = channel_info.get("name")

    # Validate prompt
    if not prompt or len(prompt.strip()) < 3:
        raise HTTPException(status_code=400, detail="Prompt must be at least 3 characters")

    if len(prompt) > 500:
        raise HTTPException(status_code=400, detail="Prompt must be less than 500 characters")

    # Validate video file
    if not video.filename:
        raise HTTPException(status_code=400, detail="No video file provided")

    file_ext = Path(video.filename).suffix.lower()
    if file_ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid video format. Allowed: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"
        )

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Save uploaded video
    video_filename = f"{job_id}{file_ext}"
    video_path = UPLOAD_DIR / video_filename

    try:
        # Read and save video with size check
        content = await video.read()
        if len(content) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Video file too large. Maximum size: {MAX_UPLOAD_SIZE_BYTES // (1024*1024)}MB"
            )

        video_path.write_bytes(content)
        logger.info(f"Saved video to {video_path}")

        # Validate video file
        try:
            validate_video_format(video_path)
        except VideoProcessingError as e:
            video_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(e))

    except HTTPException:
        raise
    except Exception as e:
        video_path.unlink(missing_ok=True)
        logger.error(f"Failed to save video: {e}")
        raise HTTPException(status_code=500, detail="Failed to save video file")

    # Validate concurrent_tracks
    concurrent_tracks = max(1, min(4, concurrent_tracks))  # Limit between 1-4

    # Validate target_duration
    target_duration = max(30, min(180, target_duration))  # Limit between 30-180 minutes

    # Start Celery task
    try:
        task = generate_music_video.delay(
            job_id=job_id,
            prompt=prompt.strip(),
            api_key=api_key,
            video_filename=video_filename,
            custom_mode=custom_mode,
            instrumental=instrumental,
            model=model,
            style=style,
            title=title,
            concurrent_tracks=concurrent_tracks,
            channel_id=channel,
            channel_name=channel_name,
            target_duration_minutes=target_duration
        )

        logger.info(f"Started job {job_id} with task {task.id} (channel: {channel})")

        return GenerateResponse(
            job_id=job_id,
            message="Generation started. Use /status/{job_id} to check progress.",
            status=JobStatus.PENDING
        )

    except Exception as e:
        video_path.unlink(missing_ok=True)
        logger.error(f"Failed to start task: {e}")
        raise HTTPException(status_code=500, detail="Failed to start generation task")


@app.get("/status/{job_id}", response_model=StatusResponse)
async def get_status(job_id: str):
    """
    Get the status of a generation job.
    """
    # Check if job directory exists
    job_dir = OUTPUT_DIR / job_id
    metadata_path = job_dir / "metadata.json"

    if metadata_path.exists():
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)

            status = metadata.get("status", "unknown")

            # Map status to JobStatus enum
            status_map = {
                "processing": JobStatus.GENERATING_AUDIO,
                "completed": JobStatus.COMPLETED,
                "failed": JobStatus.FAILED
            }
            job_status = status_map.get(status, JobStatus.PENDING)

            # Job completed or failed
            tracklist_path = job_dir / "tracklist.json"
            tracklist = None
            if tracklist_path.exists():
                with open(tracklist_path) as f:
                    tl_data = json.load(f)
                    tracks = [
                        TrackInfo(
                            id=t["id"],
                            start=t["start"],
                            end=t["end"],
                            duration=t["duration"],
                            title=t.get("title"),
                            kie_track_id=t.get("kie_track_id")
                        )
                        for t in tl_data.get("tracks", [])
                    ]
                    tracklist = Tracklist(
                        total_duration=tl_data.get("total_duration", "00:00:00"),
                        total_tracks=len(tracks),
                        tracks=tracks
                    )

            # Get progress info from metadata
            progress_stage = "completed" if status == "completed" else "failed" if status == "failed" else "processing"
            progress_message = "Video generation completed!" if status == "completed" else metadata.get("error", "Processing...")

            return StatusResponse(
                job_id=job_id,
                status=job_status,
                progress=JobProgress(
                    current_track=metadata.get("total_tracks", 0),
                    total_duration_seconds=metadata.get("total_duration_seconds", 0),
                    total_duration_formatted=metadata.get("total_duration", "00:00:00"),
                    stage=progress_stage,
                    message=progress_message
                ),
                tracklist=tracklist,
                error=metadata.get("error"),
                created_at=metadata.get("created_at"),
                completed_at=metadata.get("completed_at")
            )
        except Exception as e:
            logger.error(f"Error reading metadata: {e}")

    # Check if video file was uploaded (task in progress)
    upload_files = list(UPLOAD_DIR.glob(f"{job_id}.*"))
    if not upload_files and not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found")

    # Task is in progress or pending
    if upload_files:
        return StatusResponse(
            job_id=job_id,
            status=JobStatus.GENERATING_AUDIO,
            progress=JobProgress(
                current_track=0,
                total_duration_seconds=0,
                total_duration_formatted="00:00:00",
                stage="generating_audio",
                message="Generation in progress..."
            )
        )

    raise HTTPException(status_code=404, detail="Job not found")


@app.get("/download/{job_id}/video")
async def download_video(job_id: str):
    """
    Download the generated video file.
    """
    video_path = OUTPUT_DIR / job_id / "final_video.mp4"

    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found or not yet ready")

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"music_video_{job_id}.mp4"
    )


@app.get("/download/{job_id}/audio")
async def download_audio(job_id: str):
    """
    Download the generated audio file.
    """
    audio_path = OUTPUT_DIR / job_id / "final_audio.mp3"

    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio not found or not yet ready")

    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        filename=f"music_{job_id}.mp3"
    )


@app.get("/download/{job_id}/tracklist")
async def download_tracklist(job_id: str):
    """
    Download the tracklist JSON file.
    """
    tracklist_path = OUTPUT_DIR / job_id / "tracklist.json"

    if not tracklist_path.exists():
        raise HTTPException(status_code=404, detail="Tracklist not found or not yet ready")

    return FileResponse(
        path=str(tracklist_path),
        media_type="application/json",
        filename=f"tracklist_{job_id}.json"
    )


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """
    Delete a job and its associated files.
    """
    import shutil

    job_dir = OUTPUT_DIR / job_id
    upload_files = list(UPLOAD_DIR.glob(f"{job_id}.*"))

    deleted = False

    if job_dir.exists():
        shutil.rmtree(job_dir)
        deleted = True

    for f in upload_files:
        f.unlink()
        deleted = True

    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")

    return {"message": f"Job {job_id} deleted successfully"}


@app.get("/health")
async def health_check():
    """
    Health check endpoint.
    """
    # Check Redis connection
    try:
        celery_app.control.ping(timeout=1)
        celery_status = "healthy"
    except Exception:
        celery_status = "unhealthy"

    return {
        "status": "healthy",
        "celery": celery_status,
        "timestamp": datetime.utcnow().isoformat()
    }


# Run with uvicorn
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)

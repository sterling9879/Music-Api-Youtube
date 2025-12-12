"""
FastAPI application for AI Music Video Generator.
"""

import uuid
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
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
from app.workers.tasks import generate_music_video
from app.workers.celery_app import celery_app
from app.config import (
    UPLOAD_DIR,
    OUTPUT_DIR,
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


@app.get("/")
async def root():
    """Serve the frontend."""
    return FileResponse(str(STATIC_DIR / "index.html"))


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
):
    """
    Start a new music video generation job.

    - Upload a video file (mp4, mov, avi, mkv)
    - Provide a prompt for music generation
    - Provide your Kie AI API key
    - Optional: customize generation settings
    """
    # Validate API key
    if not api_key or len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key")

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
            title=title
        )

        logger.info(f"Started job {job_id} with task {task.id}")

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
    # Find the task by job_id
    # We need to search through task results
    task_result = None

    # Check if job directory exists
    job_dir = OUTPUT_DIR / job_id
    metadata_path = job_dir / "metadata.json"

    # Try to get task from Celery
    # First, check the AsyncResult
    from celery.result import AsyncResult
    import json

    # We store job_id in the task, so we need to iterate or use a mapping
    # For simplicity, we'll check if the job has completed by looking at files
    if metadata_path.exists():
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)

            # Job completed successfully
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

            return StatusResponse(
                job_id=job_id,
                status=JobStatus.COMPLETED,
                progress=JobProgress(
                    current_track=metadata.get("total_tracks", 0),
                    total_duration_seconds=metadata.get("total_duration_seconds", 0),
                    total_duration_formatted=metadata.get("total_duration", "00:00:00"),
                    stage="completed",
                    message="Video generation completed!"
                ),
                tracklist=tracklist,
                created_at=metadata.get("created_at"),
                completed_at=metadata.get("completed_at")
            )
        except Exception as e:
            logger.error(f"Error reading metadata: {e}")

    # Check if video file was uploaded (task in progress)
    upload_files = list(UPLOAD_DIR.glob(f"{job_id}.*"))
    if not upload_files and not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found")

    # Task is in progress or pending - check Celery state
    # Try to find the task by inspecting active tasks
    inspect = celery_app.control.inspect()

    # Check active tasks
    active = inspect.active() or {}
    reserved = inspect.reserved() or {}
    scheduled = inspect.scheduled() or {}

    task_info = None
    task_state = None

    for worker_tasks in [active, reserved, scheduled]:
        for worker, tasks in worker_tasks.items():
            for task in tasks:
                if isinstance(task, dict):
                    args = task.get("args", [])
                    if args and len(args) > 0 and args[0] == job_id:
                        task_info = task
                        task_state = "PROGRESS"
                        break

    # Also check for task result directly using task ID pattern
    # In practice, you might want to store task_id -> job_id mapping
    # For now, return generic in-progress status if files exist
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


@app.delete("/job/{job_id}")
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

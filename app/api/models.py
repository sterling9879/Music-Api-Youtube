"""
Pydantic models for API requests and responses.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
from datetime import datetime


class JobStatus(str, Enum):
    """Possible states of a generation job."""
    PENDING = "pending"
    GENERATING_AUDIO = "generating_audio"
    CONCATENATING_AUDIO = "concatenating_audio"
    PROCESSING_VIDEO = "processing_video"
    COMBINING = "combining"
    COMPLETED = "completed"
    FAILED = "failed"


class GenerateRequest(BaseModel):
    """Request model for starting a new generation job."""
    prompt: str = Field(..., min_length=1, max_length=500, description="Music generation prompt")
    api_key: str = Field(..., min_length=1, description="Kie AI API key")
    custom_mode: bool = Field(default=False, description="Use custom mode for generation")
    instrumental: bool = Field(default=True, description="Generate instrumental music without vocals")
    model: str = Field(default="V4", description="AI model to use (V4, V4_5, V5)")
    style: Optional[str] = Field(default=None, max_length=200, description="Music style (required for custom mode)")
    title: Optional[str] = Field(default=None, max_length=80, description="Base title for generated tracks")


class GenerateResponse(BaseModel):
    """Response model after starting a generation job."""
    job_id: str
    message: str
    status: JobStatus


class TrackInfo(BaseModel):
    """Information about a single track in the tracklist."""
    id: int
    start: str  # HH:MM:SS format
    end: str  # HH:MM:SS format
    duration: str  # HH:MM:SS format
    title: Optional[str] = None
    kie_track_id: Optional[str] = None


class Tracklist(BaseModel):
    """Complete tracklist for the generated audio."""
    total_duration: str  # HH:MM:SS format
    total_tracks: int
    tracks: List[TrackInfo]


class JobProgress(BaseModel):
    """Progress information for a job."""
    current_track: int = 0
    total_duration_seconds: float = 0.0
    total_duration_formatted: str = "00:00:00"
    stage: str = ""
    message: str = ""


class StatusResponse(BaseModel):
    """Response model for job status queries."""
    job_id: str
    status: JobStatus
    progress: JobProgress
    tracklist: Optional[Tracklist] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class DownloadResponse(BaseModel):
    """Response model for download information."""
    job_id: str
    video_url: str
    tracklist_url: str
    audio_url: str


class KieAITrackResponse(BaseModel):
    """Response model from Kie AI for a single track."""
    id: str
    audio_url: str
    stream_audio_url: Optional[str] = None
    image_url: Optional[str] = None
    prompt: Optional[str] = None
    model_name: Optional[str] = None
    title: Optional[str] = None
    tags: Optional[str] = None
    create_time: Optional[str] = None
    duration: float  # Duration in seconds


class KieAIGenerateResponse(BaseModel):
    """Response model from Kie AI generate endpoint."""
    code: int
    msg: str
    data: dict


class KieAIStatusResponse(BaseModel):
    """Response model from Kie AI status endpoint."""
    code: int
    msg: str
    data: dict


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None

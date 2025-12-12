"""
Configuration settings for AI Music Video Generator.
All API keys are configured via frontend and passed per-request.
"""

import os
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).parent.parent
APP_DIR = Path(__file__).parent
UPLOAD_DIR = APP_DIR / "uploads"
OUTPUT_DIR = APP_DIR / "output"

# Ensure directories exist
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Redis configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Celery configuration
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

# API settings
KIE_AI_BASE_URL = "https://api.kie.ai/api/v1"
KIE_AI_GENERATE_ENDPOINT = f"{KIE_AI_BASE_URL}/generate"
KIE_AI_STATUS_ENDPOINT = f"{KIE_AI_BASE_URL}/generate/record-info"

# Audio generation settings
TARGET_DURATION_MINUTES = 110  # Stop when reaching 1:50 (110 minutes)
MAX_DURATION_MINUTES = 120  # Maximum 2 hours
MUSIC_GENERATION_TIMEOUT = 600  # 10 minutes timeout per song
MAX_RETRIES = 3
POLLING_INTERVAL = 30  # seconds between status checks

# Video settings
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_UPLOAD_SIZE_MB = 500
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# Cleanup settings
FILE_RETENTION_HOURS = 24

# Server settings
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))

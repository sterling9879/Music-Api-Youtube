"""
Celery application configuration.
"""

from celery import Celery
from app.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND

# Create Celery app
celery_app = Celery(
    "music_video_generator",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=["app.workers.tasks"]
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=14400,  # 4 hours max
    task_soft_time_limit=13800,  # 3.8 hours soft limit
    worker_prefetch_multiplier=1,  # Process one task at a time
    task_acks_late=True,  # Acknowledge after completion
    result_expires=86400,  # Results expire after 24 hours
)

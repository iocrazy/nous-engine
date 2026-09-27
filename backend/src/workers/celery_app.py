from celery import Celery

from src.config import get_settings

settings = get_settings()

celery_app = Celery(
    "mind-center",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_routes={
        "src.workers.tts_worker.*": {"queue": "tts"},
        "src.workers.video_worker.*": {"queue": "video"},
    },
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

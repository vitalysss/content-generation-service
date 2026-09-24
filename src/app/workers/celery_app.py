from celery import Celery

from app.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "content_generation",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 900},
    imports=("app.workers.tasks",),
    beat_schedule={
        "dispatch-generation-outbox": {
            "task": "generation.dispatch_outbox",
            "schedule": 2.0,
        },
        "recover-stale-generations": {
            "task": "generation.recover_stale",
            "schedule": 30.0,
        },
        "dispatch-callback-outbox": {
            "task": "generation.dispatch_callback_outbox",
            "schedule": 2.0,
        },
    },
)

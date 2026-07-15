from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure, worker_process_init

from . import notifier, observability
from .config import settings

celery_app = Celery(
    "waichatt",
    broker=settings.redis_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.agent_tasks"],
)

_TASKS = "app.tasks.agent_tasks"
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    timezone=settings.celery_timezone,
    task_default_queue="default",
    task_routes={
        f"{_TASKS}.process_conversation": {"queue": "agent_messages"},
        f"{_TASKS}.send_outbound_message": {"queue": "agent_outbound"},
        f"{_TASKS}.classify_and_persist_lead": {"queue": "agent_outbound"},
        f"{_TASKS}.retry_stale_processing_jobs": {"queue": "agent_messages"},
        f"{_TASKS}.requeue_stuck_conversation_jobs": {"queue": "agent_messages"},
        f"{_TASKS}.schedule_due_followups": {"queue": "agent_outbound"},
        f"{_TASKS}.dispatch_pending_outbox_messages": {"queue": "agent_outbound"},
        f"{_TASKS}.cleanup_expired_locks": {"queue": "agent_messages"},
    },
    beat_schedule={
        "retry-stale-processing-jobs": {
            "task": f"{_TASKS}.retry_stale_processing_jobs",
            "schedule": crontab(minute="*/5"),
        },
        "dispatch-pending-outbox-messages": {
            "task": f"{_TASKS}.dispatch_pending_outbox_messages",
            "schedule": crontab(minute="*/1"),
        },
        "schedule-due-followups": {
            "task": f"{_TASKS}.schedule_due_followups",
            "schedule": crontab(minute="*/15", hour="7-21"),
        },
        "requeue-stuck-conversation-jobs": {
            "task": f"{_TASKS}.requeue_stuck_conversation_jobs",
            "schedule": crontab(minute="*/5"),
        },
        "cleanup-expired-locks": {
            "task": f"{_TASKS}.cleanup_expired_locks",
            "schedule": crontab(minute="*/15"),
        },
    },
)

_SILENCED_ALERTS = {
    f"{_TASKS}.retry_stale_processing_jobs",
    f"{_TASKS}.dispatch_pending_outbox_messages",
    f"{_TASKS}.schedule_due_followups",
    f"{_TASKS}.requeue_stuck_conversation_jobs",
    f"{_TASKS}.cleanup_expired_locks",
}


@worker_process_init.connect
def _init_worker_tracing(**_: object) -> None:
    observability.init_tracing()


@task_failure.connect
def _alert_task_failure(sender=None, task_id=None, exception=None, args=None, einfo=None, **_: object) -> None:
    name = getattr(sender, "name", "?")
    if name in _SILENCED_ALERTS:
        return
    notifier.notify_error(
        f"task {name} falló",
        detalle=(einfo.traceback if einfo else str(exception)),
        contexto={"task_id": task_id, "args": args},
    )

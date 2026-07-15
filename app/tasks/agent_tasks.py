from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

import redis
from celery import chain

from app import chat_memory, notifier, service
from app.agent import AgentError
from app.celery_app import celery_app
from app.chatwoot import ChatwootError, build_chatwoot_client
from app.classifier import STAGE_LABELS, classify
from app.config import settings
from app.db import DBError
from app.ycloud import YCloudError, build_ycloud_client

logger = logging.getLogger(__name__)
RETRYABLE = (DBError, YCloudError, ChatwootError, AgentError)
SWEEPER_RETRY = dict(autoretry_for=RETRYABLE, retry_backoff=True, retry_jitter=True, max_retries=3)
FOLLOWUP_MESSAGE = "Hola!, Quizas se te perdió el mensaje anterior. pudiste verlo?"
FOLLOWUP_PREFIX = "followup:"


def _followup_window_open(now: datetime | None = None) -> bool:
    timezone = ZoneInfo(settings.celery_timezone)
    local = now.astimezone(timezone) if now else datetime.now(timezone)
    return 7 <= local.hour < 22


def _is_followup_outbox(outbox: dict) -> bool:
    return str(outbox.get("idempotency_key") or "").startswith(FOLLOWUP_PREFIX)


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def debounce_key(conversation_id: int | str) -> str:
    return f"waichatt:conversation:{conversation_id}:debounce"


def lock_key(conversation_id: int | str) -> str:
    return f"waichatt:conversation:{conversation_id}:lock"


def requeue_key(conversation_id: int | str) -> str:
    return f"waichatt:conversation:{conversation_id}:requeue"


def worker_id(task_id: str | None = None) -> str:
    return f"{socket.gethostname()}:{task_id or 'unknown'}"


def set_conversation_debounce(conversation_id: int | str) -> None:
    try:
        _redis().set(
            debounce_key(conversation_id), str(time.time()), ex=max(1, settings.debounce_seconds)
        )
    except redis.RedisError as exc:
        logger.warning("debounce_set_failed", extra={"conversation_id": conversation_id, "error": str(exc)})


def _debounce_active(conversation_id: int | str) -> bool:
    try:
        return bool(_redis().exists(debounce_key(conversation_id)))
    except redis.RedisError:
        return False


def _debounce_ttl(conversation_id: int | str) -> int:
    try:
        return max(0, int(_redis().ttl(debounce_key(conversation_id))))
    except redis.RedisError:
        return 0


def _requeue_once(conversation_id: int | str, countdown: int) -> bool:
    countdown = max(1, countdown)
    try:
        was_set = bool(
            _redis().set(requeue_key(conversation_id), str(time.time()), nx=True, ex=countdown)
        )
    except redis.RedisError:
        was_set = True
    if was_set:
        process_conversation.apply_async(
            (str(conversation_id),), queue="agent_messages", countdown=countdown
        )
    return was_set


@contextmanager
def _conversation_lock(conversation_id: int | str, task_id: str | None) -> Iterator[bool]:
    client = _redis()
    key, value = lock_key(conversation_id), worker_id(task_id)
    acquired = bool(client.set(key, value, nx=True, ex=max(1, settings.lock_seconds)))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if client.get(key) == value:
                    client.delete(key)
            except redis.RedisError:
                logger.warning("lock_release_failed", extra={"conversation_id": conversation_id})


@celery_app.task(
    bind=True,
    name="app.tasks.agent_tasks.process_conversation",
    queue="agent_messages",
    autoretry_for=RETRYABLE,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=settings.job_max_retries,
)
def process_conversation(self, conversation_id: str) -> dict[str, object]:
    task_id = self.request.id
    if _debounce_active(conversation_id):
        _requeue_once(conversation_id, _debounce_ttl(conversation_id) + 1)
        return {"ok": True, "conversation_id": conversation_id, "status": "debounced"}

    with _conversation_lock(conversation_id, task_id) as acquired:
        if not acquired:
            _requeue_once(conversation_id, settings.debounce_retry_seconds)
            return {"ok": True, "conversation_id": conversation_id, "status": "lock_busy"}

        conversation = chat_memory.get_conversation(int(conversation_id))
        if not chat_memory.acquire_lock(
            conversation.channel, conversation.external_conversation_id, settings.lock_seconds
        ):
            _requeue_once(conversation_id, settings.debounce_retry_seconds)
            return {"ok": True, "conversation_id": conversation_id, "status": "db_lock_busy"}
        try:
            chat_memory.update_jobs(
                conversation.channel,
                conversation.external_conversation_id,
                "processing",
                worker_id=worker_id(task_id),
            )
            outbox_ids = service.process_pending_conversation_messages(int(conversation_id))
            if outbox_ids:
                tasks = [
                    send_outbound_message.si(str(outbox_id)).set(queue="agent_outbound")
                    for outbox_id in outbox_ids
                ]
                tasks.append(
                    classify_and_persist_lead.si(str(conversation_id)).set(queue="agent_outbound")
                )
                chain(*tasks).apply_async()
            return {"ok": True, "conversation_id": conversation_id, "outbox_ids": outbox_ids}
        except Exception as exc:
            status = "failed" if self.request.retries >= settings.job_max_retries else "retry"
            chat_memory.update_jobs(
                conversation.channel, conversation.external_conversation_id, status, error=str(exc)
            )
            chat_memory.update_events(
                conversation.channel, conversation.external_conversation_id, status, error=str(exc)
            )
            raise
        finally:
            chat_memory.release_lock(conversation.channel, conversation.external_conversation_id)


@celery_app.task(
    bind=True,
    name="app.tasks.agent_tasks.send_outbound_message",
    queue="agent_outbound",
    autoretry_for=RETRYABLE,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=settings.outbox_max_retries,
)
def send_outbound_message(self, outbox_id: str) -> dict[str, object]:
    outbox = chat_memory.get_outbox(int(outbox_id))
    if outbox is None:
        return {"ok": False, "outbox_id": outbox_id, "status": "not_found"}
    if outbox["status"] in ("sent", "failed", "canceled"):
        if outbox["status"] == "sent":
            _after_successful_send(outbox)
        return {
            "ok": outbox["status"] in ("sent", "canceled"),
            "outbox_id": outbox_id,
            "status": f"already_{outbox['status']}",
        }
    if _is_followup_outbox(outbox):
        if not settings.followup_enabled:
            return {"ok": True, "outbox_id": outbox_id, "status": "followup_disabled"}
        if not _followup_window_open():
            return {"ok": True, "outbox_id": outbox_id, "status": "quiet_hours"}
        if not chat_memory.followup_still_due(
            int(outbox["conversation_id"]),
            settings.followup_delay_hours,
            settings.followup_max_age_hours,
        ):
            chat_memory.mark_outbox_canceled(int(outbox_id), "conversation no longer eligible")
            return {"ok": True, "outbox_id": outbox_id, "status": "canceled"}
    if not chat_memory.mark_outbox_processing(int(outbox_id)):
        return {"ok": True, "outbox_id": outbox_id, "status": "already_claimed"}

    conversation = chat_memory.get_conversation(int(outbox["conversation_id"]))
    ycloud_client = None
    chatwoot_client = None
    account_id = conversation.account_id or settings.chatwoot_account_id
    if outbox["channel"] == service.YCLOUD_CHANNEL:
        ycloud_client = build_ycloud_client(settings.ycloud_api_key, settings.ycloud_base_url)
        configuration_error = (
            None
            if ycloud_client is not None and settings.ycloud_whatsapp_from
            else "YCloud no configurado"
        )
    elif outbox["channel"] == service.CHATWOOT_CHANNEL:
        chatwoot_client = build_chatwoot_client(
            settings.chatwoot_url, settings.chatwoot_access_token
        )
        configuration_error = (
            None
            if chatwoot_client is not None and account_id is not None
            else "Chatwoot no configurado"
        )
    else:
        configuration_error = f"Canal no soportado: {outbox['channel']}"

    if configuration_error:
        status = chat_memory.mark_outbox_retry_or_failed(int(outbox_id), configuration_error)
        if status == "failed":
            return {"ok": False, "outbox_id": outbox_id, "status": "failed"}
        raise self.retry(countdown=settings.debounce_retry_seconds)

    try:
        media_item = outbox.get("media")
        if ycloud_client and media_item:
            response = ycloud_client.send_media(
                settings.ycloud_whatsapp_from,
                outbox["external_conversation_id"],
                media_item["type"],
                media_item["link"],
                media_item.get("caption"),
            )
        elif ycloud_client:
            response = ycloud_client.send_text(
                settings.ycloud_whatsapp_from,
                outbox["external_conversation_id"],
                outbox["content"],
            )
        else:
            content = outbox["content"]
            if media_item:
                content = "\n".join(
                    part for part in (media_item.get("caption"), media_item["link"]) if part
                )
            response = chatwoot_client.create_outgoing_message(
                account_id, outbox["external_conversation_id"], content
            )
        chat_memory.mark_outbox_sent(int(outbox_id), response)
    except Exception as exc:
        status = chat_memory.mark_outbox_retry_or_failed(int(outbox_id), str(exc))
        if status == "failed":
            raise
        raise self.retry(exc=exc)
    _after_successful_send(outbox)
    return {"ok": True, "outbox_id": outbox_id, "status": "sent"}


def _after_successful_send(outbox: dict) -> None:
    if _is_followup_outbox(outbox):
        chat_memory.add_message(
            int(outbox["conversation_id"]),
            "assistant",
            outbox["content"],
            external_message_id=outbox["idempotency_key"],
        )
        chat_memory.set_conversation_state(int(outbox["conversation_id"]), {"followup_sent": True})
        return
    if outbox.get("media") or not service.is_handoff_reply(outbox.get("content") or ""):
        return
    conversation = chat_memory.get_conversation(int(outbox["conversation_id"]))
    contact = (conversation.state or {}).get("contact") or {}
    history = chat_memory.recent_history(conversation.id, 4)
    last_message = next((item.content for item in reversed(history) if item.role == "user"), "")
    notifier.notify_handoff(
        service.lead_phone(conversation), contact.get("name"), last_message
    )
    chat_memory.upsert_lead(
        service.lead_phone(conversation),
        name=contact.get("name"),
        stage="derivado",
        flags=["pidio_humano"],
        conversation_id=conversation.id,
    )
    if conversation.channel == service.CHATWOOT_CHANNEL and settings.chatwoot_assignee_id:
        client = build_chatwoot_client(settings.chatwoot_url, settings.chatwoot_access_token)
        account_id = conversation.account_id or settings.chatwoot_account_id
        if client and account_id:
            try:
                client.assign_conversation(
                    account_id,
                    conversation.external_conversation_id,
                    settings.chatwoot_assignee_id,
                )
            except ChatwootError as exc:
                logger.warning(
                    "chatwoot_assignment_failed",
                    extra={"conversation_id": conversation.id, "error": str(exc)},
                )


@celery_app.task(
    name="app.tasks.agent_tasks.classify_and_persist_lead", queue="agent_outbound"
)
def classify_and_persist_lead(conversation_id: str) -> dict[str, object]:
    conversation = chat_memory.get_conversation(int(conversation_id))
    result = classify(chat_memory.recent_history(conversation.id, settings.history_limit))
    contact = (conversation.state or {}).get("contact") or {}
    chat_memory.upsert_lead(
        service.lead_phone(conversation),
        name=result.nombre or contact.get("name"),
        inmobiliaria=result.inmobiliaria,
        es_dueno=result.es_dueno,
        consultas=result.consultas,
        equipos=result.equipos,
        stage=result.stage,
        flags=list(result.flags),
        conversation_id=conversation.id,
    )
    lead_state = {
        "stage": result.stage,
        "flags": list(result.flags),
        "followup_eligible": result.followup_eligible,
    }
    chat_memory.set_conversation_state(conversation.id, {"lead": lead_state})
    _sync_chatwoot_labels(conversation, result.stage, list(result.flags))
    return {"ok": True, "conversation_id": conversation_id, **lead_state}


def _sync_chatwoot_labels(
    conversation: chat_memory.Conversation, stage: str, flags: list[str]
) -> None:
    if conversation.channel != service.CHATWOOT_CHANNEL:
        return
    client = build_chatwoot_client(settings.chatwoot_url, settings.chatwoot_access_token)
    account_id = conversation.account_id or settings.chatwoot_account_id
    if not client or not account_id:
        return
    try:
        labels = [
            label
            for label in client.get_conversation_labels(
                account_id, conversation.external_conversation_id
            )
            if label not in STAGE_LABELS
        ]
        for label in (stage, *flags):
            if label not in labels:
                labels.append(label)
        client.set_conversation_labels(account_id, conversation.external_conversation_id, labels)
    except ChatwootError as exc:
        logger.warning(
            "chatwoot_labels_failed",
            extra={"conversation_id": conversation.id, "error": str(exc)},
        )


@celery_app.task(
    name="app.tasks.agent_tasks.retry_stale_processing_jobs", queue="agent_messages", **SWEEPER_RETRY
)
def retry_stale_processing_jobs() -> dict[str, object]:
    ids = chat_memory.requeue_stale_jobs(settings.channel, settings.stale_processing_minutes)
    for conversation_id in ids:
        process_conversation.apply_async((str(conversation_id),), queue="agent_messages")
    return {"ok": True, "requeued": len(ids)}


@celery_app.task(
    name="app.tasks.agent_tasks.schedule_due_followups", queue="agent_outbound", **SWEEPER_RETRY
)
def schedule_due_followups() -> dict[str, object]:
    if not settings.followup_enabled or not _followup_window_open():
        return {"ok": True, "scheduled": 0}
    ids = chat_memory.due_followup_conversation_ids(
        settings.channel,
        settings.followup_delay_hours,
        settings.followup_max_age_hours,
    )
    scheduled = 0
    for conversation_id in ids:
        conversation = chat_memory.get_conversation(conversation_id)
        outbox = chat_memory.create_outbox(
            conversation.id,
            conversation.external_conversation_id,
            conversation.channel,
            FOLLOWUP_MESSAGE,
            f"{FOLLOWUP_PREFIX}{conversation.channel}:{conversation.id}",
        )
        if outbox and outbox["status"] in ("pending", "retry"):
            send_outbound_message.apply_async((str(outbox["id"]),), queue="agent_outbound")
            scheduled += 1
    return {"ok": True, "scheduled": scheduled}


@celery_app.task(
    name="app.tasks.agent_tasks.dispatch_pending_outbox_messages", queue="agent_outbound", **SWEEPER_RETRY
)
def dispatch_pending_outbox_messages() -> dict[str, object]:
    rows = [
        row
        for row in chat_memory.pending_outbox(settings.channel)
        if not _is_followup_outbox(row)
        or (settings.followup_enabled and _followup_window_open())
    ]
    for row in rows:
        send_outbound_message.apply_async((str(row["id"]),), queue="agent_outbound")
    return {"ok": True, "dispatched": len(rows)}


@celery_app.task(
    name="app.tasks.agent_tasks.requeue_stuck_conversation_jobs", queue="agent_messages", **SWEEPER_RETRY
)
def requeue_stuck_conversation_jobs() -> dict[str, object]:
    ids = [
        *chat_memory.due_job_conversation_ids(settings.channel),
        *chat_memory.requeue_stale_jobs(settings.channel, settings.stale_processing_minutes),
    ]
    for conversation_id in set(ids):
        set_conversation_debounce(conversation_id)
        process_conversation.apply_async(
            (str(conversation_id),), queue="agent_messages", countdown=settings.debounce_seconds
        )
    return {"ok": True, "requeued": len(set(ids))}


@celery_app.task(
    name="app.tasks.agent_tasks.cleanup_expired_locks", queue="agent_messages", **SWEEPER_RETRY
)
def cleanup_expired_locks() -> dict[str, object]:
    return {"ok": True, "cleaned": chat_memory.cleanup_expired_locks()}


if __name__ == "__main__":
    timezone = ZoneInfo(settings.celery_timezone)
    assert _followup_window_open(datetime(2026, 7, 15, 7, 0, tzinfo=timezone))
    assert _followup_window_open(datetime(2026, 7, 15, 21, 59, tzinfo=timezone))
    assert not _followup_window_open(datetime(2026, 7, 15, 22, 0, tzinfo=timezone))
    assert not _followup_window_open(datetime(2026, 7, 15, 6, 59, tzinfo=timezone))
    print("self-check puro: OK (follow-up window)")

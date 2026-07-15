from __future__ import annotations

import logging
import traceback

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import chat_memory, notifier, observability, service
from .chatwoot import (
    ChatwootError,
    extract_message_event as extract_chatwoot_event,
    parse_chatwoot_payload,
    verify_chatwoot_signature,
    verify_chatwoot_webhook_token,
)
from .config import settings
from .ycloud import (
    YCloudError,
    extract_message_event as extract_ycloud_event,
    parse_ycloud_payload,
    verify_ycloud_signature,
)

app = FastAPI(title="agente-waichatt")
logger = logging.getLogger(__name__)


@app.exception_handler(Exception)
async def _alert_unhandled(request: Request, exc: Exception) -> JSONResponse:
    notifier.notify_error(
        "excepción no manejada en la API",
        detalle=traceback.format_exc(),
        contexto={"method": request.method, "path": request.url.path},
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


@app.on_event("startup")
def startup() -> None:
    if not settings.database_url:
        raise RuntimeError("Falta DATABASE_URL: el agente no puede leer ni persistir.")
    if settings.channel not in (service.YCLOUD_CHANNEL, service.CHATWOOT_CHANNEL):
        raise RuntimeError("CHANNEL debe ser 'ycloud' o 'chatwoot'.")

    required = [("OPENAI_API_KEY", settings.openai_api_key)]
    if settings.channel == service.YCLOUD_CHANNEL:
        required.extend(
            (
                ("YCLOUD_API_KEY", settings.ycloud_api_key),
                ("YCLOUD_WHATSAPP_FROM", settings.ycloud_whatsapp_from),
            )
        )
        webhook_secret = settings.ycloud_webhook_secret
    else:
        required.extend(
            (
                ("CHATWOOT_URL", settings.chatwoot_url),
                ("CHATWOOT_ACCOUNT_ID", settings.chatwoot_account_id),
                ("CHATWOOT_ACCESS_TOKEN", settings.chatwoot_access_token),
            )
        )
        webhook_secret = settings.chatwoot_webhook_secret

    for variable, value in required:
        if not value:
            logger.warning("%s ausente: funcionalidad incompleta.", variable)
    if not webhook_secret:
        if settings.require_webhook_secret:
            raise RuntimeError(
                "REQUIRE_WEBHOOK_SECRET=true pero falta el secret del webhook del canal activo; "
                "abortando arranque inseguro."
            )
        logger.warning("Webhook %s sin secret: acepta cualquier POST.", settings.channel)
    observability.init_tracing()


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "has_database": bool(settings.database_url),
        "has_openai": bool(settings.openai_api_key),
        "channel": settings.channel,
        "has_ycloud": bool(settings.ycloud_api_key and settings.ycloud_whatsapp_from),
        "has_chatwoot": bool(
            settings.chatwoot_url
            and settings.chatwoot_account_id
            and settings.chatwoot_access_token
        ),
        "has_webhook_secret": bool(
            settings.ycloud_webhook_secret
            if settings.channel == service.YCLOUD_CHANNEL
            else settings.chatwoot_webhook_secret
        ),
        "has_langfuse": bool(settings.langfuse_public_key and settings.langfuse_secret_key),
    }


def _require_channel(channel: str) -> None:
    if settings.channel != channel:
        raise HTTPException(status_code=404, detail=f"Canal inactivo: {channel}")


@app.get("/webhooks/ycloud/health")
def webhook_health() -> dict[str, object]:
    return {
        "ok": True,
        "endpoint": "/webhooks/ycloud",
        "channel": settings.channel,
        "active": settings.channel == service.YCLOUD_CHANNEL,
    }


@app.post("/webhooks/ycloud")
async def ycloud_webhook(request: Request) -> dict[str, object]:
    _require_channel(service.YCLOUD_CHANNEL)
    raw_body = await request.body()
    if not verify_ycloud_signature(
        raw_body,
        settings.ycloud_webhook_secret,
        request.headers.get("ycloud-signature"),
        settings.ycloud_webhook_timestamp_tolerance_seconds,
    ):
        raise HTTPException(status_code=401, detail="Firma de webhook inválida")
    try:
        payload = parse_ycloud_payload(raw_body)
        event, reason = extract_ycloud_event(payload)
    except YCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if event is None:
        return {"ok": True, "handled": False, "reason": reason}

    event_key = service.ycloud_event_key(event)
    try:
        is_new, conversation, job_id = await run_in_threadpool(
            service.persist_incoming_event, event_key, event, payload
        )
    except Exception as exc:
        logger.exception("persist_incoming_failed", extra={"event_key": event_key})
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not is_new:
        return {"ok": True, "handled": False, "status": "duplicate", "conversation_id": conversation.id}

    from .tasks.agent_tasks import process_conversation, set_conversation_debounce

    set_conversation_debounce(conversation.id)
    process_conversation.apply_async(
        (str(conversation.id),), queue="agent_messages", countdown=settings.debounce_seconds
    )
    return {
        "ok": True,
        "handled": True,
        "status": "queued",
        "conversation_id": conversation.id,
        "job_id": job_id,
    }


@app.get("/webhooks/chatwoot/health")
def chatwoot_webhook_health() -> dict[str, object]:
    return {
        "ok": True,
        "endpoint": "/webhooks/chatwoot",
        "channel": settings.channel,
        "active": settings.channel == service.CHATWOOT_CHANNEL,
    }


@app.post("/webhooks/chatwoot")
async def chatwoot_webhook(request: Request) -> dict[str, object]:
    _require_channel(service.CHATWOOT_CHANNEL)
    raw_body = await request.body()
    verified = verify_chatwoot_signature(
        raw_body,
        settings.chatwoot_webhook_secret,
        request.headers.get("x-chatwoot-signature"),
        request.headers.get("x-chatwoot-timestamp"),
        settings.chatwoot_webhook_timestamp_tolerance_seconds,
    )
    if not verified:
        verified = verify_chatwoot_webhook_token(
            settings.chatwoot_webhook_secret, request.query_params.get("token")
        )
    if not verified:
        raise HTTPException(status_code=401, detail="Firma de webhook inválida")
    try:
        payload = parse_chatwoot_payload(raw_body)
        event, reason = extract_chatwoot_event(payload)
    except ChatwootError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if event is None:
        return {"ok": True, "handled": False, "reason": reason}

    event_key = service.chatwoot_event_key(
        {key.lower(): value for key, value in request.headers.items()}, event
    )
    try:
        is_new, conversation, job_id = await run_in_threadpool(
            service.persist_incoming_chatwoot_event, event_key, event, payload
        )
    except Exception as exc:
        logger.exception("persist_incoming_failed", extra={"event_key": event_key})
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not is_new:
        return {
            "ok": True,
            "handled": False,
            "status": "duplicate",
            "conversation_id": conversation.id,
        }

    from .tasks.agent_tasks import process_conversation, set_conversation_debounce

    set_conversation_debounce(conversation.id)
    process_conversation.apply_async(
        (str(conversation.id),), queue="agent_messages", countdown=settings.debounce_seconds
    )
    return {
        "ok": True,
        "handled": True,
        "status": "queued",
        "conversation_id": conversation.id,
        "job_id": job_id,
    }


def _admin_auth(x_admin_token: str | None = Header(default=None)) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN no configurado")
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Token admin inválido")


def _conversation_or_404(phone: str) -> chat_memory.Conversation:
    conversation = chat_memory.find_conversation(settings.channel, phone)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    return conversation


@app.post("/admin/conversations/{phone}/pause", dependencies=[Depends(_admin_auth)])
def pause_conversation(phone: str) -> dict[str, object]:
    conversation = _conversation_or_404(phone)
    chat_memory.set_conversation_state(conversation.id, {"bot_apagado": True})
    return {"ok": True, "phone": phone, "bot_apagado": True}


@app.post("/admin/conversations/{phone}/resume", dependencies=[Depends(_admin_auth)])
def resume_conversation(phone: str) -> dict[str, object]:
    conversation = _conversation_or_404(phone)
    chat_memory.set_conversation_state(conversation.id, {"bot_apagado": False})
    return {"ok": True, "phone": phone, "bot_apagado": False}


@app.get("/admin/conversations/{phone}", dependencies=[Depends(_admin_auth)])
def get_conversation(phone: str) -> dict[str, object]:
    conversation = _conversation_or_404(phone)
    lead_phone = service.lead_phone(conversation)
    lead = chat_memory.get_lead(lead_phone)
    return {
        "ok": True,
        "conversation_id": conversation.id,
        "external_conversation_id": phone,
        "phone": lead_phone,
        "state": conversation.state,
        "stage": lead.get("stage") if lead else None,
    }

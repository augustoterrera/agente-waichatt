from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from . import chat_memory, media
from .agent import _PHONE_RE, _digits, _phone_allowed, allowed_phones, run_agent
from .chatwoot import ChatwootMessageEvent, message_attachments as chatwoot_attachments
from .config import settings
from .ycloud import (
    YCloudMessageEvent,
    build_ycloud_client,
    message_attachments as ycloud_attachments,
)

logger = logging.getLogger(__name__)
YCLOUD_CHANNEL = "ycloud"
CHATWOOT_CHANNEL = "chatwoot"


def ycloud_event_key(event: YCloudMessageEvent) -> str:
    return f"ycloud:{event.event_id}"


def chatwoot_event_key(
    headers: dict[str, str | None], event: ChatwootMessageEvent
) -> str:
    delivery_id = headers.get("x-chatwoot-delivery")
    if delivery_id:
        return f"chatwoot:delivery:{delivery_id}"
    return f"chatwoot:message:{event.conversation_id}:{event.message_id}"


def outbox_idempotency_key(
    conversation_id: int | str,
    message_ids: list[int],
    content: str,
    channel: str = YCLOUD_CHANNEL,
) -> str:
    digest = hashlib.sha256(
        json.dumps({"c": str(conversation_id), "m": message_ids, "t": content}, sort_keys=True).encode()
    ).hexdigest()
    return f"{channel}:{conversation_id}:{digest}"


def persist_incoming_event(
    event_key: str, event: YCloudMessageEvent, payload: dict[str, Any]
) -> tuple[bool, chat_memory.Conversation, int | None]:
    is_new = chat_memory.mark_event_received(
        event_key, YCLOUD_CHANNEL, event.conversation_id, event.message_id, payload
    )
    conversation = chat_memory.get_or_create_conversation(
        YCLOUD_CHANNEL,
        event.conversation_id,
        external_contact_id=event.conversation_id,
        account_id=event.account_id,
    )
    if not is_new:
        return False, conversation, None

    if "contact" not in (conversation.state or {}):
        referral = event.referral or {}
        contact = {
            "name": event.customer_name,
            "referral_headline": referral.get("headline"),
            "ctwa_clid": referral.get("ctwa_clid"),
        }
        chat_memory.set_conversation_state(conversation.id, {"contact": contact})
        conversation.state = {**(conversation.state or {}), "contact": contact}

    chat_memory.add_message(
        conversation.id,
        "user",
        event.content,
        external_message_id=event.message_id,
        processing_status="pending",
        raw_payload=payload,
    )
    job_id = chat_memory.enqueue_webhook_job(
        event_key, YCLOUD_CHANNEL, event.conversation_id, event.message_id, payload
    )
    logger.info(
        "ycloud_webhook_queued",
        extra={"event_key": event_key, "conversation_id": conversation.id, "job_id": job_id},
    )
    return True, conversation, job_id


def persist_incoming_chatwoot_event(
    event_key: str, event: ChatwootMessageEvent, payload: dict[str, Any]
) -> tuple[bool, chat_memory.Conversation, int | None]:
    is_new = chat_memory.mark_event_received(
        event_key, CHATWOOT_CHANNEL, event.conversation_id, event.message_id, payload
    )
    conversation = chat_memory.get_or_create_conversation(
        CHATWOOT_CHANNEL,
        event.conversation_id,
        external_contact_id=event.contact_id,
        account_id=event.account_id
        or (str(settings.chatwoot_account_id) if settings.chatwoot_account_id else None),
    )
    if not is_new:
        return False, conversation, None

    previous_contact = (conversation.state or {}).get("contact") or {}
    contact = {
        **previous_contact,
        "name": event.customer_name or previous_contact.get("name"),
        "phone": event.customer_phone or previous_contact.get("phone"),
    }
    if contact != previous_contact:
        chat_memory.set_conversation_state(conversation.id, {"contact": contact})
        conversation.state = {**(conversation.state or {}), "contact": contact}

    chat_memory.add_message(
        conversation.id,
        "user",
        event.content,
        external_message_id=event.message_id,
        processing_status="pending",
        raw_payload=payload,
    )
    job_id = chat_memory.enqueue_webhook_job(
        event_key, CHATWOOT_CHANNEL, event.conversation_id, event.message_id, payload
    )
    logger.info(
        "chatwoot_webhook_queued",
        extra={"event_key": event_key, "conversation_id": conversation.id, "job_id": job_id},
    )
    return True, conversation, job_id


def process_pending_conversation_messages(conversation_id: int) -> list[int]:
    """Procesa un turno y devuelve sus outbox ids en orden: texto y luego adjuntos."""
    conversation = chat_memory.get_conversation(conversation_id)
    pending = chat_memory.pending_messages(conversation_id)
    if not pending:
        _finish(conversation, "completed")
        return []

    message_ids = [int(message["id"]) for message in pending]
    if chat_memory.bot_paused(conversation):
        chat_memory.mark_messages_processed(message_ids)
        _finish(conversation, "skipped")
        return []

    client = (
        build_ycloud_client(settings.ycloud_api_key, settings.ycloud_base_url)
        if conversation.channel == YCLOUD_CHANNEL
        else None
    )
    inbound_id = pending[-1].get("external_message_id")
    if client and inbound_id:
        try:
            client.typing_indicator(str(inbound_id))
        except Exception as exc:
            logger.warning(
                "typing_indicator_failed",
                extra={"conversation_id": conversation_id, "error": str(exc)},
            )

    content, images, transcripts = _collect_inputs(pending, conversation.channel)
    if not content and not images:
        chat_memory.mark_messages_processed(message_ids)
        _finish(conversation, "skipped")
        return []

    if is_reset_command(content):
        chat_memory.reset_conversation_memory(conversation.id, lead_phone(conversation))
        outbox = chat_memory.create_outbox(
            conversation.id,
            conversation.external_conversation_id,
            conversation.channel,
            RESET_REPLY,
            outbox_idempotency_key(
                conversation.external_conversation_id, message_ids, RESET_REPLY, conversation.channel
            ),
        )
        _finish(conversation, "completed")
        logger.info("conversation_reset", extra={"conversation_id": conversation_id})
        return [int(outbox["id"])] if outbox else []

    contact = (conversation.state or {}).get("contact") or {}
    try:
        chat_memory.upsert_lead(
            phone=lead_phone(conversation),
            name=contact.get("name"),
            conversation_id=conversation.id,
        )
    except Exception as exc:
        logger.warning("lead_upsert_failed", extra={"conversation_id": conversation_id, "error": str(exc)})

    history = chat_memory.recent_history(
        conversation_id, settings.history_limit, exclude_ids=set(message_ids)
    )
    answer, media_out = run_agent(
        content,
        history,
        images=images or None,
        contact_context=_contact_context(contact),
    )

    superseding = [
        message
        for message in chat_memory.pending_messages(conversation_id)
        if message["id"] not in set(message_ids)
    ]
    if superseding:
        _finish(conversation, "completed")
        logger.info("ycloud_turn_superseded", extra={"conversation_id": conversation_id})
        return []

    for message_id, transcript in transcripts:
        chat_memory.set_message_content(message_id, transcript)
    chat_memory.add_message(conversation_id, "assistant", answer, processing_status="processed")
    chat_memory.mark_messages_processed(message_ids)

    base_key = outbox_idempotency_key(
        conversation.external_conversation_id, message_ids, answer, conversation.channel
    )
    outbox_ids: list[int] = []
    outbox = chat_memory.create_outbox(
        conversation.id,
        conversation.external_conversation_id,
        conversation.channel,
        answer,
        base_key,
    )
    if outbox:
        outbox_ids.append(int(outbox["id"]))
    for item in media_out:
        media_outbox = chat_memory.create_outbox(
            conversation.id,
            conversation.external_conversation_id,
            conversation.channel,
            "",
            f"{base_key}:media:{item.id}",
            media={"type": item.type, "link": item.url, "caption": None},
        )
        if media_outbox:
            outbox_ids.append(int(media_outbox["id"]))

    _finish(conversation, "completed")
    return outbox_ids


def is_handoff_reply(content: str) -> bool:
    allowed = allowed_phones()
    human = _digits(settings.humano_phone)
    for candidate in _PHONE_RE.findall(content):
        digits = _digits(candidate)
        if len(digits) >= 8 and _phone_allowed(digits, allowed) and digits[-9:] == human[-9:]:
            return True
    return False


def lead_phone(conversation: chat_memory.Conversation) -> str:
    contact = (conversation.state or {}).get("contact") or {}
    return str(contact.get("phone") or conversation.external_conversation_id)


def _finish(conversation: chat_memory.Conversation, job_status: str) -> None:
    chat_memory.update_jobs(conversation.channel, conversation.external_conversation_id, job_status)
    chat_memory.update_events(conversation.channel, conversation.external_conversation_id, "completed")


_RESET_COMMANDS = {"/reset", "/reiniciar", "/borrar", "reiniciar chat", "borrar memoria"}
RESET_REPLY = "Listo, borré nuestra conversación. Escribime lo que quieras y arrancamos de cero."


def is_reset_command(content: str) -> bool:
    """Comando de prueba para el equipo: deja la conversación en blanco desde el chat mismo.
    Solo se habilita con RESET_COMMAND_ENABLED (dev); en prod un lead real podría escribirlo."""
    return settings.reset_command_enabled and content.strip().lower() in _RESET_COMMANDS


def _contact_context(contact: dict[str, Any]) -> str | None:
    parts: list[str] = []
    if contact.get("name"):
        parts.append(f"Nombre: {contact['name']}")
    if contact.get("referral_headline"):
        parts.append(f"Llegó desde un anuncio: {contact['referral_headline']}")
    return "\n".join(parts) or None


def _collect_inputs(
    pending: list[dict], channel: str
) -> tuple[str, list[tuple[bytes, str]], list[tuple[int, str]]]:
    text_parts: list[str] = []
    images: list[tuple[bytes, str]] = []
    audio_transcripts: list[tuple[int, str]] = []
    for message in pending:
        if (message.get("content") or "").strip():
            text_parts.append(message["content"].strip())
        attachment_parser = (
            chatwoot_attachments if channel == CHATWOOT_CHANNEL else ycloud_attachments
        )
        for attachment in attachment_parser(message.get("raw_payload") or {}):
            if attachment["type"] == "audio":
                try:
                    transcript = media.transcribe(
                        media.download(attachment["url"]), extension=attachment.get("extension")
                    )
                except media.MediaError:
                    transcript = ""
                if transcript:
                    text_parts.append(transcript)
                    audio_transcripts.append((int(message["id"]), transcript))
                else:
                    text_parts.append("(el cliente envió un audio que no se pudo entender)")
            elif attachment["type"] == "image":
                try:
                    data = media.download(attachment["url"])
                    images.append((data, media.image_media_type(attachment.get("extension"), data)))
                except media.MediaError:
                    text_parts.append("(el cliente envió una imagen que no se pudo abrir)")
            else:
                text_parts.append(f"(el cliente envió un archivo adjunto: {attachment['type']})")
    content = "\n".join(text_parts).strip()
    if not content and images:
        content = "El cliente envió una o más imágenes. Miralas y ayudalo con lo que muestran."
    return content, images, audio_transcripts


if __name__ == "__main__":
    event = YCloudMessageEvent("evt-1", "msg-1", None, "+5493810000000", None, "hola", None, None)
    assert ycloud_event_key(event) == "ycloud:evt-1"
    chatwoot_event = ChatwootMessageEvent(
        "msg-1", "42", "6", "hola", "8", "Ana", "+5493810000000"
    )
    assert chatwoot_event_key({}, chatwoot_event) == "chatwoot:message:42:msg-1"
    assert outbox_idempotency_key(1, [2, 3], "hola") == outbox_idempotency_key(1, [2, 3], "hola")
    assert is_handoff_reply(f"Hablá con {settings.humano_nombre}: +54 381 681 4079")
    assert not is_handoff_reply("El plan cuesta USD 180 por mes")
    assert _contact_context({"name": "Ana", "referral_headline": "Automatizá tu inmobiliaria"})
    settings.reset_command_enabled = True
    assert is_reset_command("/reset") and is_reset_command("  /Reset  ")
    assert is_reset_command("borrar memoria") and not is_reset_command("quiero borrar memoria")
    settings.reset_command_enabled = False
    assert not is_reset_command("/reset")
    print("self-check puro: OK (service)")

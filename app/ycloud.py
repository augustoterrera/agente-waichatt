from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Transporte WhatsApp vía YCloud (https://docs.ycloud.com).
# - Entrada: webhook whatsapp.inbound_message.received (firmado con HMAC-SHA256).
# - Salida: POST /whatsapp/messages/sendDirectly (texto y media).
# - UX: typingIndicator marca como leído Y muestra "escribiendo..." mientras generamos.


class YCloudError(RuntimeError):
    pass


INBOUND_EVENT = "whatsapp.inbound_message.received"

# Tipos de mensaje con adjunto que sabemos procesar río abajo.
_MEDIA_TYPES = ("image", "audio", "video", "document", "sticker")

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/amr": "amr",
    "video/mp4": "mp4",
}


@dataclass(frozen=True)
class YCloudMessageEvent:
    event_id: str
    message_id: str | None  # id de YCloud (sirve para markAsRead / typingIndicator)
    wamid: str | None
    conversation_id: str  # teléfono del lead (from, E.164)
    account_id: str | None  # nuestro número (to)
    content: str
    customer_name: str | None
    referral: dict[str, Any] | None  # datos del anuncio click-to-WhatsApp (Meta Ads)
    attachments: list[dict[str, Any]] = field(default_factory=list)


# ── Firma del webhook ────────────────────────────────────────────────────────
def verify_ycloud_signature(
    raw_body: bytes,
    secret: str | None,
    signature_header: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> bool:
    """Header `YCloud-Signature: t={unix},s={hmac}`. Firma = HMAC-SHA256(secret, "{t}.{body}")."""
    if not secret:
        return True  # sin secret configurado, no se verifica (avisar en arranque)
    if not signature_header:
        return False
    parts: dict[str, str] = {}
    for chunk in signature_header.split(","):
        key, _, value = chunk.strip().partition("=")
        if key and value:
            parts[key] = value
    timestamp, signature = parts.get("t"), parts.get("s")
    if not timestamp or not signature:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    current_time = time.time() if now is None else now
    if tolerance_seconds > 0 and abs(current_time - signed_at) > tolerance_seconds:
        return False
    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.lower())


# ── Parsing del webhook ──────────────────────────────────────────────────────
def parse_ycloud_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise YCloudError("JSON de webhook inválido") from exc
    if not isinstance(payload, dict):
        raise YCloudError("El payload del webhook debe ser un objeto JSON")
    return payload


def extract_message_event(payload: dict[str, Any]) -> tuple[YCloudMessageEvent | None, str | None]:
    if str(payload.get("type") or "") != INBOUND_EVENT:
        return None, "ignored_event"
    message = payload.get("whatsappInboundMessage")
    if not isinstance(message, dict):
        return None, "missing_inbound_message"
    sender = str(message.get("from") or "").strip()
    if not sender:
        return None, "missing_sender"

    content, attachments = _content_and_attachments(message)
    if not content and not attachments:
        return None, "ignored_empty_message"

    profile = message.get("customerProfile") if isinstance(message.get("customerProfile"), dict) else {}
    referral = message.get("referral") if isinstance(message.get("referral"), dict) else None
    return (
        YCloudMessageEvent(
            event_id=str(payload.get("id") or ""),
            message_id=str(message.get("id")) if message.get("id") else None,
            wamid=str(message.get("wamid")) if message.get("wamid") else None,
            conversation_id=_normalize_phone(sender),
            account_id=_normalize_phone(str(message.get("to"))) if message.get("to") else None,
            content=content,
            customer_name=(str(profile.get("name")).strip() or None) if profile.get("name") else None,
            referral=referral,
            attachments=attachments,
        ),
        None,
    )


def _content_and_attachments(message: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    msg_type = str(message.get("type") or "text")
    if msg_type == "text":
        body = message.get("text") if isinstance(message.get("text"), dict) else {}
        return str(body.get("body") or "").strip(), []
    if msg_type in _MEDIA_TYPES:
        media = message.get(msg_type) if isinstance(message.get(msg_type), dict) else {}
        caption = str(media.get("caption") or "").strip()
        url = media.get("link")
        if not url:
            return caption or f"(el cliente envió un adjunto de tipo {msg_type} que no se pudo leer)", []
        mime = str(media.get("mime_type") or "")
        return caption, [
            {
                "type": "image" if msg_type == "sticker" else msg_type,
                "url": str(url),
                "mime_type": mime,
                "extension": _EXTENSIONS.get(mime.split(";")[0].strip()),
            }
        ]
    if msg_type == "location":
        return "(el cliente envió una ubicación)", []
    return f"(el cliente envió un mensaje de tipo {msg_type} que no se puede procesar)", []


def message_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Adjuntos a partir del raw_payload persistido del webhook (para el worker)."""
    message = payload.get("whatsappInboundMessage")
    if not isinstance(message, dict):
        return []
    _, attachments = _content_and_attachments(message)
    return attachments


def _normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"+{digits}" if digits else phone


# ── Cliente saliente ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class YCloudClient:
    api_key: str
    base_url: str = "https://api.ycloud.com/v2"
    timeout_seconds: int = 30

    def send_text(self, from_number: str, to: str, body: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/whatsapp/messages/sendDirectly",
            {"from": from_number, "to": to, "type": "text", "text": {"body": body, "preview_url": True}},
        )

    def send_media(
        self, from_number: str, to: str, media_type: str, link: str, caption: str | None = None
    ) -> dict[str, Any]:
        if media_type not in ("image", "video", "document"):
            raise YCloudError(f"Tipo de media no soportado para envío: {media_type}")
        media: dict[str, Any] = {"link": link}
        if caption:
            media[  # WhatsApp muestra el caption debajo de la imagen/video
                "caption"
            ] = caption
        return self._request(
            "POST",
            "/whatsapp/messages/sendDirectly",
            {"from": from_number, "to": to, "type": media_type, media_type: media},
        )

    def typing_indicator(self, inbound_message_id: str) -> None:
        """Marca el mensaje como leído (tildes azules) y muestra 'escribiendo...' hasta que
        respondamos o pasen 25s. Best-effort: un fallo acá no debe frenar el turno."""
        self._request("POST", f"/whatsapp/inboundMessages/{inbound_message_id}/typingIndicator", None)

    def mark_as_read(self, inbound_message_id: str) -> None:
        self._request("POST", f"/whatsapp/inboundMessages/{inbound_message_id}/markAsRead", None)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        request = Request(
            self.base_url.rstrip("/") + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b"",
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise YCloudError(f"YCloud {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise YCloudError(f"No se pudo conectar a YCloud: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise YCloudError("YCloud devolvió JSON inválido") from exc


def build_ycloud_client(api_key: str | None, base_url: str) -> YCloudClient | None:
    if not api_key:
        return None
    return YCloudClient(api_key=api_key, base_url=base_url)


if __name__ == "__main__":
    # Self-check puro (sin red): firma y parsing.
    secret = "whsec_test"
    body = b'{"hello": "world"}'
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_ycloud_signature(body, secret, f"t={ts},s={sig}", 300)
    assert not verify_ycloud_signature(body, secret, f"t={ts},s={'0' * 64}", 300)
    assert not verify_ycloud_signature(body, secret, f"t=123,s={sig}", 300)  # timestamp viejo
    assert verify_ycloud_signature(body, None, None, 300)  # sin secret → no verifica

    payload = {
        "id": "evt_123",
        "type": INBOUND_EVENT,
        "whatsappInboundMessage": {
            "id": "63f8",
            "wamid": "wamid.X",
            "from": "+54 9 381 555-0000",
            "to": "+5493810000000",
            "type": "text",
            "text": {"body": "hola, qué es waichatt?"},
            "customerProfile": {"name": "Caro"},
            "referral": {"source_type": "ad", "headline": "Automatizá tu inmobiliaria", "ctwa_clid": "abc"},
        },
    }
    event, reason = extract_message_event(payload)
    assert event and reason is None
    assert event.conversation_id == "+5493815550000"  # teléfono normalizado
    assert event.customer_name == "Caro" and event.referral["headline"] == "Automatizá tu inmobiliaria"

    img = {
        "id": "evt_124",
        "type": INBOUND_EVENT,
        "whatsappInboundMessage": {
            "id": "63f9",
            "from": "+5493815550000",
            "type": "image",
            "image": {"link": "https://api.ycloud.com/v2/whatsapp/media/download/x", "mime_type": "image/jpeg", "caption": "mirá"},
        },
    }
    event2, _ = extract_message_event(img)
    assert event2 and event2.attachments[0]["type"] == "image" and event2.content == "mirá"
    assert message_attachments(img) == event2.attachments

    ignored, why = extract_message_event({"id": "evt_1", "type": "whatsapp.message.updated"})
    assert ignored is None and why == "ignored_event"
    print("self-check puro: OK (ycloud)")

from __future__ import annotations

import io
import logging
import urllib.request

from .config import settings

logger = logging.getLogger(__name__)

_IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


class MediaError(RuntimeError):
    pass


def download(url: str, timeout: int = 30) -> bytes:
    """Descarga un adjunto entrante de Chatwoot o YCloud. Los links privados de media de
    YCloud (api.ycloud.com/v2/whatsapp/media/download/...) requieren la API key."""
    headers = {"User-Agent": "agente-waichatt/1.0"}
    if settings.ycloud_api_key and url.startswith(settings.ycloud_base_url.rstrip("/")):
        headers["X-API-Key"] = settings.ycloud_api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        raise MediaError(f"No se pudo descargar el adjunto: {exc}") from exc


def image_media_type(extension: str | None, data: bytes) -> str:
    if extension and extension.lower().lstrip(".") in _IMAGE_TYPES:
        return _IMAGE_TYPES[extension.lower().lstrip(".")]
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # default razonable para fotos de WhatsApp


def transcribe(audio: bytes, *, extension: str | None = None) -> str:
    """Transcribe un audio a texto con OpenAI. Devuelve '' si falla (no rompe el turno)."""
    if not settings.openai_api_key:
        return ""
    from openai import OpenAI

    name = f"audio.{(extension or 'ogg').lstrip('.')}"  # la extensión le dice el formato al modelo
    file = io.BytesIO(audio)
    file.name = name
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        result = client.audio.transcriptions.create(model=settings.transcription_model, file=file)
        return (result.text or "").strip()
    except Exception as exc:
        logger.warning("transcribe_failed", extra={"error": str(exc)})
        return ""

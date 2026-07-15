"""Alertas Telegram con stdlib; nunca lanza y es no-op sin configuración."""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger("notifier")
_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX = 4000


def enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(text: str, parse_mode: str = "HTML") -> bool:
    token, chat = settings.telegram_bot_token, settings.telegram_chat_id
    if not (token and chat):
        log.debug("telegram no configurado; alerta omitida")
        return False
    try:
        data = json.dumps(
            {"chat_id": chat, "text": text[:_MAX], "parse_mode": parse_mode, "disable_web_page_preview": True}
        ).encode("utf-8")
        request = urllib.request.Request(
            _API.format(token=token), data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        log.warning("no pude enviar alerta a Telegram: %s", exc)
        return False


def notify_error(titulo: str, detalle: object = None, contexto: dict | None = None) -> bool:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"🔴 <b>{_esc(settings.alert_project)}</b> — {_esc(titulo)}", f"<i>{timestamp}</i>"]
    for key, value in (contexto or {}).items():
        lines.append(f"• <b>{_esc(key)}:</b> {_esc(value)}")
    if detalle:
        lines += ["", f"<pre>{_esc(str(detalle)[:1500])}</pre>"]
    return send("\n".join(lines))


def notify_handoff(phone: str, name: str | None, last_message: str) -> bool:
    """Avisa al equipo comercial cuando un lead pidió hablar con una persona."""
    lines = [
        f"🤝 <b>{_esc(settings.alert_project)}</b> — lead pide humano",
        f"• <b>teléfono:</b> {_esc(phone)}",
    ]
    if name:
        lines.append(f"• <b>nombre:</b> {_esc(name)}")
    lines += ["", f"<pre>{_esc(last_message[:1500])}</pre>"]
    return send("\n".join(lines))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level="INFO")
    message = sys.argv[1] if len(sys.argv) > 1 else "prueba de alerta"
    if not enabled():
        print("Telegram NO configurado (faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        raise SystemExit(2)
    ok = notify_error("test de notificador", detalle=message, contexto={"origen": "manual"})
    print("enviado OK" if ok else "falló el envío (ver logs)")
    raise SystemExit(0 if ok else 1)

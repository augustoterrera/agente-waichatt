from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ChatwootError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatwootMessageEvent:
    message_id: str | None
    conversation_id: str
    account_id: str | None
    content: str
    contact_id: str | None
    customer_name: str | None
    customer_phone: str | None
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ChatwootClient:
    base_url: str
    access_token: str
    timeout_seconds: int = 30

    def create_outgoing_message(
        self, account_id: int | str, conversation_id: int | str, content: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages",
            {
                "content": content,
                "message_type": "outgoing",
                "private": False,
                "content_type": "text",
            },
        )

    def get_conversation_labels(
        self, account_id: int | str, conversation_id: int | str
    ) -> list[str]:
        data = self._request(
            "GET", f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels"
        )
        return list((data or {}).get("payload") or [])

    def set_conversation_labels(
        self, account_id: int | str, conversation_id: int | str, labels: list[str]
    ) -> list[str]:
        data = self._request(
            "POST",
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/labels",
            {"labels": labels},
        )
        return list((data or {}).get("payload") or [])

    def assign_conversation(
        self, account_id: int | str, conversation_id: int | str, assignee_id: int | str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/accounts/{account_id}/conversations/{conversation_id}/assignments",
            {"assignee_id": assignee_id},
        )

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = Request(
            self.base_url.rstrip("/") + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "api_access_token": self.access_token},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise ChatwootError(f"Chatwoot {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ChatwootError(f"No se pudo conectar a Chatwoot: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ChatwootError("Chatwoot devolvió JSON inválido") from exc


def build_chatwoot_client(
    base_url: str | None, access_token: str | None
) -> ChatwootClient | None:
    if not base_url or not access_token:
        return None
    return ChatwootClient(base_url=base_url, access_token=access_token)


def verify_chatwoot_signature(
    raw_body: bytes,
    secret: str | None,
    signature: str | None,
    timestamp: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> bool:
    if not secret:
        return True
    if not signature or not timestamp:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    current_time = time.time() if now is None else now
    if tolerance_seconds > 0 and abs(current_time - signed_at) > tolerance_seconds:
        return False
    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_chatwoot_webhook_token(secret: str | None, token: str | None) -> bool:
    if not secret:
        return True
    return bool(token) and hmac.compare_digest(secret, token)


def parse_chatwoot_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatwootError("JSON de webhook inválido") from exc
    if not isinstance(payload, dict):
        raise ChatwootError("El payload del webhook debe ser un objeto JSON")
    return payload


def extract_message_event(
    payload: dict[str, Any],
) -> tuple[ChatwootMessageEvent | None, str | None]:
    if str(payload.get("event") or "") != "message_created":
        return None, "ignored_event"
    if payload.get("message_type") not in ("incoming", 0):
        return None, "ignored_non_incoming_message"
    if bool(payload.get("private")):
        return None, "ignored_private_message"

    content = str(payload.get("content") or "").strip()
    attachments = message_attachments(payload)
    if not content and not attachments:
        return None, "ignored_empty_message"

    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    conversation_id = conversation.get("id") or payload.get("conversation_id")
    if conversation_id is None:
        return None, "missing_conversation_id"

    message_id = payload.get("id")
    account_id = account.get("id") or payload.get("account_id")
    contact_id = sender.get("id") or contact.get("id")
    name = str(sender.get("name") or contact.get("name") or "").strip() or None
    phone = str(sender.get("phone_number") or contact.get("phone_number") or "").strip() or None
    return (
        ChatwootMessageEvent(
            message_id=str(message_id) if message_id is not None else None,
            conversation_id=str(conversation_id),
            account_id=str(account_id) if account_id is not None else None,
            content=content,
            contact_id=str(contact_id) if contact_id is not None else None,
            customer_name=name,
            customer_phone=phone,
            attachments=attachments,
        ),
        None,
    )


def message_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("attachments")
    if not isinstance(raw, list):
        return []
    attachments: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("data_url") or item.get("thumb_url")
        media_type = item.get("file_type")
        if url and media_type:
            attachments.append(
                {
                    "type": str(media_type),
                    "url": str(url),
                    "extension": item.get("extension"),
                }
            )
    return attachments


if __name__ == "__main__":
    secret = "chatwoot_test"
    body = b'{"event":"message_created"}'
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert verify_chatwoot_signature(body, secret, signature, timestamp, 300)
    assert verify_chatwoot_webhook_token(secret, secret)

    event, reason = extract_message_event(
        {
            "event": "message_created",
            "id": 12,
            "message_type": "incoming",
            "content": "hola",
            "conversation": {"id": 34},
            "account": {"id": 6},
            "sender": {"id": 8, "name": "Ana", "phone_number": "+5493810000000"},
        }
    )
    assert event and reason is None
    assert event.conversation_id == "34" and event.customer_phone == "+5493810000000"
    print("self-check puro: OK (chatwoot)")

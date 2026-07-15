from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import db
from .db import Json
from .models import AgentMessage

# Store de memoria conversacional sobre Postgres directo (psycopg). Cada operación atómica
# (dedup de eventos, locks, claims de outbox) es UNA sentencia SQL con ON CONFLICT / WHERE
# condicional: una llamada = una transacción. Sin maquinaria de slots: el diseño es
# conversacional, el estado lo llevan el historial y el jsonb `state` de la conversación.


@dataclass
class Conversation:
    id: int
    channel: str
    external_conversation_id: str
    account_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


# ── Entrada (webhook) ──────────────────────────────────────────────────────
def mark_event_received(
    event_key: str, channel: str, external_conversation_id: str, external_message_id: str | None, raw_payload: dict
) -> bool:
    """True si es nuevo; False si ya se procesó (dedup por event_key)."""
    count = db.execute(
        """
        insert into chat_processed_events (event_key, channel, external_conversation_id, external_message_id, raw_payload, status)
        values (%s, %s, %s, %s, %s, 'received')
        on conflict (event_key) do nothing
        """,
        (event_key, channel, external_conversation_id, external_message_id, Json(raw_payload or {})),
    )
    return count > 0


def update_event_status(event_key: str, status: str, error: str | None = None) -> None:
    db.execute(
        "update chat_processed_events set status = %s, error = %s where event_key = %s",
        (status, error, event_key),
    )


def get_or_create_conversation(
    channel: str, external_conversation_id: str, external_contact_id: str | None = None, account_id: str | None = None
) -> Conversation:
    row = db.fetch_one(
        """
        insert into chat_conversations (channel, external_conversation_id, external_contact_id, account_id, last_seen_at)
        values (%s, %s, %s, %s, now())
        on conflict (channel, external_conversation_id) do update set
          external_contact_id = coalesce(excluded.external_contact_id, chat_conversations.external_contact_id),
          account_id = coalesce(excluded.account_id, chat_conversations.account_id),
          last_seen_at = now()
        returning *
        """,
        (channel, external_conversation_id, external_contact_id, account_id),
    )
    return _conversation(row)


def enqueue_webhook_job(
    event_key: str, channel: str, external_conversation_id: str, external_message_id: str | None, raw_payload: dict
) -> int:
    rows = db.execute_returning(
        """
        insert into chat_webhook_jobs (event_key, channel, external_conversation_id, external_message_id, raw_payload, status)
        values (%s, %s, %s, %s, %s, 'queued')
        on conflict (event_key) do nothing
        returning id
        """,
        (event_key, channel, external_conversation_id, external_message_id, Json(raw_payload or {})),
    )
    if rows:
        return int(rows[0]["id"])
    return int(db.fetch_val("select id from chat_webhook_jobs where event_key = %s", (event_key,)))


def update_jobs(
    channel: str, external_conversation_id: str, status: str, *, error: str | None = None, worker_id: str | None = None
) -> None:
    """Transición de estado de los jobs activos de una conversación (queued/processing/retry)."""
    sets = ["status = %(status)s"]
    params: dict[str, Any] = {"status": status, "channel": channel, "ext": external_conversation_id}
    if error is not None:
        sets.append("error = %(error)s")
        params["error"] = error[:500]
    if status == "processing":
        sets.append("started_at = now()")
        sets.append("locked_at = now()")
        sets.append("attempts = attempts + 1")
        if worker_id:
            sets.append("worker_id = %(worker)s")
            params["worker"] = worker_id
    if status in ("completed", "failed", "skipped"):
        sets.append("finished_at = now()")
        sets.append("completed_at = now()")
    db.execute(
        f"""
        update chat_webhook_jobs set {", ".join(sets)}
        where channel = %(channel)s and external_conversation_id = %(ext)s
          and status in ('queued', 'processing', 'retry')
        """,
        params,
    )


def update_events(channel: str, external_conversation_id: str, status: str, *, error: str | None = None) -> None:
    db.execute(
        """
        update chat_processed_events set status = %s, error = %s
        where channel = %s and external_conversation_id = %s and status in ('received', 'processing')
        """,
        (status, (error or "")[:500] or None, channel, external_conversation_id),
    )


def get_conversation(conversation_id: int) -> Conversation:
    row = db.fetch_one("select * from chat_conversations where id = %s", (conversation_id,))
    if not row:
        raise ValueError(f"conversación {conversation_id} no existe")
    return _conversation(row)


def find_conversation(channel: str, external_conversation_id: str) -> Conversation | None:
    row = db.fetch_one(
        "select * from chat_conversations where channel = %s and external_conversation_id = %s",
        (channel, external_conversation_id),
    )
    return _conversation(row) if row else None


def set_conversation_state(conversation_id: int, patch: dict[str, Any]) -> None:
    """Merge superficial del jsonb state (state || patch). Acá viven bot_apagado, el contexto
    del contacto (nombre, referral del anuncio) y la última clasificación del lead."""
    db.execute("update chat_conversations set state = state || %s::jsonb where id = %s", (Json(patch), conversation_id))


def bot_paused(conversation: Conversation) -> bool:
    return bool((conversation.state or {}).get("bot_apagado"))


# ── Mensajes ────────────────────────────────────────────────────────────────
def add_message(
    conversation_id: int,
    role: str,
    content: str,
    *,
    external_message_id: str | None = None,
    processing_status: str = "processed",
    raw_payload: dict | None = None,
) -> None:
    db.execute(
        """
        insert into chat_messages (conversation_id, role, content, external_message_id, processing_status, raw_payload)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (conversation_id, external_message_id, role) do nothing
        """,
        (conversation_id, role, content, external_message_id, processing_status, Json(raw_payload or {})),
    )


def pending_messages(conversation_id: int, limit: int = 50) -> list[dict]:
    return db.fetch_all(
        """
        select * from chat_messages
        where conversation_id = %s and processing_status = 'pending' and role = 'user'
        order by created_at asc limit %s
        """,
        (conversation_id, limit),
    )


def set_message_content(message_id: int, content: str) -> None:
    """Reemplaza el contenido de un mensaje (ej. guardar la transcripción de un audio)."""
    db.execute("update chat_messages set content = %s where id = %s", (content, message_id))


def mark_messages_processed(message_ids: list[int]) -> None:
    if not message_ids:
        return
    db.execute("update chat_messages set processing_status = 'processed' where id = any(%s)", (message_ids,))


def recent_history(conversation_id: int, limit: int = 16, exclude_ids: set[int] | None = None) -> list[AgentMessage]:
    exclude = exclude_ids or set()
    rows = db.fetch_all(
        """
        select id, role, content from chat_messages
        where conversation_id = %s and role in ('user', 'assistant')
        order by created_at desc limit %s
        """,
        (conversation_id, limit + len(exclude)),
    )
    rows.reverse()
    return [AgentMessage(role=r["role"], content=r["content"]) for r in rows if r["id"] not in exclude][-limit:]


# ── Outbox ──────────────────────────────────────────────────────────────────
def create_outbox(
    conversation_id: int,
    external_conversation_id: str,
    channel: str,
    content: str,
    idempotency_key: str,
    *,
    media: dict | None = None,
) -> dict | None:
    """media (opcional): {"type": "image"|"video"|"document", "link": url, "caption": str|None}.
    idempotency_key único: si ya existe (reintento del turno), devolvemos el existente en vez
    de duplicar el envío."""
    rows = db.execute_returning(
        """
        insert into chat_outbox_messages (conversation_id, external_conversation_id, channel, content, idempotency_key, media)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (idempotency_key) do nothing
        returning *
        """,
        (conversation_id, external_conversation_id, channel, content, idempotency_key, Json(media) if media else None),
    )
    if rows:
        return rows[0]
    return db.fetch_one("select * from chat_outbox_messages where idempotency_key = %s", (idempotency_key,))


def get_outbox(outbox_id: int) -> dict | None:
    return db.fetch_one("select * from chat_outbox_messages where id = %s", (outbox_id,))


def mark_outbox_processing(outbox_id: int) -> bool:
    """Reclama el outbox para envío. False si otro worker ya lo tomó (claim atómico)."""
    count = db.execute(
        "update chat_outbox_messages set status = 'processing' where id = %s and status in ('pending', 'retry')",
        (outbox_id,),
    )
    return count > 0


def pending_outbox(channel: str, limit: int = 100) -> list[dict]:
    return db.fetch_all(
        """
        select * from chat_outbox_messages
        where channel = %s and status in ('pending', 'retry')
        order by created_at asc limit %s
        """,
        (channel, limit),
    )


def mark_outbox_sent(outbox_id: int, raw_payload: dict | None = None) -> None:
    db.execute(
        "update chat_outbox_messages set status = 'sent', sent_at = now(), raw_payload = %s where id = %s",
        (Json(raw_payload or {}), outbox_id),
    )


def mark_outbox_retry_or_failed(outbox_id: int, error: str) -> str:
    row = db.fetch_one(
        """
        update chat_outbox_messages
        set attempts = attempts + 1,
            status = case when attempts + 1 >= max_attempts then 'failed' else 'retry' end,
            error = %s
        where id = %s
        returning status
        """,
        (error[:500], outbox_id),
    )
    return row["status"] if row else "failed"


def mark_outbox_canceled(outbox_id: int, reason: str) -> None:
    db.execute(
        "update chat_outbox_messages set status = 'canceled', error = %s where id = %s and status in ('pending', 'retry')",
        (reason[:500], outbox_id),
    )


# ── Locks (segundo lock, en la DB: serializa aunque haya varios workers) ────
def acquire_lock(channel: str, external_conversation_id: str, lock_seconds: int = 60) -> bool:
    count = db.execute(
        """
        update chat_conversations
        set locked_until = now() + make_interval(secs => %s)
        where channel = %s and external_conversation_id = %s
          and (locked_until is null or locked_until < now())
        """,
        (max(lock_seconds, 1), channel, external_conversation_id),
    )
    return count > 0


def release_lock(channel: str, external_conversation_id: str) -> None:
    db.execute(
        "update chat_conversations set locked_until = null where channel = %s and external_conversation_id = %s",
        (channel, external_conversation_id),
    )


def cleanup_expired_locks() -> int:
    return db.execute("update chat_conversations set locked_until = null where locked_until < now()")


# ── Sweepers (beat) ──────────────────────────────────────────────────────────
def requeue_stale_jobs(channel: str, stale_minutes: int = 15, limit: int = 100) -> list[int]:
    rows = db.execute_returning(
        """
        with stale_jobs as (
          select j.id, c.id as conversation_id
          from chat_webhook_jobs j
          join chat_conversations c
            on c.channel = j.channel and c.external_conversation_id = j.external_conversation_id
          where j.status = 'processing'
            and j.channel = %s
            and coalesce(j.locked_at, j.started_at, j.created_at) < now() - make_interval(mins => %s)
            and j.attempts < j.max_attempts
          order by coalesce(j.locked_at, j.started_at, j.created_at)
          limit %s
        )
        update chat_webhook_jobs j
        set status = 'retry', run_at = now(), locked_at = null, worker_id = null, error = null
        from stale_jobs s where j.id = s.id
        returning s.conversation_id
        """,
        (channel, max(stale_minutes, 1), max(limit, 1)),
    )
    return sorted({int(r["conversation_id"]) for r in rows})


def due_job_conversation_ids(channel: str, limit: int = 100) -> list[int]:
    rows = db.fetch_all(
        """
        select distinct c.id
        from chat_webhook_jobs j
        join chat_conversations c
          on c.channel = j.channel and c.external_conversation_id = j.external_conversation_id
        where j.channel = %s and j.status in ('queued', 'retry')
          and j.run_at <= now() and j.attempts < j.max_attempts
        limit %s
        """,
        (channel, max(limit, 1)),
    )
    return [int(r["id"]) for r in rows]


def due_followup_conversation_ids(
    channel: str, delay_hours: int = 12, max_age_hours: int = 24, limit: int = 100
) -> list[int]:
    rows = db.fetch_all(
        """
        select c.id
        from chat_conversations c
        join lateral (
          select m.role, m.created_at
          from chat_messages m
          where m.conversation_id = c.id
          order by m.created_at desc, m.id desc
          limit 1
        ) last_message on true
        where c.channel = %s
          and coalesce(c.state #>> '{lead,followup_eligible}', 'false') = 'true'
          and coalesce(c.state #>> '{lead,stage}', '') not in ('registrado', 'derivado')
          and coalesce((c.state ->> 'bot_apagado')::boolean, false) = false
          and last_message.role = 'assistant'
          and last_message.created_at <= now() - make_interval(hours => %s)
          and last_message.created_at > now() - make_interval(hours => %s)
          and exists (
            select 1 from chat_messages m where m.conversation_id = c.id and m.role = 'user'
          )
          and not exists (
            select 1 from chat_outbox_messages o
            where o.conversation_id = c.id
              and o.idempotency_key = concat('followup:', c.channel, ':', c.id)
          )
        order by last_message.created_at
        limit %s
        """,
        (channel, max(delay_hours, 1), max(max_age_hours, delay_hours + 1), max(limit, 1)),
    )
    return [int(row["id"]) for row in rows]


def followup_still_due(conversation_id: int, delay_hours: int = 12, max_age_hours: int = 24) -> bool:
    return bool(
        db.fetch_val(
            """
            select exists (
              select 1
              from chat_conversations c
              join lateral (
                select m.role, m.created_at
                from chat_messages m
                where m.conversation_id = c.id
                order by m.created_at desc, m.id desc
                limit 1
              ) last_message on true
              where c.id = %s
                and coalesce(c.state #>> '{lead,followup_eligible}', 'false') = 'true'
                and coalesce(c.state #>> '{lead,stage}', '') not in ('registrado', 'derivado')
                and coalesce((c.state ->> 'bot_apagado')::boolean, false) = false
                and last_message.role = 'assistant'
                and last_message.created_at <= now() - make_interval(hours => %s)
                and last_message.created_at > now() - make_interval(hours => %s)
            )
            """,
            (conversation_id, max(delay_hours, 1), max(max_age_hours, delay_hours + 1)),
        )
    )


# ── Leads (CRM mínimo para el equipo comercial) ─────────────────────────────
def upsert_lead(
    phone: str,
    *,
    name: str | None = None,
    inmobiliaria: str | None = None,
    es_dueno: bool | None = None,
    consultas: str | None = None,
    equipos: str | None = None,
    stage: str | None = None,
    flags: list[str] | None = None,
    conversation_id: int | None = None,
) -> None:
    """Upsert por teléfono. Los datos de calificación solo se completan (coalesce: lo ya
    conocido no se pisa con null); la etapa REEMPLAZA; las flags se ACUMULAN (sticky)."""
    db.execute(
        """
        insert into crm_leads (phone, name, inmobiliaria, es_dueno, consultas, equipos, stage, flags, conversation_id)
        values (%(phone)s, %(name)s, %(inmo)s, %(dueno)s, %(consultas)s, %(equipos)s,
                coalesce(%(stage)s, 'curioso'), %(flags)s, %(conv)s)
        on conflict (phone) do update set
          name = coalesce(excluded.name, crm_leads.name),
          inmobiliaria = coalesce(excluded.inmobiliaria, crm_leads.inmobiliaria),
          es_dueno = coalesce(excluded.es_dueno, crm_leads.es_dueno),
          consultas = coalesce(excluded.consultas, crm_leads.consultas),
          equipos = coalesce(excluded.equipos, crm_leads.equipos),
          stage = coalesce(%(stage)s, crm_leads.stage),
          flags = (
            select coalesce(jsonb_agg(distinct x), '[]'::jsonb)
            from jsonb_array_elements_text(crm_leads.flags || excluded.flags) as t(x)
          ),
          conversation_id = coalesce(excluded.conversation_id, crm_leads.conversation_id)
        """,
        {
            "phone": phone,
            "name": name,
            "inmo": inmobiliaria,
            "dueno": es_dueno,
            "consultas": consultas,
            "equipos": equipos,
            "stage": stage,
            "flags": Json(flags or []),
            "conv": conversation_id,
        },
    )


def get_lead(phone: str) -> dict | None:
    return db.fetch_one("select * from crm_leads where phone = %s", (phone,))


def _conversation(row: dict[str, Any]) -> Conversation:
    return Conversation(
        id=int(row["id"]),
        channel=row["channel"],
        external_conversation_id=str(row["external_conversation_id"]),
        account_id=row.get("account_id"),
        state=row.get("state") or {},
    )


if __name__ == "__main__":
    # Self-check vivo contra Postgres: crea una conversación de prueba, ejercita el flujo
    # y limpia al final. Channel propio para no tocar data real.
    from .config import settings

    if not settings.database_url:
        print("self-check: SALTEADO (falta DATABASE_URL)")
        raise SystemExit

    ch, ext = "selftest", "conv-selfcheck-1"
    db.execute("delete from chat_processed_events where channel = %s", (ch,))
    db.execute("delete from chat_conversations where channel = %s", (ch,))
    db.execute("delete from crm_leads where phone = %s", ("+540000000000",))

    conv = get_or_create_conversation(ch, ext, account_id="test")
    assert conv.id and conv.channel == ch
    conv2 = get_or_create_conversation(ch, ext)  # idempotente
    assert conv2.id == conv.id

    assert mark_event_received("evt-1", ch, ext, "m1", {"x": 1}) is True
    assert mark_event_received("evt-1", ch, ext, "m1", {"x": 1}) is False  # dedup
    job_id = enqueue_webhook_job("evt-1", ch, ext, "m1", {"x": 1})
    assert enqueue_webhook_job("evt-1", ch, ext, "m1", {"x": 1}) == job_id  # idempotente

    set_conversation_state(conv.id, {"bot_apagado": True})
    assert bot_paused(get_conversation(conv.id)) is True
    set_conversation_state(conv.id, {"bot_apagado": False})
    assert bot_paused(get_conversation(conv.id)) is False

    add_message(conv.id, "user", "hola, qué es waichatt?", external_message_id="m1", processing_status="pending")
    pend = pending_messages(conv.id)
    assert len(pend) == 1 and pend[0]["content"] == "hola, qué es waichatt?"

    assert acquire_lock(ch, ext, 30) is True
    assert acquire_lock(ch, ext, 30) is False  # ya bloqueado
    release_lock(ch, ext)
    assert acquire_lock(ch, ext, 30) is True
    release_lock(ch, ext)

    add_message(conv.id, "assistant", "Hola! Waichatt es un ecosistema de agentes de IA...")
    mark_messages_processed([m["id"] for m in pend])
    assert pending_messages(conv.id) == []
    hist = recent_history(conv.id)
    assert [m.role for m in hist] == ["user", "assistant"]

    set_conversation_state(
        conv.id,
        {"lead": {"stage": "calificando", "flags": [], "followup_eligible": True}},
    )
    db.execute(
        "update chat_messages set created_at = now() - interval '14 hours' where conversation_id = %s and role = 'user'",
        (conv.id,),
    )
    db.execute(
        "update chat_messages set created_at = now() - interval '13 hours' where conversation_id = %s and role = 'assistant'",
        (conv.id,),
    )
    assert conv.id in due_followup_conversation_ids(ch)
    assert followup_still_due(conv.id)
    add_message(conv.id, "user", "sigo acá", external_message_id="m2")
    assert conv.id not in due_followup_conversation_ids(ch)
    assert not followup_still_due(conv.id)

    ob = create_outbox(conv.id, ext, ch, "Hola!", "idem-1")
    assert ob and ob["status"] == "pending"
    ob_dup = create_outbox(conv.id, ext, ch, "Hola!", "idem-1")  # idempotente
    assert ob_dup and ob_dup["id"] == ob["id"]
    assert len(pending_outbox(ch)) == 1
    assert mark_outbox_processing(ob["id"]) is True
    assert mark_outbox_processing(ob["id"]) is False  # claim atómico
    mark_outbox_sent(ob["id"])
    assert pending_outbox(ch) == []

    upsert_lead("+540000000000", name="Test", stage="calificando", flags=["pidio_demo"], conversation_id=conv.id)
    upsert_lead("+540000000000", inmobiliaria="Inmo SA", flags=["pidio_humano"])
    lead = db.fetch_one("select * from crm_leads where phone = %s", ("+540000000000",))
    assert lead["name"] == "Test" and lead["inmobiliaria"] == "Inmo SA"
    assert set(lead["flags"]) == {"pidio_demo", "pidio_humano"} and lead["stage"] == "calificando"

    # limpieza
    db.execute("delete from crm_leads where phone = %s", ("+540000000000",))
    db.execute("delete from chat_processed_events where channel = %s", (ch,))
    db.execute("delete from chat_conversations where channel = %s", (ch,))
    print("self-check vivo: OK (chat_memory Postgres end-to-end)")

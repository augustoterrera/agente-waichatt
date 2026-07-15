from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Postgres directo (el del VPS, con database propia para este agente).
    # Toda la memoria conversacional (historial, dedup, jobs, outbox, leads) vive acá.
    # Ej: postgresql://waichatt:pass@host:5432/agente_waichatt
    database_url: str | None = None
    db_pool_max: int = 10

    # OpenAI: agente + clasificador + transcripción de audios.
    openai_api_key: str | None = None
    # gpt-5-mini: barato y sigue bien las reglas condicionales del prompt (no inventar,
    # no descuentos). Es de razonamiento (~10s por respuesta con effort low). Si necesitás
    # más rapidez: gpt-4.1 (~3s, más caro). Cambiable por env AGENT_MODEL.
    agent_model: str = "gpt-5-mini"
    # Esfuerzo de razonamiento (solo modelos gpt-5*/o*). "low" equilibra precisión y latencia.
    agent_reasoning_effort: str | None = "low"
    # Clasificador de leads: tarea simple → modelo barato y rápido, sin razonar.
    classifier_model: str = "gpt-4.1-mini"
    # Transcripción de audios (WhatsApp → texto).
    transcription_model: str = "gpt-4o-mini-transcribe"

    # Langfuse: gestión del prompt (los no-devs lo editan desde la UI) + trazas.
    # Sin keys → todo cae al prompt local (app/prompts/waichatt.md) y sin tracing: el bot
    # funciona igual. El prompt se busca por nombre + etiqueta, con caché y fallback local.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://us.cloud.langfuse.com"
    langfuse_prompt_name: str = "agente-vendedor-waichatt"
    langfuse_prompt_label: str = "production"
    langfuse_prompt_cache_seconds: int = 60

    # YCloud (transporte WhatsApp). El webhook se configura en la consola de YCloud
    # apuntando a /webhooks/ycloud y suscripto a whatsapp.inbound_message.received.
    ycloud_api_key: str | None = None
    ycloud_base_url: str = "https://api.ycloud.com/v2"
    # Número de WhatsApp Business desde el que responde el bot (E.164, ej +549381...).
    ycloud_whatsapp_from: str | None = None
    # Secret del webhook endpoint (lo da YCloud al crearlo). Firma: HMAC-SHA256 de
    # "{timestamp}.{body}" en el header YCloud-Signature ("t=...,s=...").
    ycloud_webhook_secret: str | None = None
    ycloud_webhook_timestamp_tolerance_seconds: int = 300

    # Chatwoot se usa como transporte de desarrollo. Producción sigue usando YCloud;
    # docker-compose.override.yml / docker-compose.prod.yml fijan CHANNEL por entorno.
    chatwoot_url: str | None = None
    chatwoot_account_id: int | None = None
    chatwoot_assignee_id: int | None = None
    chatwoot_access_token: str | None = None
    chatwoot_webhook_secret: str | None = None
    chatwoot_webhook_timestamp_tolerance_seconds: int = 300

    # En prod poner True: sin webhook secret, el arranque aborta en vez de aceptar POSTs sin firmar.
    require_webhook_secret: bool = False

    # Links y teléfonos OFICIALES (única fuente para el guard anti-alucinación: cualquier
    # URL o teléfono que el modelo emita y no esté acá —o en el catálogo de media— se filtra
    # antes de enviarse). Mantener en sync con la sección 2 del prompt.
    registro_url: str = "https://www.waichatt.app/register"
    crm_url: str = "https://www.waichatt.app"
    agenda_url: str = "https://calendar.app.google/TZdc9qpD6w1T4s4K9"
    sitio_url: str = "https://waichatt.com/"
    demo_phone: str = "+5493815225112"
    humano_phone: str = "+543816814079"
    humano_nombre: str = "Julian"

    # Historial que ve el agente por turno.
    history_limit: int = 16
    # Transporte activo y namespace de conversaciones en las tablas chat_*.
    # Valores válidos: "ycloud" o "chatwoot".
    channel: str = "ycloud"

    # Celery / Redis (el compose levanta su propio Redis y setea estas por environment).
    redis_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_timezone: str = "America/Argentina/Tucuman"
    debounce_seconds: int = 5
    debounce_retry_seconds: int = 3
    lock_seconds: int = 60
    job_max_retries: int = 5
    outbox_max_retries: int = 5
    stale_processing_minutes: int = 15

    # Endpoints /admin (pausar/reanudar el bot por conversación). No se rutean públicamente
    # en prod (Traefik solo expone /webhooks); el token protege el acceso interno igual.
    admin_token: str | None = None

    # Alertas por Telegram (opcional). Sin token/chat → no-op. Avisa en fallos finales de
    # tasks, excepciones no manejadas de la API y cuando un lead pide hablar con un humano.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    alert_project: str = "agente-waichatt"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()

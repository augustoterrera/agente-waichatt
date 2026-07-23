from __future__ import annotations

import logging

from .config import settings

logger = logging.getLogger(__name__)

_initialized = False
_host_version: str | None = None


def _host_ingests_otel() -> bool:
    """True si la instancia de Langfuse acepta spans OTel (v3+). Ante la duda, True: no
    queremos perder trazas por un chequeo que falló."""
    global _host_version
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{settings.langfuse_host}/api/public/health", timeout=5) as resp:
            _host_version = json.load(resp).get("version")
        return int(str(_host_version).split(".")[0]) >= 3
    except Exception:
        return True


def init_tracing() -> None:
    """Trazas de cada corrida del agente hacia Langfuse (modelo, tokens, costo, latencia).

    El SDK v3 de Langfuse registra un TracerProvider de OpenTelemetry al instanciarse;
    pydantic-ai emite spans OTel con Agent.instrument_all(). Guarded: si algo falla acá,
    el bot sigue funcionando sin trazas (nunca es camino crítico).
    Se llama en el startup de la API y en el init de cada proceso worker de Celery.
    """
    global _initialized
    if _initialized:
        return
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.info("Langfuse sin configurar: sin trazas (el bot funciona igual).")
        _initialized = True
        return
    if not _host_ingests_otel():
        # Langfuse v2 no expone /api/public/otel: instrumentar igual solo produce un
        # "Failed to export span batch 404" por cada batch. Se habilita solo al actualizar
        # la instancia a v3, sin tocar código.
        logger.warning(
            "Langfuse %s no soporta ingesta OTel (hace falta v3): sigo sin trazas.",
            _host_version or "?",
        )
        _initialized = True
        return
    try:
        from .prompt_manager import get_langfuse

        get_langfuse()  # registra el TracerProvider global

        from pydantic_ai import Agent

        Agent.instrument_all()
        logger.info("Trazas hacia Langfuse habilitadas (host=%s).", settings.langfuse_host)
    except Exception as exc:
        logger.warning("no pude inicializar el tracing de Langfuse: %s", exc)
    _initialized = True

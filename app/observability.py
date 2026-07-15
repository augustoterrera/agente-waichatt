from __future__ import annotations

import logging

from .config import settings

logger = logging.getLogger(__name__)

_initialized = False


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
    try:
        from .prompt_manager import get_langfuse

        get_langfuse()  # registra el TracerProvider global

        from pydantic_ai import Agent

        Agent.instrument_all()
        logger.info("Trazas hacia Langfuse habilitadas (host=%s).", settings.langfuse_host)
    except Exception as exc:
        logger.warning("no pude inicializar el tracing de Langfuse: %s", exc)
    _initialized = True

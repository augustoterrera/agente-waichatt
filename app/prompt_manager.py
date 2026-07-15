from __future__ import annotations

import logging
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

# El prompt es EDITABLE POR NO-DEVS desde la UI de Langfuse: versionado, playground y
# rollback moviendo la etiqueta (default "production"). El bot lo busca por nombre+etiqueta
# con caché corta, y si Langfuse no responde (o no está configurado) cae al archivo local.
# Seed inicial: python -m scripts.seed_langfuse
#
# Separación deliberada: el COMPORTAMIENTO vive en el prompt (editable); la SEGURIDAD
# (guard de links/teléfonos, pausa, debounce) vive en código. Romper el prompt puede hacer
# que el bot venda peor, nunca que alucine un link o un teléfono.

PROMPT_FILE = Path(__file__).parent / "prompts" / "waichatt.md"

_langfuse = None


def local_prompt() -> str:
    # Se relee por turno: editás waichatt.md y el bot cambia sin reiniciar (fallback/dev).
    return PROMPT_FILE.read_text(encoding="utf-8")


def langfuse_enabled() -> bool:
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def get_langfuse():
    """Cliente Langfuse singleton (además registra el TracerProvider OTel para las trazas)."""
    global _langfuse
    if _langfuse is None:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _langfuse


def load_system_prompt(label: str | None = None) -> str:
    if not langfuse_enabled():
        return local_prompt()
    try:
        prompt = get_langfuse().get_prompt(
            settings.langfuse_prompt_name,
            label=label or settings.langfuse_prompt_label,
            type="text",
            cache_ttl_seconds=settings.langfuse_prompt_cache_seconds,
            fallback=local_prompt(),
        )
        text = prompt.prompt
        if isinstance(text, str) and text.strip():
            return text
        logger.warning("prompt de Langfuse vacío o no-texto; uso el local")
    except Exception as exc:  # Langfuse caído ≠ bot caído
        logger.warning("no pude leer el prompt de Langfuse (%s); uso el local", exc)
    return local_prompt()

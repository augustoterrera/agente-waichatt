from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Catálogo CERRADO de imágenes/videos que el bot puede enviar. El modelo decide CUÁNDO
# mostrar algo, pero solo puede elegir QUÉ de esta lista — nunca inventa una URL.
# Formato de app/media_catalog.json (lista de objetos):
#   [
#     {
#       "id": "demo-inbox",
#       "type": "video",                                  // image | video | document
#       "url": "https://waichatt.app/media/demo-inbox.mp4",  // URL pública (WhatsApp la descarga)
#       "descripcion": "Video corto del inbox de WhatsApp en equipo. Usar cuando preguntan cómo ven las conversaciones los vendedores."
#     }
#   ]
# Se relee en cada turno: agregás una entrada y el bot la puede usar sin reiniciar nada.

CATALOG_FILE = Path(__file__).parent / "media_catalog.json"


@dataclass(frozen=True)
class MediaItem:
    id: str
    type: str  # image | video | document
    url: str
    descripcion: str


def load_catalog() -> list[MediaItem]:
    try:
        raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        logger.warning("media_catalog.json inválido, catálogo vacío: %s", exc)
        return []
    items: list[MediaItem] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id") or "").strip()
        item_type = str(entry.get("type") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not item_id or not url or item_type not in ("image", "video", "document"):
            logger.warning("entrada de catálogo inválida (id/type/url): %s", entry)
            continue
        items.append(MediaItem(id=item_id, type=item_type, url=url, descripcion=str(entry.get("descripcion") or "")))
    return items


def get(item_id: str) -> MediaItem | None:
    return next((item for item in load_catalog() if item.id == item_id), None)


def catalog_urls() -> set[str]:
    """URLs del catálogo: se suman al set permitido del guard de links."""
    return {item.url for item in load_catalog()}

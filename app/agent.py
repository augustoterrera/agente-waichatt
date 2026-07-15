from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic_ai import Agent, BinaryContent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings
from .media_catalog import MediaItem, load_catalog
from .models import AgentMessage
from .prompt_manager import load_system_prompt


class AgentError(RuntimeError):
    pass


@dataclass
class Deps:
    # Media del catálogo que el modelo pidió enviar este turno (vía tool enviar_media).
    media_out: list[MediaItem] = field(default_factory=list)


def build_agent(prompt_label: str | None = None) -> Agent[Deps, str]:
    if not settings.openai_api_key:
        raise AgentError("Falta OPENAI_API_KEY para el agente.")
    model = OpenAIChatModel(settings.agent_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    # reasoning_effort solo aplica a familias de razonamiento (gpt-5*, o*); en gpt-4.1 el
    # parámetro no va, así que se omite y el cambio de modelo por env sigue funcionando.
    model_settings = None
    effort = settings.agent_reasoning_effort
    if effort and (settings.agent_model.startswith("gpt-5") or settings.agent_model.startswith("o")):
        model_settings = OpenAIChatModelSettings(openai_reasoning_effort=effort)
    agent = Agent(
        model=model,
        deps_type=Deps,
        system_prompt=load_system_prompt(prompt_label),
        model_settings=model_settings,
    )

    # La tool solo existe si el catálogo tiene entradas: sin catálogo, el modelo ni se entera
    # de que "enviar media" es una posibilidad (no puede pedir algo que no existe).
    catalog = load_catalog()
    if catalog:
        agent.tool(_make_media_tool(catalog))
    return agent


def _make_media_tool(catalog: list[MediaItem]):
    by_id = {item.id: item for item in catalog}

    def enviar_media(ctx: RunContext[Deps], media_id: str) -> str:
        item = by_id.get(media_id)
        if item is None:
            return f"media_id inválido. Disponibles: {', '.join(sorted(by_id))}"
        if all(existing.id != item.id for existing in ctx.deps.media_out):
            ctx.deps.media_out.append(item)
        return f"OK: {item.id} se enviará junto con tu respuesta. No pegues la URL en el texto."

    lines = "\n".join(f"- {item.id} ({item.type}): {item.descripcion}" for item in catalog)
    enviar_media.__doc__ = (
        "Adjunta a tu respuesta una imagen/video/documento OFICIAL del catálogo de Waichatt. "
        "Usala cuando mostrar el sistema ayude a la venta (una demo vale más que un párrafo); "
        "máximo 1 o 2 por turno, nunca repitas uno ya enviado en la conversación. "
        "Elegí el media_id EXACTO de esta lista:\n" + lines
    )
    return enviar_media


def run_agent(
    message: str,
    history: list[AgentMessage] | None = None,
    images: list[tuple[bytes, str]] | None = None,
    contact_context: str | None = None,
    prompt_label: str | None = None,
) -> tuple[str, list[MediaItem]]:
    """Corre un turno. `images`: lista de (bytes, media_type) para que el modelo las vea.
    `contact_context`: datos del contacto/anuncio (nombre de perfil, referral de Meta Ads).
    Devuelve (texto ya pasado por los guards, media del catálogo a enviar)."""
    agent = build_agent(prompt_label)
    deps = Deps()
    text = _build_input(message, history or [], contact_context)
    prompt: object = text
    if images:
        prompt = [text, *[BinaryContent(data=data, media_type=mt) for data, mt in images]]
    try:
        result = agent.run_sync(prompt, deps=deps)
    except Exception as exc:
        raise AgentError(f"Falló la corrida del agente: {exc}") from exc
    return guard_output(result.output), list(deps.media_out)


def _build_input(message: str, history: list[AgentMessage], contact_context: str | None) -> str:
    lines: list[str] = []
    if contact_context:
        lines += ["Contexto del lead (dato del sistema, usalo con naturalidad, no lo repitas literal):", contact_context, ""]
    if history:
        lines.append("Historial reciente:")
        lines += [f"{m.role}: {m.content}" for m in history[-8:]]
        lines.append("")
    if lines:
        lines.append(f"Mensaje actual del lead: {message}")
        return "\n".join(lines)
    return message


# ── Guards anti-alucinación (red de seguridad post-modelo) ───────────────────
# El prompt ya prohíbe inventar; esto garantiza que NINGÚN link ni teléfono que no sea
# oficial salga por WhatsApp, sin importar qué diga el prompt (que es editable por no-devs).

_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
# Teléfonos: 8+ dígitos, con o sin +, admitiendo separadores comunes. No matchea precios
# ("USD 180"), años ni horarios (quedan cortos de dígitos).
_PHONE_RE = re.compile(r"\+?\d(?:[\s().\-]?\d){7,}")


def _norm_url(url: str) -> str:
    url = url.strip().rstrip(".,;:!?)").rstrip("/")
    for prefix in ("https://", "http://"):
        if url.lower().startswith(prefix):
            url = url[len(prefix):]
            break
    if url.lower().startswith("www."):
        url = url[4:]
    return url.lower()


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def allowed_links() -> set[str]:
    from .media_catalog import catalog_urls

    fixed = {
        settings.registro_url,
        settings.crm_url,
        settings.agenda_url,
        settings.sitio_url,
        settings.instagram_url,
    }
    return {_norm_url(u) for u in fixed | catalog_urls()}


def allowed_phones() -> set[str]:
    return {_digits(settings.demo_phone), _digits(settings.humano_phone)}


def _phone_allowed(candidate_digits: str, allowed: set[str]) -> bool:
    # Compara por sufijo (últimos 9 dígitos): "+54 9 381 522-5112" y "381 522 5112"
    # son el mismo número aunque difieran prefijos de país/celular.
    tail = candidate_digits[-9:]
    return any(phone[-9:] == tail for phone in allowed)


def guard_output(answer: str) -> str:
    """Elimina bloques con links o teléfonos que no estén en la lista oficial (config +
    catálogo de media). Es LA red de seguridad: el prompt es editable por no-devs, esto no."""
    links = allowed_links()
    phones = allowed_phones()
    kept: list[str] = []
    for block in answer.split("\n\n"):
        urls = [_norm_url(u) for u in _URL_RE.findall(block)]
        if any(u not in links for u in urls):
            continue
        phone_candidates = [_digits(p) for p in _PHONE_RE.findall(block)]
        if any(len(d) >= 8 and not _phone_allowed(d, phones) for d in phone_candidates):
            continue
        kept.append(block)
    result = "\n\n".join(kept).strip()
    # Si el guard vació la respuesta entera, devolvemos una salida segura en vez de silencio.
    return result or "Ese dato no te lo puedo compartir por acá, pero lo vemos en la llamada 1 a 1. ¿Te ayudo con otra consulta sobre Waichatt?"


if __name__ == "__main__":
    # Guard puro (sin red) siempre.
    ok_link = settings.registro_url
    g = guard_output(f"Registrate acá:\n\n🔗 {ok_link}\n\n🔗 https://waichatt-falso.com/register")
    assert "waichatt.app/register" in g and "falso" not in g, g

    # El link de registro sin esquema (como está escrito en el prompt) también pasa.
    g = guard_output("Registrate en www.waichatt.app/register y arrancás los 5 días gratis.")
    assert "register" in g, g

    # El CRM y la web institucional son destinos oficiales distintos.
    g = guard_output(f"Entrá al CRM en {settings.crm_url} o conocé Waichatt en {settings.sitio_url}")
    assert "waichatt.app" in g and "waichatt.com" in g, g

    g = guard_output(f"Seguinos en Instagram: {settings.instagram_url}")
    assert "instagram.com/waichatt" in g, g

    # Teléfonos oficiales pasan; inventados no.
    g = guard_output(f"Probá la demo: {settings.demo_phone}\n\nO llamá al +54 9 11 5555-0000 (soporte)")
    assert settings.demo_phone in g and "5555" not in g, g
    g = guard_output("Hablá con Julian: +54 381 681 4079")
    assert "681 4079" in g, g

    # Precios y cantidades no disparan el guard.
    g = guard_output("El Plan Profesional sale USD 180 por mes e incluye 5 días gratis.")
    assert "180" in g, g

    # Respuesta totalmente bloqueada → salida segura, nunca vacío.
    g = guard_output("Llamá al 011 4444 8888")
    assert g and "4444" not in g, g

    assert _phone_allowed(_digits("381 522 5112"), allowed_phones())
    assert not _phone_allowed(_digits("11 5555 0000"), allowed_phones())
    print("guard puro: OK")

    if settings.openai_api_key:
        print("\n--- 'hola, qué es waichatt?' ---")
        answer, media = run_agent("hola, qué es waichatt?")
        print(answer)
        print("\n--- 'me hacés un descuento?' ---")
        answer, _ = run_agent("me hacés un descuento?", history=[AgentMessage(role="user", content="cuánto sale?"), AgentMessage(role="assistant", content="El Plan Profesional cuesta USD 180 por mes.")])
        print(answer)
        print("\nself-check vivo: OK (revisar a ojo las respuestas)")
    else:
        print("self-check vivo: SALTEADO (falta OPENAI_API_KEY)")

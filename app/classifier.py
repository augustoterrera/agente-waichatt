from __future__ import annotations

import logging
from typing import Literal, get_args

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings
from .models import AgentMessage

logger = logging.getLogger(__name__)

Stage = Literal["curioso", "calificando", "invitado_registro", "registrado", "derivado"]
Flag = Literal["sin_decision", "pidio_descuento", "pidio_humano", "pidio_demo", "fuera_de_alcance"]

STAGE_LABELS: set[str] = set(get_args(Stage))
FLAG_LABELS: set[str] = set(get_args(Flag))
DEFAULT_STAGE: Stage = "curioso"


class LeadClassification(BaseModel):
    stage: Stage
    flags: list[Flag] = Field(default_factory=list)
    followup_eligible: bool = False
    nombre: str | None = None
    inmobiliaria: str | None = None
    es_dueno: bool | None = None
    consultas: str | None = None
    equipos: str | None = None


CLASSIFIER_PROMPT = """\
Sos un clasificador de conversaciones comerciales de Waichatt, un ecosistema de agentes de IA
para inmobiliarias. Analizá la conversación COMPLETA y devolvé la etapa, todas las flags con
evidencia y los datos de calificación mencionados. No inventes datos ni expliques la salida.

ETAPA (exactamente una, ante la duda elegí la más baja):
- curioso: recién llegó, saludó o hizo una consulta suelta.
- calificando: está respondiendo o ya respondió preguntas sobre rol, volumen o equipos.
- invitado_registro: el asistente ya le compartió el link de registro o lo invitó explícitamente.
- registrado: el lead afirma que ya se registró o creó su cuenta.
- derivado: el asistente ya le dio el contacto humano de Julian.

FLAGS (todas las que correspondan):
- sin_decision: es agente/vendedor y no decide sobre herramientas de la inmobiliaria.
- pidio_descuento: pidió descuento, promoción, rebaja o precio especial.
- pidio_humano: pidió hablar con una persona o con el equipo comercial.
- pidio_demo: pidió probar o ver al agente funcionando.
- fuera_de_alcance: pidió una función o asunto no confirmado en la base de conocimiento.

DATOS (None cuando no aparezcan): nombre, inmobiliaria, si es dueño/decisor, volumen de
consultas y equipos/áreas mencionados. Conservá consultas y equipos como texto breve.

FOLLOWUP_ELIGIBLE:
- true solamente si la conversación comercial quedó abierta, el último mensaje es del asistente
  y está esperando que el lead responda, confirme o realice el siguiente paso.
- false si hubo una despedida o agradecimiento final sin nada pendiente, pidió no recibir más
  mensajes, dijo que no le interesa, ya se registró, fue derivado a una persona, acordaron
  retomar en otro momento o el último mensaje no requiere respuesta.
"""


def classify(history: list[AgentMessage]) -> LeadClassification:
    """Clasifica la conversación; cualquier falla cae silenciosamente al estado inicial."""
    if not settings.openai_api_key or not history:
        return LeadClassification(stage=DEFAULT_STAGE)
    model = OpenAIChatModel(settings.classifier_model, provider=OpenAIProvider(api_key=settings.openai_api_key))
    agent: Agent[None, LeadClassification] = Agent(
        model=model, output_type=LeadClassification, system_prompt=CLASSIFIER_PROMPT
    )
    conversation = "\n".join(f"{message.role}: {message.content}" for message in history)
    try:
        result = agent.run_sync(f"Conversación:\n{conversation}").output
        result.followup_eligible = (
            result.followup_eligible
            and history[-1].role == "assistant"
            and result.stage not in ("registrado", "derivado")
        )
        return result
    except Exception as exc:
        logger.warning("classify_failed", extra={"error": str(exc)})
        return LeadClassification(stage=DEFAULT_STAGE)


if __name__ == "__main__":
    M = AgentMessage
    cases: list[tuple[str | None, tuple[str, ...], bool | None, list[M]]] = [
        ("curioso", (), False, [M(role="user", content="hola, cuánto sale?")]),
        ("calificando", (), False, [M(role="user", content="soy dueño y recibimos unas 50 consultas por día")]),
        ("invitado_registro", (), None, [M(role="assistant", content="Registrate en www.waichatt.app/register")]),
        ("registrado", (), False, [M(role="user", content="listo, ya me registré")]),
        ("derivado", (), False, [M(role="assistant", content="Hablá con Julian al +54 381 681 4079")]),
        (None, ("sin_decision",), False, [M(role="user", content="soy vendedor, no soy el dueño ni decido")]),
        (None, ("pidio_descuento",), False, [M(role="user", content="me hacés un descuento?")]),
        (None, ("pidio_humano",), False, [M(role="user", content="quiero hablar con una persona")]),
        (None, ("pidio_demo",), False, [M(role="user", content="puedo ver al bot funcionando?")]),
        (None, (), True, [M(role="assistant", content="¿Querés que te pase el link para probarlo?")]),
        (
            None,
            (),
            False,
            [
                M(role="user", content="Nos hablamos la semana que viene. Buen viaje"),
                M(role="assistant", content="¡Gracias! Que tengas un gran viaje y nos hablamos pronto."),
            ],
        ),
    ]
    if not settings.openai_api_key:
        print("self-check: SALTEADO (falta OPENAI_API_KEY)")
    else:
        passed = 0
        for expected_stage, expected_flags, expected_followup, history in cases:
            result = classify(history)
            ok = (
                (expected_stage is None or result.stage == expected_stage)
                and set(expected_flags) <= set(result.flags)
                and (expected_followup is None or result.followup_eligible is expected_followup)
            )
            passed += int(ok)
            print(
                f"  {'✓' if ok else '✗'} esperado=({expected_stage},{expected_flags},{expected_followup}) "
                f"got=({result.stage},{result.flags},{result.followup_eligible})"
            )
        print(f"clasificador: {passed}/{len(cases)}")

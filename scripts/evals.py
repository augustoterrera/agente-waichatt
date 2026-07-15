from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass

from pydantic import BaseModel


class Verdict(BaseModel):
    passed: bool
    reason: str


@dataclass(frozen=True)
class Scenario:
    name: str
    message: str
    history: tuple[tuple[str, str], ...] = ()
    must_include: tuple[str, ...] = ()
    must_not_include: tuple[str, ...] = ()
    judge: str | None = None
    max_chars: int | None = None


SCENARIOS = (
    Scenario(
        "precio",
        "cuánto sale waichatt?",
        must_include=(r"180",),
        judge="No ofrece descuentos ni promociones.",
        max_chars=300,
    ),
    Scenario(
        "descuento",
        "me hacés un descuento?",
        judge="Rechaza amablemente; solo menciona los 5 días gratis y no inventa promociones.",
    ),
    Scenario(
        "redes en desarrollo",
        "puedo publicar mis propiedades en instagram desde waichatt?",
        judge="Dice que está en desarrollo, no lo ofrece como disponible y no da una fecha.",
    ),
    Scenario(
        "agente sin decisión",
        "soy vendedor en una inmobiliaria, no soy el dueño",
        judge="No lo presiona a registrarse y ofrece pasarle la información al dueño.",
    ),
    Scenario(
        "agendar sin registro",
        "quiero agendar la llamada ya",
        must_include=(r"waichatt\.app/register",),
        judge="Insiste en registrarse primero y no da el link de Calendly o Google Calendar.",
    ),
    Scenario(
        "humano",
        "quiero hablar con una persona",
        must_include=(r"681\s*.?40\s*79|3816814079",),
    ),
    Scenario(
        "feature inventada",
        "waichatt firma contratos digitales?",
        judge="No afirma que exista y dice que lo confirma en la llamada 1 a 1.",
    ),
    Scenario(
        "demo",
        "puedo ver al bot funcionando?",
        must_include=(r"5[\s-]*22[\s-]*5112",),
    ),
    Scenario(
        "web institucional",
        "¿cuál es la web de Waichatt para conocer más?",
        must_include=(r"https://waichatt\.com/?",),
    ),
    Scenario(
        "cuatro sucursales",
        "Tenemos 4 sucursales y queremos una cuenta con número propio para cada una, ¿cuánto sale?",
        must_include=(r"160",),
        must_not_include=(r"USD\s*120|USD\s*80",),
        judge="Indica USD 160 por mes por cuenta y no revela precios de otros tramos.",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evalúa el prompt de ventas")
    parser.add_argument("--label", default="staging")
    args = parser.parse_args()
    os.environ["LANGFUSE_PROMPT_LABEL"] = args.label

    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from app.agent import run_agent
    from app.config import settings
    from app.models import AgentMessage

    if not settings.openai_api_key:
        print("SALTEADO: falta OPENAI_API_KEY")
        return 0

    judge_agent: Agent[None, Verdict] = Agent(
        model=OpenAIChatModel(
            settings.classifier_model, provider=OpenAIProvider(api_key=settings.openai_api_key)
        ),
        output_type=Verdict,
        system_prompt=(
            "Evaluás respuestas de un agente comercial. Aplicá solamente el criterio indicado. "
            "No penalices estilo ni detalles ajenos al criterio."
        ),
    )
    results: list[tuple[str, bool, str]] = []
    for scenario in SCENARIOS:
        try:
            history = [AgentMessage(role=role, content=content) for role, content in scenario.history]
            answer, _ = run_agent(scenario.message, history=history, prompt_label=args.label)
            errors = [
                f"falta /{pattern}/"
                for pattern in scenario.must_include
                if not re.search(pattern, answer, re.IGNORECASE)
            ]
            errors += [
                f"incluye /{pattern}/"
                for pattern in scenario.must_not_include
                if re.search(pattern, answer, re.IGNORECASE)
            ]
            if scenario.max_chars is not None and len(answer) > scenario.max_chars:
                errors.append(f"tiene {len(answer)} caracteres; máximo {scenario.max_chars}")
            if scenario.judge:
                verdict = judge_agent.run_sync(
                    f"Criterio: {scenario.judge}\n\nRespuesta:\n{answer}"
                ).output
                if not verdict.passed:
                    errors.append(verdict.reason)
            results.append((scenario.name, not errors, "; ".join(errors) or "OK"))
        except Exception as exc:
            results.append((scenario.name, False, str(exc)))

    width = max(len(name) for name, _, _ in results)
    print(f"{'ESCENARIO':<{width}}  RESULTADO  DETALLE")
    for name, passed, detail in results:
        print(f"{name:<{width}}  {'✓' if passed else '✗':^9}  {detail}")
    return 0 if all(passed for _, passed, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse

from app.config import settings
from app.prompt_manager import get_langfuse, local_prompt


def main() -> int:
    parser = argparse.ArgumentParser(description="Sube el prompt local a Langfuse")
    parser.add_argument("--label", default="production")
    args = parser.parse_args()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        parser.error("faltan LANGFUSE_PUBLIC_KEY y/o LANGFUSE_SECRET_KEY")
    prompt = get_langfuse().create_prompt(
        name=settings.langfuse_prompt_name,
        type="text",
        prompt=local_prompt(),
        labels=[args.label],
    )
    print(f"Prompt {settings.langfuse_prompt_name!r} creado con label {args.label!r} (version={prompt.version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

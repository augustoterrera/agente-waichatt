from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AgentMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

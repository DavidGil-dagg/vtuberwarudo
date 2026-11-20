"""Nombres por turno: el del personaje y el del usuario llegan desde la web con cada mensaje.

El agente se construye una sola vez; los nombres viajan en el *contexto del turno*
(``agent.astream(..., context=TurnContext(...))``) y ``PersonaPromptMiddleware`` los pone
al principio del prompt de sistema en cada llamada al modelo. No se guardan en el hilo,
así que cambiarlos no altera el historial de las conversaciones existentes.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage

from .config import Config

MAX_NAME_CHARS = 40
_SPACES = re.compile(r"\s+")


def clean_name(value: Any, max_chars: int = MAX_NAME_CHARS) -> str:
    """Nombre en una sola línea, sin espacios de más y con longitud acotada ("" si no hay)."""
    if not isinstance(value, str):
        return ""
    return _SPACES.sub(" ", value).strip()[:max_chars].strip()


@dataclass(frozen=True)
class TurnContext:
    """Contexto de un turno de chat (``Runtime.context`` en los middlewares)."""

    character: str
    user: str = ""

    @classmethod
    def from_message(cls, cfg: Config, msg: dict) -> TurnContext:
        """A partir del mensaje ``{"type": "user", "character": ..., "user_name": ...}`` de la web."""
        return cls(
            character=clean_name(msg.get("character")) or cfg.persona.name,
            user=clean_name(msg.get("user_name")),
        )


def character_name(cfg: Config, context: Any) -> str:
    """Nombre del personaje del turno; ``config.toml [persona] name`` si no viene ninguno."""
    return clean_name(getattr(context, "character", None)) or cfg.persona.name


def user_name(context: Any) -> str:
    return clean_name(getattr(context, "user", None))


def identity_prompt(character: str, user: str = "") -> str:
    lines = [f"Eres {character}."]
    if user:
        lines.append(f"El usuario con el que hablas se llama {user}; puedes llamarle por su nombre.")
    return "\n".join(lines)


def _prepend(system: SystemMessage | None, text: str) -> SystemMessage:
    if system is None:
        return SystemMessage(content=text)
    content = system.content
    if isinstance(content, str):
        return SystemMessage(content=f"{text}\n{content}" if content else text)
    return SystemMessage(content=[{"type": "text", "text": text}, *content])


class PersonaPromptMiddleware(AgentMiddleware):
    """Antepone "Eres <personaje>. El usuario se llama <usuario>." al prompt de sistema."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def _with_identity(self, request: ModelRequest) -> ModelRequest:
        ctx = request.runtime.context if request.runtime else None
        text = identity_prompt(character_name(self.cfg, ctx), user_name(ctx))
        return request.override(system_message=_prepend(request.system_message, text))

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return handler(self._with_identity(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]
    ) -> Any:
        return await handler(self._with_identity(request))

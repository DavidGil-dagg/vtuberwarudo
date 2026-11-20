"""Construcción del Deep Agent del personaje."""

from __future__ import annotations

import logging

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.profiles import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from langchain_openai import ChatOpenAI

from .avatar import Avatar
from .config import Config
from .decision import DecisionClient
from .persona import PersonaPromptMiddleware, TurnContext
from .reaction import ReactionMiddleware
from .tools import build_tools

log = logging.getLogger(__name__)

MEMORY_PATH = "/memories/AGENTS.md"
MEMORY_SEED = """# Memoria sobre el usuario

- Nombre: (desconocido)
- Gustos: (desconocido)
- Otros datos: (ninguno)
"""

# Herramientas de deepagents que sobran para un personaje de chat con un modelo local de 12B.
# Se conservan read_file y edit_file para que pueda actualizar su memoria.
_EXCLUDED_TOOLS = frozenset({"ls", "write_file", "glob", "grep", "execute", "delete", "write_todos", "task"})


def build_chat_model(cfg: Config) -> ChatOpenAI:
    extra = {}
    if cfg.lmstudio.reasoning_effort:
        # LM Studio acepta reasoning_effort="none" para desactivar el thinking de Gemma 4
        extra["reasoning_effort"] = cfg.lmstudio.reasoning_effort
    return ChatOpenAI(
        base_url=cfg.lmstudio.base_url,
        api_key="lm-studio",  # LM Studio no valida la clave
        model=cfg.lmstudio.chat_model,
        temperature=cfg.lmstudio.temperature,
        max_tokens=cfg.lmstudio.max_tokens,
        timeout=cfg.lmstudio.timeout_s,
        streaming=True,
        use_responses_api=False,
        **extra,
    )


def _register_lean_profile(model: ChatOpenAI) -> str:
    """Registra un perfil de harness para este modelo: sin subagente general ni tools de más."""
    from deepagents._models import get_model_identifier, get_model_provider

    key = f"{get_model_provider(model) or 'openai'}:{get_model_identifier(model)}"
    register_harness_profile(
        key,
        HarnessProfile(
            excluded_tools=_EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    return key


def persona_prompt(cfg: Config, with_tools: bool) -> str:
    p = cfg.persona
    rules = [
        "Respondes siempre en español, de forma natural y breve (1 a 3 frases), como en un chat en directo.",
        "No describas tus acciones entre asteriscos ni con emojis de acciones: tu cuerpo y tu cara se animan solos.",
        "Nunca digas que eres un modelo de lenguaje o una IA a menos que te lo pregunten directamente.",
        f"Tu memoria sobre el usuario está en el archivo {MEMORY_PATH} (ya la ves arriba). Cuando el usuario te cuente "
        "algo importante y duradero (su nombre, gustos, planes), actualízala con edit_file sin mencionarlo: "
        "reemplaza una sola línea cada vez (p.ej. old_string='- Nombre: (desconocido)', new_string='- Nombre: Ana'), "
        "sin saltos de línea escapados.",
    ]
    if with_tools:
        rules.append(
            "Si el usuario te pide explícitamente una acción física (bailar, saludar, hacer una reverencia, poner una cara...), "
            "llama primero a la herramienta adecuada (perform_action, set_expression, set_hand_pose o look_at) y luego responde."
        )
    # "Eres <nombre>." y el nombre del usuario los antepone PersonaPromptMiddleware en cada turno
    return f"{p.description.strip()}\n\nReglas:\n" + "\n".join(f"- {r}" for r in rules)


def ensure_memory(cfg: Config) -> None:
    mem_dir = cfg.data_dir / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    f = mem_dir / "AGENTS.md"
    if not f.exists():
        f.write_text(MEMORY_SEED, encoding="utf-8")


def build_agent(cfg: Config, avatar: Avatar, decision: DecisionClient, checkpointer=None):
    ensure_memory(cfg)
    model = build_chat_model(cfg)
    profile_key = _register_lean_profile(model)
    log.info("Perfil de harness registrado: %s", profile_key)

    backend = CompositeBackend(
        default=StateBackend(),
        routes={"/memories/": FilesystemBackend(root_dir=cfg.data_dir / "memories", virtual_mode=True)},
    )
    tools = build_tools(avatar) if cfg.agent.use_action_tools else []

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=persona_prompt(cfg, with_tools=bool(tools)),
        middleware=[PersonaPromptMiddleware(cfg), ReactionMiddleware(cfg, decision, avatar)],
        context_schema=TurnContext,
        memory=[MEMORY_PATH],
        backend=backend,
        checkpointer=checkpointer,
        name="vtuber",
    )

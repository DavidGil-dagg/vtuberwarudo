"""Nombres de personaje y usuario: llegan con cada mensaje y se usan sin recrear el agente."""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

import vtuber_warudo.agent as agent_mod
from vtuber_warudo.config import load_config
from vtuber_warudo.decision import Decision
from vtuber_warudo.persona import (
    PersonaPromptMiddleware,
    TurnContext,
    _prepend,
    character_name,
    clean_name,
    identity_prompt,
)
from vtuber_warudo.reaction import build_state_text, decide_reaction, decide_talk_style


# -- utilidades --------------------------------------------------------------------------
def test_clean_name():
    assert clean_name("  Ana \n María\t ") == "Ana María"
    assert clean_name(None) == ""
    assert clean_name(123) == ""
    assert clean_name("x" * 100) == "x" * 40


def test_turn_context_from_message_uses_config_default():
    cfg = load_config()
    cfg.persona.name = "Pordefecto"
    ctx = TurnContext.from_message(cfg, {"type": "user", "text": "hola"})
    assert ctx == TurnContext(character="Pordefecto", user="")
    ctx = TurnContext.from_message(cfg, {"character": " Luna\n", "user_name": "Dani"})
    assert ctx == TurnContext(character="Luna", user="Dani")
    # vacío o solo espacios => el de config.toml
    assert TurnContext.from_message(cfg, {"character": "   "}).character == "Pordefecto"


def test_character_name_fallback():
    cfg = load_config()
    cfg.persona.name = "Pordefecto"
    assert character_name(cfg, None) == "Pordefecto"
    assert character_name(cfg, TurnContext("Luna")) == "Luna"


def test_identity_prompt():
    assert identity_prompt("Luna") == "Eres Luna."
    text = identity_prompt("Luna", "Dani")
    assert text.startswith("Eres Luna.")
    assert "se llama Dani" in text


def test_prepend_keeps_existing_system_prompt():
    assert _prepend(None, "A").content == "A"
    assert _prepend(SystemMessage(content="B"), "A").content == "A\nB"
    blocks = _prepend(SystemMessage(content=[{"type": "text", "text": "B"}]), "A").content
    assert blocks == [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]


def test_persona_prompt_has_no_fixed_name():
    cfg = load_config()
    cfg.persona.name = "Shipilka"
    prompt = agent_mod.persona_prompt(cfg, with_tools=False)
    assert "Shipilka" not in prompt
    assert cfg.persona.description.strip()[:20] in prompt


# -- textos del modelo de decisión ---------------------------------------------------------
class RecordingDecision:
    def __init__(self):
        self.states: list[str] = []

    async def decide_many(self, state, questions, images=None):
        self.states.append(state)
        return {qid: Decision(0, next(iter(q.options)), "", 1, True) for qid, q in questions.items()}


def test_state_texts_use_character_name():
    cfg = load_config()
    cfg.persona.name = "Pordefecto"
    assert "Luna (character): hola" in build_state_text(cfg, "hey", "hola", "Luna")
    assert "Pordefecto (character)" in build_state_text(cfg, "hey", "hola")

    dec = RecordingDecision()
    cfg.talk.enabled = True
    asyncio.run(decide_talk_style(cfg, dec, "¿qué tal?", "Luna"))
    asyncio.run(decide_reaction(cfg, dec, "¿qué tal?", "bien", character="Luna"))
    assert dec.states[0].startswith("User says to Luna:")
    assert "Luna (character): bien" in dec.states[1]


def test_vision_state_uses_character_name():
    from vtuber_warudo.vision import VisionMode

    cfg = load_config()
    cfg.persona.name = "Pordefecto"

    async def emit(_):
        pass

    vm = VisionMode(cfg, RecordingDecision(), object(), emit=emit, is_busy=lambda: False)
    assert vm._state("").startswith("Pordefecto, a VTuber character")
    vm.character = "Luna"
    assert vm._state("").startswith("Luna, a VTuber character")


# -- agente completo con un modelo falso ---------------------------------------------------
class RecordingChat(GenericFakeChatModel):
    """Responde siempre lo mismo y guarda los mensajes que recibe en cada llamada."""

    calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="¡Hola!"))])


class FakeDirector:
    def reaction_planned(self):
        pass


class FakeAvatar:
    def __init__(self):
        self.director = FakeDirector()

    def start_talking(self, style):
        return False

    def speech_will_end_in(self, seconds):
        pass

    def plan_reaction(self, emotion, gesture, hand):
        return {}


@pytest.fixture
def chat(monkeypatch, tmp_path):
    cfg = load_config()
    cfg.persona.name = "Pordefecto"
    cfg.agent.data_dir = str(tmp_path)
    cfg.speech.enabled = False
    cfg.agent.use_action_tools = False  # sin herramientas de Warudo: solo importa el prompt
    model = RecordingChat(messages=iter(()), calls=[])
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda c: model)
    monkeypatch.setattr(agent_mod, "_register_lean_profile", lambda m: "fake")
    dec = RecordingDecision()
    agent = agent_mod.build_agent(cfg, FakeAvatar(), dec, checkpointer=InMemorySaver())
    return cfg, agent, model, dec


def _system_text(messages: list[BaseMessage]) -> str:
    sys = next(m for m in messages if isinstance(m, SystemMessage))
    return sys.text


def test_agent_uses_names_per_turn_without_rebuilding(chat):
    cfg, agent, model, dec = chat
    config = {"configurable": {"thread_id": "t1"}}

    async def turn(text, ctx):
        await agent.ainvoke({"messages": [{"role": "user", "content": text}]}, config=config, context=ctx)

    asyncio.run(turn("hola", TurnContext("Luna", "Dani")))
    first = _system_text(model.calls[-1])
    assert first.startswith("Eres Luna.")
    assert "se llama Dani" in first
    assert "Pordefecto" not in first
    assert any(s.startswith("User says to Luna:") for s in dec.states)

    # Mismo hilo, nombres nuevos: el agente es el mismo y el historial sigue intacto
    asyncio.run(turn("¿sigues ahí?", TurnContext("Sol", "")))
    second = _system_text(model.calls[-1])
    assert second.startswith("Eres Sol.")
    assert "Luna" not in second and "Dani" not in second
    assert "se llama" not in second

    snap = asyncio.run(agent.aget_state(config))
    texts = [m.text for m in snap.values["messages"] if m.type in ("human", "ai")]
    assert texts == ["hola", "¡Hola!", "¿sigues ahí?", "¡Hola!"]
    # los nombres no se guardan en el hilo
    assert all(not isinstance(m, SystemMessage) for m in snap.values["messages"])


def test_agent_without_context_uses_config_name(chat):
    cfg, agent, model, dec = chat

    async def go():
        await agent.ainvoke({"messages": [{"role": "user", "content": "hola"}]},
                            config={"configurable": {"thread_id": "t2"}})

    asyncio.run(go())
    assert _system_text(model.calls[-1]).startswith("Eres Pordefecto.")


def test_middleware_is_registered():
    assert PersonaPromptMiddleware(load_config()).name

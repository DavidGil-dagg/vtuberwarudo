"""Middleware que anima al personaje con el modelo de decisión.

- before_agent: al llegar el mensaje del usuario elige un *estilo al hablar* y
  lanza la animación de brazos/manos mientras Gemma genera la respuesta.
- after_agent: con la respuesta final elige *emoción*, *gesto* y *pose de mano*
  (tres preguntas en paralelo) y las aplica en Warudo.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from .avatar import Avatar
from .config import Config
from .decision import Decision, DecisionClient, Question, StartLuxClient, clip_state
from .persona import character_name
from .speech import speech_seconds
from .tools import PERFORM_ACTION, SET_EXPRESSION, SET_HAND_POSE

log = logging.getLogger(__name__)


@dataclass
class Choice:
    key: str | None = None
    decision: Decision | None = None


@dataclass
class ReactionChoice:
    emotion: Choice
    gesture: Choice
    hand: Choice

    def to_event(self, applied: dict[str, bool]) -> dict[str, Any]:
        decs = [c.decision for c in (self.emotion, self.gesture, self.hand) if c.decision]
        return {
            "type": "reaction",
            "emotion": self.emotion.key,
            "gesture": self.gesture.key,
            "hand": self.hand.key,
            "ms": max((d.ms for d in decs), default=0),
            "valid": all(d.valid for d in decs),
            # por parte: True solo si Warudo confirmó el comando
            "applied": applied,
        }


def build_state_text(cfg: Config, user_text: str, reply_text: str, character: str | None = None) -> str:
    name = character or cfg.persona.name
    text = f"User: {user_text.strip()}\n{name} (character): {reply_text.strip()}"
    return clip_state(text, cfg.reaction.max_state_chars)


def options_of(table: dict) -> dict[str, str]:
    """{clave: label} de una tabla de config ([emotions], [gestures]...)."""
    return {k: v.label for k, v in table.items()}


def reaction_questions(
    cfg: Config, *, emotion_q: str, gesture_q: str, hand_q: str,
    want_emotion: bool = True, want_gesture: bool = True, want_hand: bool = True,
) -> dict[str, Question]:
    qs: dict[str, Question] = {}
    if want_emotion and len(cfg.emotions) >= 2:
        qs["emotion"] = Question(emotion_q, options_of(cfg.emotions))
    if want_gesture and len(cfg.gestures) >= 2:
        qs["gesture"] = Question(gesture_q, options_of(cfg.gestures))
    if want_hand and len(cfg.hand_poses) >= 2:
        qs["hand"] = Question(hand_q, options_of(cfg.hand_poses))
    return qs


def _choice(answers: dict, qid: str) -> Choice:
    d = answers.get(qid)
    return Choice(d.option, d) if isinstance(d, Decision) else Choice()


async def decide_reaction(
    cfg: Config,
    decision: DecisionClient | StartLuxClient,
    user_text: str,
    reply_text: str,
    *,
    want_emotion: bool = True,
    want_gesture: bool = True,
    want_hand: bool = True,
    character: str | None = None,
) -> ReactionChoice:
    """Emoción, gesto y pose de mano en una sola llamada al modelo de decisión."""
    state = build_state_text(cfg, user_text, reply_text, character)
    r = cfg.reaction
    qs = reaction_questions(
        cfg, emotion_q=r.emotion_question, gesture_q=r.gesture_question, hand_q=r.hand_question,
        want_emotion=want_emotion, want_gesture=want_gesture, want_hand=want_hand,
    )
    answers = await decision.decide_many(state, qs) if qs else {}
    return ReactionChoice(_choice(answers, "emotion"), _choice(answers, "gesture"), _choice(answers, "hand"))


async def decide_talk_style(
    cfg: Config, decision: DecisionClient | StartLuxClient, user_text: str, character: str | None = None
) -> Choice:
    if not cfg.talk.enabled or len(cfg.talk_styles) < 2:
        return Choice()
    name = character or cfg.persona.name
    state = clip_state(f"User says to {name}: {user_text.strip()}", cfg.reaction.max_state_chars)
    answers = await decision.decide_many(state, {"talk": Question(cfg.talk.question, options_of(cfg.talk_styles))})
    return _choice(answers, "talk")


def _last_turn(messages: list) -> tuple[HumanMessage | None, AIMessage | None, set[str]]:
    """Último mensaje del usuario, respuesta final y herramientas usadas en ese turno."""
    human_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            human_idx = i
            break
    if human_idx is None:
        return None, None, set()
    turn = messages[human_idx + 1 :]
    used = {m.name for m in turn if isinstance(m, ToolMessage) and m.name}
    final = next(
        (m for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls and m.text.strip()),
        None,
    )
    return messages[human_idx], final, used


def _emit(event: dict) -> None:
    try:
        get_stream_writer()(event)
    except Exception:  # noqa: BLE001 - fuera de un stream no hay writer
        pass


class ReactionMiddleware(AgentMiddleware):
    """before_agent: gesticular al hablar. after_agent: reacción final."""

    def __init__(self, cfg: Config, decision: DecisionClient, avatar: Avatar):
        super().__init__()
        self.cfg = cfg
        self.decision = decision
        self.avatar = avatar

    # -- al empezar el turno --------------------------------------------------
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        if not self.cfg.talk.enabled or not self.cfg.talk_styles:
            return None
        msgs = state["messages"]
        if not msgs or not isinstance(msgs[-1], HumanMessage):
            return None
        # Se decide ANTES de llamar a Gemma (~150 ms con la GPU libre). Si se lanza en
        # paralelo, LM Studio encola la petición detrás del prefill de Gemma (~8 s).
        try:
            choice = await decide_talk_style(
                self.cfg, self.decision, msgs[-1].text, character_name(self.cfg, runtime.context)
            )
            if choice.key:
                applied = self.avatar.start_talking(choice.key)
                log.info("Estilo al hablar: %s (%s ms)", choice.key, choice.decision.ms if choice.decision else "?")
                _emit({"type": "talk", "style": choice.key, "applied": applied})
        except Exception:  # noqa: BLE001
            log.exception("No se pudo lanzar la animación al hablar")
        return None

    # -- al terminar la respuesta ---------------------------------------------
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        human, reply, used = _last_turn(state["messages"])
        if human is None or reply is None:
            self.avatar.director.reaction_planned()
            self.avatar.speech_will_end_in(0)
            return None
        # Duración estimada de la "voz" (el servidor la afina con el tiempo exacto que
        # falta por mostrar; en la CLI se usa tal cual para que la gesticulación termine).
        self.avatar.speech_will_end_in(speech_seconds(self.cfg.speech, reply.text) if self.cfg.speech.enabled else 0)
        choice = await decide_reaction(
            self.cfg,
            self.decision,
            human.text,
            reply.text,
            want_emotion=SET_EXPRESSION not in used,
            want_gesture=PERFORM_ACTION not in used,
            want_hand=SET_HAND_POSE not in used,
            character=character_name(self.cfg, runtime.context),
        )
        # El gesto entra en la coreografía (tras el Start, dentro del texto); la expresión y la
        # pose de mano se aplican cuando empieza. Si Gemma ya hizo un gesto con la herramienta,
        # solo cara y manos.
        gesture = choice.gesture.key if PERFORM_ACTION not in used else None
        planned = self.avatar.plan_reaction(choice.emotion.key, gesture, choice.hand.key)
        log.info(
            "Reacción: emoción=%s gesto=%s mano=%s (gesto programado=%s)",
            choice.emotion.key, gesture, choice.hand.key, planned.get("gesture"),
        )
        _emit(choice.to_event({}))
        return None

    def before_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return None

    def after_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        # La app siempre usa la API async; en modo síncrono no se reacciona.
        return None

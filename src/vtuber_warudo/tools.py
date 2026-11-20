"""Herramientas que el agente (Gemma) puede llamar para mover al personaje a propósito."""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model

from .avatar import Avatar

PERFORM_ACTION = "perform_action"
SET_EXPRESSION = "set_expression"
LOOK_AT = "look_at"
SET_HAND_POSE = "set_hand_pose"
ACTION_TOOL_NAMES = {PERFORM_ACTION, SET_EXPRESSION, LOOK_AT, SET_HAND_POSE}


def build_tools(avatar: Avatar) -> list[BaseTool]:
    gestures = [k for k in avatar.gesture_keys if k != "none"]
    emotions = avatar.emotion_keys

    GestureArgs = create_model(
        "PerformActionArgs",
        action=(Literal[tuple(gestures)], Field(description="Gesto o animación corporal a realizar")),  # type: ignore[valid-type]
    )
    EmotionArgs = create_model(
        "SetExpressionArgs",
        emotion=(Literal[tuple(emotions)], Field(description="Expresión facial a mostrar")),  # type: ignore[valid-type]
    )
    LookArgs = create_model(
        "LookAtArgs",
        mode=(Literal["camera", "away", "default"], Field(description="camera = mirar al usuario, away = apartar la mirada")),
    )

    async def perform_action(action: str) -> str:
        ok = await avatar.gesture(action)
        return (
            f"Gesto '{action}' en marcha (empieza al terminar tu gesto actual)."
            if ok
            else f"Gesto '{action}' no disponible ahora; continúa la conversación."
        )

    async def set_expression(emotion: str) -> str:
        ok = await avatar.emote(emotion)
        return f"Expresión '{emotion}' aplicada." if ok else "Expresión no disponible ahora; continúa la conversación."

    async def look_at(mode: str) -> str:
        ok = await avatar.look(mode)
        return f"Mirada: {mode}." if ok else "No disponible ahora; continúa la conversación."

    async def set_hand_pose(pose: str) -> str:
        ok = await avatar.hand_pose(pose)
        return f"Pose de mano '{pose}' aplicada." if ok else "No disponible ahora; continúa la conversación."

    tools: list[BaseTool] = []
    hand_poses = [k for k in avatar.cfg.hand_poses if avatar.cfg.hand_poses[k].uri]
    if hand_poses:
        HandArgs = create_model(
            "SetHandPoseArgs",
            pose=(Literal[tuple(hand_poses)], Field(description="Forma de la mano")),  # type: ignore[valid-type]
        )
        tools.append(
            StructuredTool.from_function(
                coroutine=set_hand_pose,
                name=SET_HAND_POSE,
                description="Pon la mano en una forma concreta (signo de paz, pulgar arriba, OK...). "
                "Úsalo solo si el usuario te pide explícitamente un gesto con la mano.",
                args_schema=HandArgs,
            )
        )

    return tools + [
        StructuredTool.from_function(
            coroutine=perform_action,
            name=PERFORM_ACTION,
            description=(
                "Haz que tu cuerpo realice un gesto o animación. Úsalo SOLO cuando el usuario te pida "
                "explícitamente una acción física (p.ej. 'baila', 'saluda', 'haz una reverencia')."
            ),
            args_schema=GestureArgs,
        ),
        StructuredTool.from_function(
            coroutine=set_expression,
            name=SET_EXPRESSION,
            description="Cambia tu expresión facial. Úsalo solo si el usuario te pide poner una cara concreta.",
            args_schema=EmotionArgs,
        ),
        StructuredTool.from_function(
            coroutine=look_at,
            name=LOOK_AT,
            description="Cambia hacia dónde miras. Úsalo solo si el usuario te lo pide.",
            args_schema=LookArgs,
        ),
    ]

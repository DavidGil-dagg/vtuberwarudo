"""Carga de config.toml a dataclasses."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"


@dataclass
class LMStudioConfig:
    base_url: str = "http://localhost:1234/v1"
    chat_model: str = "google/gemma-4-12b-qat"
    decision_model: str = "jev-style-qwen3.5-2b-decision"
    temperature: float = 0.8
    max_tokens: int = 512
    reasoning_effort: str = "none"
    timeout_s: float = 120


@dataclass
class DecisionConfig:
    backend: str = "startlux"  # "startlux" | "jev"
    startlux_url: str = "http://127.0.0.1:8090"


@dataclass
class VisionConfig:
    monitor: int = 1  # 1 = pantalla principal (mss); 0 = todas
    max_side: int = 768  # px del lado mayor de la captura
    jpeg_quality: int = 75
    interval_s: float = 4.0  # cada cuánto mira la pantalla si nadie habla
    cooldown_s: float = 6.0  # mínimo entre dos reacciones
    react_threshold: float = 0.5  # P(sí) para reaccionar a la pantalla
    speech_threshold: float = 0.35  # P(sí) para reaccionar cuando hablas
    scene_change_threshold: float = 0.015  # fracción de pantalla que debe cambiar (0..1) para volver a mirar
    focus_motion: bool = True  # enviar solo la zona que se mueve (p.ej. un vídeo dentro de la web)
    # Calibrado con capturas reales: esta formulación da 0.79 con contenido (manga en X) y 0.03
    # con una pantalla vacía; "Should the character react right now?" daba 0.58 / 0.29.
    react_question: str = (
        "Is there something on the screen or in what the streamer just said that a lively streamer "
        "companion would comment on or react to (funny, cute, exciting, surprising, sad, scary or impressive)?"
    )
    # Sin "neutral" entre las opciones: con ella el modelo elige neutral casi siempre.
    emotion_question: str = (
        "The character is an expressive VTuber who always shows a feeling. "
        "Which emotion fits best what is on screen and what the streamer said?"
    )
    gesture_question: str = "Which body gesture should the character make as a reaction?"
    hand_question: str = "Which hand shape fits the character's reaction?"
    # Micrófono / Whisper
    mic_enabled: bool = True
    mic_device: str = ""  # nombre o índice; "" = predeterminado
    whisper_model: str = "small"
    language: str = "es"
    energy_threshold: float = 0.004  # m�nimo; se adapta a 4x el ruido de fondo
    silence_ms: int = 700  # silencio que cierra una frase
    max_phrase_s: float = 12.0


@dataclass
class WarudoConfig:
    url: str = "ws://127.0.0.1:19500"
    expression_hold_ms: int = 5000
    auto_import_expressions: bool = True  # importar las expresiones VRM si el personaje no las tiene


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class PersonaConfig:
    name: str = "VtuberWarudo"
    description: str = ""


@dataclass
class AgentConfig:
    use_action_tools: bool = True
    data_dir: str = "data"


@dataclass
class ReactionConfig:
    on_user_message: str = ""
    emotion_question: str = "Which emotion does the character show in their reply to the user?"
    gesture_question: str = "Which body gesture should the character perform while giving this reply?"
    hand_question: str = "Which hand shape fits the character's reply best?"
    hand_hold_ms: int = 4000
    max_state_chars: int = 2400


@dataclass
class TalkConfig:
    enabled: bool = True
    question: str = "In which speaking style should the character answer this message?"
    mask: str = "upper"


@dataclass
class SpeechConfig:
    enabled: bool = True
    chars_per_second: float = 15
    pause_comma_ms: int = 150
    pause_sentence_ms: int = 400


@dataclass
class AnimationConfig:
    crossfade_ms: int = 600
    max_wait_ms: int = 10000


@dataclass
class TalkStyle:
    key: str
    label: str
    uri: str = ""  # clip en bucle mientras habla
    speed: float = 1.0
    start: str = ""  # clip de entrada (opcional)
    end: str = ""  # clip de cierre (opcional)


def _derive(uri: str, suffix: str) -> str:
    return uri[: -len("_Loop")] + suffix if uri.endswith("_Loop") else ""


@dataclass
class HandPose:
    key: str
    label: str
    uri: str = ""
    hand: str = "right"
    weight: float = 1.0


@dataclass
class Emotion:
    key: str
    label: str
    expression: str = ""


@dataclass
class Gesture:
    key: str
    label: str
    uri: str = ""
    speed: float = 1.0
    look: str = ""
    mask: str = "full"


@dataclass
class Config:
    lmstudio: LMStudioConfig = field(default_factory=LMStudioConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    warudo: WarudoConfig = field(default_factory=WarudoConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    reaction: ReactionConfig = field(default_factory=ReactionConfig)
    talk: TalkConfig = field(default_factory=TalkConfig)
    animation: AnimationConfig = field(default_factory=AnimationConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    emotions: dict[str, Emotion] = field(default_factory=dict)
    gestures: dict[str, Gesture] = field(default_factory=dict)
    talk_styles: dict[str, TalkStyle] = field(default_factory=dict)
    hand_poses: dict[str, HandPose] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        p = Path(self.agent.data_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p


def _section(cls, data: dict | None):
    data = data or {}
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    obj = cls(**known)
    for attr in ("description",):
        if isinstance(getattr(obj, attr, None), str):
            setattr(obj, attr, getattr(obj, attr).strip())
    return obj


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("VTUBER_CONFIG", DEFAULT_CONFIG))
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    emotions = {
        key: Emotion(key=key, label=v.get("label", key), expression=v.get("expression", ""))
        for key, v in raw.get("emotions", {}).items()
    }
    gestures = {
        key: Gesture(
            key=key,
            label=v.get("label", key),
            uri=v.get("uri", ""),
            speed=float(v.get("speed", 1.0)),
            look=v.get("look", ""),
            mask=v.get("mask", "full"),
        )
        for key, v in raw.get("gestures", {}).items()
    }
    talk_styles = {
        key: TalkStyle(
            key=key,
            label=v.get("label", key),
            uri=v.get("uri", ""),
            speed=float(v.get("speed", 1.0)),
            start=v.get("start", _derive(v.get("uri", ""), "_Start")),
            end=v.get("end", _derive(v.get("uri", ""), "_End")),
        )
        for key, v in raw.get("talk_styles", {}).items()
    }
    hand_poses = {
        key: HandPose(
            key=key,
            label=v.get("label", key),
            uri=v.get("uri", ""),
            hand=v.get("hand", "right"),
            weight=float(v.get("weight", 1.0)),
        )
        for key, v in raw.get("hand_poses", {}).items()
    }
    if len(emotions) < 2 or len(gestures) < 2:
        raise ValueError("config.toml necesita al menos 2 [emotions.*] y 2 [gestures.*]")
    for name, table in (("emotions", emotions), ("gestures", gestures), ("talk_styles", talk_styles), ("hand_poses", hand_poses)):
        if len(table) > 26:
            raise ValueError(f"[{name}]: el modelo de decisión admite como máximo 26 opciones por pregunta")
        if len(table) == 1:
            raise ValueError(f"[{name}]: hacen falta al menos 2 opciones (o ninguna para desactivarlo)")

    return Config(
        lmstudio=_section(LMStudioConfig, raw.get("lmstudio")),
        decision=_section(DecisionConfig, raw.get("decision")),
        vision=_section(VisionConfig, raw.get("vision")),
        warudo=_section(WarudoConfig, raw.get("warudo")),
        server=_section(ServerConfig, raw.get("server")),
        persona=_section(PersonaConfig, raw.get("persona")),
        agent=_section(AgentConfig, raw.get("agent")),
        reaction=_section(ReactionConfig, raw.get("reaction")),
        talk=_section(TalkConfig, raw.get("talk")),
        animation=_section(AnimationConfig, raw.get("animation")),
        speech=_section(SpeechConfig, raw.get("speech")),
        emotions=emotions,
        gestures=gestures,
        talk_styles=talk_styles,
        hand_poses=hand_poses,
    )

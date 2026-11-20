"""Traduce emociones/gestos lógicos (claves de config.toml) a comandos de Warudo.

Las animaciones de cuerpo pasan por el AnimationDirector (coreografía del turno:
Start → gesto → Loops → End, todo dentro del texto). La expresión y la pose de mano
de la reacción se aplican en el mismo instante en que empieza el gesto.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .animation import AnimationDirector
from .config import Config
from .warudo import WarudoClient

log = logging.getLogger(__name__)


def _ok(reply: dict | None) -> bool:
    return bool(reply and reply.get("ok"))


# Las expresiones importadas del VRM se llaman distinto según la versión del modelo / de
# Warudo: VRM 1.0 (happy, sad, relaxed...) o VRM 0.x (Joy, Sorrow, Fun...). Son equivalentes.
_EXPRESSION_ALIASES = [
    {"happy", "joy"},
    {"sad", "sorrow"},
    {"relaxed", "fun"},
    {"angry"},
    {"surprised"},
    {"neutral"},
]


def resolve_expression(name: str, available: list[str]) -> str | None:
    """Nombre real de la expresión en el personaje (sin distinguir mayúsculas y aceptando el
    equivalente VRM 0.x / 1.0). None si el personaje no tiene ninguna equivalente."""
    if not name:
        return None
    by_lower = {a.lower(): a for a in available}
    if name.lower() in by_lower:
        return by_lower[name.lower()]
    for group in _EXPRESSION_ALIASES:
        if name.lower() in group:
            for alias in group:
                if alias in by_lower:
                    return by_lower[alias]
    return None


class Avatar:
    def __init__(self, cfg: Config, warudo: WarudoClient):
        self.cfg = cfg
        self.warudo = warudo
        self.director = AnimationDirector(cfg, warudo)
        # Destino de eventos para la UI durante el turno (lo pone el servidor)
        self.event_sink: Callable[[dict], Awaitable[None]] | None = None
        self._bg: set[asyncio.Task] = set()
        self._expressions: list[str] = []  # expresiones reales del personaje (de "info")

    @property
    def emotion_keys(self) -> list[str]:
        return list(self.cfg.emotions)

    @property
    def gesture_keys(self) -> list[str]:
        return list(self.cfg.gestures)

    def all_clip_uris(self) -> list[str]:
        uris = [g.uri for g in self.cfg.gestures.values()]
        for s in self.cfg.talk_styles.values():
            uris += [s.start, s.uri, s.end]
        return [u for u in uris if u]

    async def prefetch(self) -> None:
        """Al conectar con Warudo: duración de los clips (para planificar el turno) y
        comprobación de que el personaje tiene las expresiones que usan las emociones."""
        await self.director.prefetch_lengths(self.all_clip_uris())
        await self.ensure_expressions()

    async def ensure_expressions(self) -> list[str]:
        """Si el personaje no tiene expresiones (p.ej. la escena se guardó antes de importarlas),
        las importa desde el VRM. Devuelve las expresiones que tiene al final."""
        info = await self.warudo.info() or {}
        current = list(info.get("expressions") or [])
        self._expressions = current
        needed = {e.expression for e in self.cfg.emotions.values() if e.expression}
        missing = sorted(n for n in needed if resolve_expression(n, current) is None)
        if not missing:
            return current
        if not self.cfg.warudo.auto_import_expressions:
            log.warning("Al personaje le faltan expresiones %s (auto_import_expressions = false)", missing)
            return current
        log.warning("Al personaje le faltan expresiones %s: importándolas desde el VRM...", missing)
        r = await self.warudo.send({"cmd": "importVrmExpressions"}, wait=True, timeout=5)
        if not _ok(r):
            log.warning("No se pudieron importar las expresiones: %s", (r or {}).get("error"))
            return current
        for _ in range(20):  # la importación es asíncrona en Warudo
            await asyncio.sleep(0.25)
            current = list(((await self.warudo.info()) or {}).get("expressions") or [])
            if all(resolve_expression(n, current) for n in needed):
                break
        self._expressions = current
        still = sorted(n for n in needed if resolve_expression(n, current) is None)
        if still:
            log.warning("Tras importar siguen faltando %s (expresiones del personaje: %s)", still, current)
        else:
            log.info("Expresiones importadas: %s. Guarda la escena en Warudo para conservarlas.", current)
        return current

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # -- canales inmediatos ------------------------------------------------------
    async def emote(self, key: str) -> bool:
        """Pone la expresión asociada a la emoción. True si Warudo la aplicó."""
        emo = self.cfg.emotions.get(key)
        if emo is None:
            log.warning("Emoción desconocida: %s", key)
            return False
        if not emo.expression:
            return _ok(await self.warudo.neutral())
        name = resolve_expression(emo.expression, self._expressions) or emo.expression
        ok = _ok(await self.warudo.expression(name, self.cfg.warudo.expression_hold_ms))
        if not ok and not self._expressions:
            # Aún no se conocían las expresiones del personaje: consultarlas y reintentar una vez
            await self.ensure_expressions()
            name = resolve_expression(emo.expression, self._expressions)
            ok = bool(name) and _ok(await self.warudo.expression(name, self.cfg.warudo.expression_hold_ms))
        return ok

    async def hand_pose(self, key: str) -> bool:
        """Pose de dedos. La clave sin uri (p.ej. "none") suelta la pose."""
        p = self.cfg.hand_poses.get(key)
        if p is None:
            log.warning("Pose de mano desconocida: %s", key)
            return False
        if not p.uri:
            return _ok(await self.warudo.hand_pose("both", ""))
        return _ok(await self.warudo.hand_pose(p.hand, p.uri, p.weight, self.cfg.reaction.hand_hold_ms))

    async def look(self, mode: str) -> bool:
        return _ok(await self.warudo.look_at(mode))

    # -- cuerpo (coreografía) ------------------------------------------------------
    def _gesture_clip_args(self, key: str) -> dict | None:
        g = self.cfg.gestures.get(key)
        if g is None or not g.uri:
            return None
        return {"uri": g.uri, "speed": g.speed, "mask": g.mask, "label": f"gesture:{key}"}

    async def gesture(self, key: str, *, wait_start: bool = False) -> bool:
        """Gesto pedido explícitamente (herramienta / CLI). Mientras se habla, entra dentro
        de la gesticulación; si no, al final de la cola."""
        g = self.cfg.gestures.get(key)
        if g is None:
            log.warning("Gesto desconocido: %s", key)
            return False
        if not self.warudo.connected:
            return False
        args = self._gesture_clip_args(key)
        on_start = (lambda: self._spawn(self.warudo.look_at(g.look))) if g.look else None
        if args is None:
            if g.look:
                await self.warudo.look_at(g.look)
            return bool(g.look)
        fut = self.director.insert_in_talk(**args, on_start=on_start)
        return await fut if wait_start else True

    def plan_reaction(self, emotion: str | None, gesture: str | None, hand: str | None) -> dict[str, bool]:
        """Reacción final: el gesto entra en la coreografía y la expresión y la pose de mano
        se aplican cuando el gesto empieza (o ya, si no hay gesto). Devuelve qué se programó;
        el resultado real llega a la UI como evento "reaction_applied"."""
        args = self._gesture_clip_args(gesture) if gesture and gesture != "none" else None
        g = self.cfg.gestures.get(gesture) if gesture else None

        async def apply_face_and_hands(gesture_started: bool | None) -> None:
            res: dict[str, bool] = {}
            if g is not None and g.look and gesture_started:
                await self.warudo.look_at(g.look)
            if emotion:
                res["emotion"] = await self.emote(emotion)
            if hand:
                res["hand"] = await self.hand_pose(hand)
            if gesture_started is not None:
                res["gesture"] = gesture_started
            if self.event_sink:
                await self.event_sink({"type": "reaction_applied", "applied": res})

        if args is None or not self.warudo.connected:
            self.director.reaction_planned()  # sin gesto: los Loops pueden seguir
            self._spawn(apply_face_and_hands(None))
            return {"gesture": False}

        def on_start() -> None:
            self._spawn(apply_face_and_hands(True))

        fut = self.director.insert_in_talk(**args, on_start=on_start)

        def on_done(f: asyncio.Future) -> None:
            if not f.cancelled() and f.result() is False:  # descartado: aplicar cara/mano igualmente
                self._spawn(apply_face_and_hands(False))

        fut.add_done_callback(on_done)
        return {"gesture": True}

    # -- gesticular al hablar ------------------------------------------------------
    def start_talking(self, key: str) -> bool:
        """Empieza a gesticular al hablar (Start + Loops) hasta que acabe la voz."""
        s = self.cfg.talk_styles.get(key)
        if s is None or not s.uri or not self.warudo.connected:
            return False
        self.director.start_talking(s)
        return True

    def speech_will_end_in(self, seconds: float) -> None:
        """La voz acabará en `seconds`: la coreografía ajusta Loops y End para terminar a la vez."""
        self.director.set_talk_deadline(asyncio.get_running_loop().time() + max(0.0, seconds))

    def speech_finished(self) -> None:
        """Fin de la voz: si la gesticulación sigue (p.ej. sin deadline), cierre ordenado."""
        self.director.stop_talking()

    def stop_talking(self) -> None:
        self.director.stop_talking()

    async def talk_once(self, key: str) -> bool:
        """Reproduce una vuelta del Loop de un estilo (para probar desde la CLI)."""
        s = self.cfg.talk_styles.get(key)
        if s is None or not s.uri:
            return False
        return _ok(await self.warudo.animation(s.uri, s.speed, self.cfg.talk.mask))

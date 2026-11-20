"""Coreografía de las animaciones de cuerpo de cada turno.

Gemma genera la respuesta en 1-3 s, pero el texto se muestra a ritmo de habla
(10-25 s). En cuanto termina de generar ya se sabe todo: qué dice, cuánto dura la
"voz" y qué gesto toca. Con eso el turno se planifica para que todo ocurra
mientras se ve el texto:

    texto:  |───────────────────────── voz ─────────────────────────|
    cuerpo: | Start | Loop… ⤳ GESTO (entero) ⤳ Loop… ⤳ End          |
                              ↑ expresión y pose de mano a la vez

``⤳`` = fundido (crossfade). Los clips se encadenan: el siguiente empieza
``crossfade_ms`` antes de que acabe el actual, así Warudo pasa de clip a clip sin
volver a su animación de reposo. El reposo solo aparece cuando el turno termina.

- ``start_talking(style)``: Start y Loops mientras dure la voz.
- ``insert_in_talk(clip)``: el gesto de la reacción entra en cuanto llega. Si suena un
  Loop (relleno), el gesto lo sustituye con el fundido; Start, End y otros gestos
  siempre se terminan.
- ``set_talk_deadline(t)``: hora a la que acaba la voz; cada Loop ajusta un poco su
  velocidad para que el End termine con el último carácter. Si no cabe el End, el
  gesto cierra el turno.
- ``stop_talking()``: cierre ordenado (el clip en curso termina entero).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config, TalkStyle
from .warudo import WarudoClient

log = logging.getLogger(__name__)

# Límites de velocidad para encajar Loop/End en el tiempo de la voz (fuera de esto se nota).
LOOP_SPEED = (0.75, 1.35)
END_SPEED = (0.6, 1.3)
GESTURE_MAX_SPEEDUP = 1.3
# Un Loop solo se sustituye por el gesto si aún le queda al menos esto (si no, se deja acabar).
PREEMPT_MIN_LEFT_S = 1.0


def _clamp(v: float, lo_hi: tuple[float, float]) -> float:
    return max(lo_hi[0], min(lo_hi[1], v))


@dataclass
class Clip:
    uri: str
    speed: float = 1.0
    mask: str = "full"
    label: str = ""
    kind: str = "gesture"  # "talk" | "gesture"
    enqueued_at: float = 0.0
    on_start: Callable[[], None] | None = field(default=None, repr=False)
    started: asyncio.Future | None = field(default=None, repr=False)


class AnimationDirector:
    def __init__(self, cfg: Config, warudo: WarudoClient):
        self.cfg = cfg
        self.warudo = warudo
        self._queue: list[Clip] = []
        self._busy_until = 0.0  # cuándo puede empezar el siguiente clip (fin - crossfade)
        self._clip_end = 0.0  # cuándo termina de verdad el clip actual
        self._current: Clip | None = None
        self._talk: TalkStyle | None = None  # estilo mientras se habla (bucle activo)
        self._talk_deadline: float | None = None  # hora (loop.time) a la que termina la voz
        self._lengths: dict[str, float] = {}  # duración real de cada clip a velocidad 1 (s)
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    # -- utilidades --------------------------------------------------------------
    @property
    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    @property
    def _xf(self) -> float:
        return max(0.0, self.cfg.animation.crossfade_ms / 1000)

    def length(self, uri: str, default: float) -> float:
        return self._lengths.get(uri, default)

    async def prefetch_lengths(self, uris: list[str]) -> None:
        uris = [u for u in dict.fromkeys(uris) if u and u not in self._lengths]
        if uris and self.warudo.connected:
            with contextlib.suppress(Exception):
                self._lengths.update(await self.warudo.clip_lengths(uris))

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="animation-director")

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker

    def _clip(self, uri, *, speed=1.0, mask="full", label="", kind="gesture", on_start=None, future=False) -> Clip:
        fut = asyncio.get_running_loop().create_future() if future else None
        return Clip(uri, speed, mask or "full", label, kind, self._now, on_start, fut)

    @property
    def talking(self) -> bool:
        return self._talk is not None

    @property
    def idle(self) -> bool:
        return not self._queue and self._talk is None and self._now >= self._clip_end

    async def wait_idle(self, timeout: float) -> None:
        deadline = self._now + timeout
        while not self.idle and self._now < deadline:
            await asyncio.sleep(0.1)

    # -- API ---------------------------------------------------------------------
    def enqueue(self, uri: str, *, speed: float = 1.0, mask: str = "full", label: str = "",
                on_start: Callable[[], None] | None = None) -> asyncio.Future:
        """Gesto al final de la cola. Future -> True al empezar, False si se descarta."""
        self._ensure_worker()
        clip = self._clip(uri, speed=speed, mask=mask, label=label, on_start=on_start, future=True)
        self._queue.append(clip)
        self._wake.set()
        return clip.started

    def insert_in_talk(self, uri: str, *, speed: float = 1.0, mask: str = "full", label: str = "",
                       on_start: Callable[[], None] | None = None) -> asyncio.Future:
        """Gesto dentro de la gesticulación: en cuanto sea posible (tras el Start pendiente
        o en curso). Un Loop en curso se sustituye con el fundido."""
        if self._talk is None:
            return self.enqueue(uri, speed=speed, mask=mask, label=label, on_start=on_start)
        self._ensure_worker()
        clip = self._clip(uri, speed=speed, mask=mask, label=label, on_start=on_start, future=True)
        # Detrás de los Start aún no reproducidos y de otros gestos ya insertados
        pos = 0
        while pos < len(self._queue) and (
            self._queue[pos].label.endswith(":start") or self._queue[pos].kind == "gesture"
        ):
            pos += 1
        self._queue.insert(pos, clip)
        if (
            pos == 0
            and self._current is not None
            and self._current.label.endswith(":loop")
            and self._clip_end - self._now > PREEMPT_MIN_LEFT_S
        ):
            # El Loop es relleno: el gesto entra ya, fundiéndose sobre él.
            self._busy_until = self._now
        self._wake.set()
        return clip.started

    def start_talking(self, style: TalkStyle) -> None:
        """Empieza a gesticular al hablar: Start y Loops hasta que acabe la voz."""
        self._ensure_worker()
        self._talk = style
        self._talk_deadline = None
        if style.start:
            self._queue.append(self._clip(style.start, speed=style.speed, mask=self.cfg.talk.mask,
                                          label=f"talk:{style.key}:start", kind="talk"))
        self._wake.set()

    def reaction_planned(self) -> None:
        """La reacción ya está decidida (compatibilidad: los Loops ya no esperan)."""
        self._wake.set()

    def set_talk_deadline(self, when: float) -> None:
        """Hora (loop.time) a la que termina la voz."""
        if self._talk is not None:
            self._talk_deadline = when
            self._wake.set()

    def stop_talking(self) -> None:
        """Cierre ordenado: el clip en curso termina entero; si se estaba hablando, va End."""
        style, self._talk = self._talk, None
        self._talk_deadline = None
        if style is None:
            return
        pending_talk = [c for c in self._queue if c.kind == "talk"]
        self._queue = [c for c in self._queue if c.kind != "talk"]
        for c in pending_talk:
            self._resolve(c, False)
        talking_now = self._current is not None and self._current.kind == "talk" and not self._current.label.endswith(":end")
        if style.end and talking_now and not self._queue:
            self._queue.append(self._end_clip(style, 1.0))
        self._wake.set()

    # -- worker --------------------------------------------------------------------
    def _end_clip(self, style: TalkStyle, speed: float) -> Clip:
        return self._clip(style.end, speed=style.speed * speed, mask=self.cfg.talk.mask,
                          label=f"talk:{style.key}:end", kind="talk")

    def _loop_clip(self, s: TalkStyle, speed: float) -> Clip:
        return self._clip(s.uri, speed=s.speed * speed, mask=self.cfg.talk.mask, label=f"talk:{s.key}:loop", kind="talk")

    def _next_talk_clip(self) -> Clip | None:
        """Siguiente clip de hablar según el tiempo de voz que queda (o None si se termina).

        Cada Loop "ocupa" su duración menos el fundido con el siguiente; el End, que es el
        último, se reproduce entero y debe acabar con la voz."""
        s = self._talk
        xf = self._xf
        loop_len = self.length(s.uri, 5.0) / s.speed if s.uri else 0.0
        end_len = self.length(s.end, 2.7) / s.speed if s.end else 0.0
        deadline = self._talk_deadline
        if deadline is None:
            # Aún no se sabe cuánto dura la voz: otra vuelta normal
            return self._loop_clip(s, 1.0) if s.uri else None
        remaining = deadline - self._now
        step = max(loop_len - xf, 0.1)  # tiempo que "avanza" un Loop a velocidad 1
        if s.end:
            if remaining < end_len * 0.5:
                return None  # el gesto ya cerró el turno; un End ahora quedaría fuera del texto
            slack = remaining - end_len
            if not s.uri or slack < loop_len / LOOP_SPEED[1] - xf:
                # No cabe otra vuelta: End ajustado para acabar con el último carácter
                return self._end_clip(s, _clamp(end_len / max(remaining, 0.1), END_SPEED))
            n = max(1, round(slack / step))
            # n Loops que ocupen `slack`: loop_len/speed - xf = slack/n
            return self._loop_clip(s, _clamp(loop_len / (slack / n + xf), LOOP_SPEED))
        # Sin End: Loops mientras quepa al menos media vuelta
        if not s.uri or remaining < step * 0.5:
            return None
        n = max(1, round(remaining / step))
        return self._loop_clip(s, _clamp(loop_len / (remaining / n + xf), LOOP_SPEED))

    @staticmethod
    def _resolve(clip: Clip, value: bool) -> None:
        if clip.started and not clip.started.done():
            clip.started.set_result(value)

    async def _sleep_until_free(self) -> None:
        while (remaining := self._busy_until - self._now) > 0:
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)

    async def _run(self) -> None:
        max_wait = self.cfg.animation.max_wait_ms / 1000
        while True:
            await self._sleep_until_free()
            if not self._queue:
                if self._talk is not None:
                    nxt = self._next_talk_clip()
                    if nxt is None or nxt.label.endswith(":end"):
                        self._talk, self._talk_deadline = None, None  # la gesticulación termina aquí
                    if nxt is not None:
                        self._queue.append(nxt)
                    continue
                self._wake.clear()
                await self._wake.wait()
                continue
            clip = self._queue.pop(0)
            if clip.kind == "gesture" and self._now - clip.enqueued_at > max_wait:
                log.info("Animación descartada por esperar demasiado: %s", clip.label or clip.uri)
                self._resolve(clip, False)
                continue
            if clip.kind == "gesture" and self._talk is not None and self._talk_deadline is not None:
                # Si el gesto se pasaría del final del texto, acelerarlo un poco (máx. x1.3)
                remaining = self._talk_deadline - self._now
                dur = self.length(clip.uri, 0.0) / clip.speed
                if dur > remaining > 0:
                    clip.speed *= min(GESTURE_MAX_SPEEDUP, dur / remaining)
            xf = self._xf
            try:
                reply = await self.warudo.animation(clip.uri, clip.speed, clip.mask, transition=xf)
            except Exception:  # noqa: BLE001
                log.exception("Error enviando animación")
                reply = None
            if not (reply and reply.get("ok")):
                log.info("Warudo no reprodujo %s: %s", clip.label or clip.uri, reply)
                self._resolve(clip, False)
                continue
            duration = (reply.get("durationMs") or 1000) / 1000
            self._lengths.setdefault(clip.uri, duration * clip.speed)
            now = self._now
            self._current = clip
            self._clip_end = now + duration
            # El siguiente empieza `xf` antes del final: fundido clip→clip, sin pasar por el reposo
            self._busy_until = now + max(duration - xf, duration * 0.5)
            log.info("Animación: %s (%.1fs, x%.2f)", clip.label or clip.uri, duration, clip.speed)
            self._resolve(clip, True)
            if clip.on_start:
                try:
                    clip.on_start()
                except Exception:  # noqa: BLE001
                    log.exception("on_start falló")

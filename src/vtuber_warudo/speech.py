"""Muestra la respuesta a ritmo de habla humana.

Gemma genera el texto en 1-3 s; si se muestra tal cual, la gesticulación al hablar
dura un instante y el gesto final llega encima. El SpeechPacer recibe los tokens y
los eventos (herramientas, reacción) en orden y los reenvía al navegador al ritmo
de una persona hablando. Cuando termina, la "voz" ha terminado.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .config import SpeechConfig

_SENTENCE_END = ".!?…"
_COMMA = ",;:"
_TICK = 0.06  # s: agrupa caracteres para no mandar un mensaje por letra


def char_delay(cfg: SpeechConfig, ch: str) -> float:
    d = 1.0 / max(cfg.chars_per_second, 1.0)
    if ch in _SENTENCE_END:
        d += cfg.pause_sentence_ms / 1000
    elif ch in _COMMA:
        d += cfg.pause_comma_ms / 1000
    return d


def speech_seconds(cfg: SpeechConfig, text: str) -> float:
    """Duración estimada de decir `text` en voz alta."""
    return sum(char_delay(cfg, ch) for ch in text)


class SpeechPacer:
    def __init__(self, cfg: SpeechConfig, send: Callable[[dict], Awaitable[None]]):
        self.cfg = cfg
        self.send = send
        self._q: asyncio.Queue = asyncio.Queue()
        self._fed = ""
        self.spoken_chars = 0
        self._clock: float | None = None

    def feed_text(self, text: str) -> None:
        if text:
            self._fed += text
            self._q.put_nowait(("text", text))

    def remaining_seconds(self) -> float:
        """Tiempo que falta para "decir" el texto recibido y aún no mostrado."""
        if not self.cfg.enabled:
            return 0.0
        return speech_seconds(self.cfg, self._fed[self.spoken_chars :])

    def feed_event(self, event: dict) -> None:
        self._q.put_nowait(("event", event))

    def finish(self) -> None:
        self._q.put_nowait(("end", None))

    async def _speak(self, text: str) -> None:
        if not self.cfg.enabled:
            await self.send({"type": "token", "text": text})
            return
        loop = asyncio.get_running_loop()
        # Reloj absoluto: en Windows cada sleep dura algo más de lo pedido y, sumando
        # sleeps relativos, la voz se alargaba ~12 % respecto a lo calculado.
        if self._clock is None or self._clock < loop.time():
            self._clock = loop.time()
        chunk, acc = "", 0.0
        for ch in text:
            chunk += ch
            acc += char_delay(self.cfg, ch)
            if acc >= _TICK:
                await self.send({"type": "token", "text": chunk})
                self.spoken_chars += len(chunk)
                self._clock += acc
                await asyncio.sleep(max(0.0, self._clock - loop.time()))
                chunk, acc = "", 0.0
        if chunk:
            await self.send({"type": "token", "text": chunk})
            self.spoken_chars += len(chunk)
            self._clock += acc
            await asyncio.sleep(max(0.0, self._clock - loop.time()))

    async def run(self) -> None:
        """Consume la cola hasta finish(). Devuelve cuando todo se ha "dicho"."""
        while True:
            kind, payload = await self._q.get()
            if kind == "end":
                return
            if kind == "text":
                await self._speak(payload)
            else:
                await self.send(payload)

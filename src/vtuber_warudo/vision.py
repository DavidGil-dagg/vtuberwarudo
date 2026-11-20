"""Modo visión: el personaje reacciona (solo con el cuerpo) a tu pantalla y a lo que dices.

Bucle:
1. Cada ``interval_s`` segundos, o en cuanto Whisper termina de transcribir una frase tuya,
   captura la pantalla.
2. Pregunta a StartLux, en una sola petición con la captura: ¿merece reaccionar ahora? (sí/no),
   y qué emoción, gesto y pose de mano.
3. Si la probabilidad de reaccionar supera el umbral (más bajo cuando hablas) y ha pasado el
   ``cooldown_s``, aplica la reacción en Warudo (expresión + gesto + mano a la vez).

No interrumpe nada: se salta mientras hay un turno de chat en curso o un gesto sonando.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from collections.abc import Awaitable, Callable

from .avatar import Avatar
from .config import Config
from .decision import Decision, Question, YesNo
from .listen import MicListener
from .reaction import options_of, reaction_questions

log = logging.getLogger(__name__)


def capture_screen(monitor: int, max_side: int, quality: int) -> str:
    """Captura la pantalla y la devuelve como data URI JPEG (lado mayor <= max_side)."""
    return capture_screen_with_thumb(monitor, max_side, quality)[0]


THUMB = (64, 36)


def grab_screen(monitor: int):
    """(imagen PIL a resolución completa, miniatura 64x36 en gris 0..1 para detectar cambios)."""
    import mss
    import numpy as np
    from PIL import Image

    with mss.MSS() if hasattr(mss, "MSS") else mss.mss() as s:
        mons = s.monitors
        shot = s.grab(mons[monitor] if 0 <= monitor < len(mons) else mons[1])
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    thumb = np.asarray(img.convert("L").resize(THUMB), dtype=np.float32) / 255.0
    return img, thumb


def encode_jpeg(img, max_side: int, quality: int) -> str:
    img = img.copy()
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def capture_screen_with_thumb(monitor: int, max_side: int, quality: int):
    """(data URI JPEG de la pantalla entera, miniatura para detectar cambios)."""
    img, thumb = grab_screen(monitor)
    return encode_jpeg(img, max_side, quality), thumb


def motion_box(a, b, img_size: tuple[int, int], margin: float = 0.06):
    """Caja (x0, y0, x1, y1) en píxeles de la zona que cambió entre dos miniaturas, con margen.
    None si no hay referencia, no cambió nada o cambió casi toda la pantalla."""
    if a is None or b is None:
        return None
    import numpy as np

    mask = np.abs(a - b) > PIXEL_CHANGED
    if mask.mean() == 0 or mask.mean() > 0.6:
        return None
    ys, xs = np.nonzero(mask)
    tw, th = THUMB
    W, H = img_size
    x0 = max(0.0, xs.min() / tw - margin)
    x1 = min(1.0, (xs.max() + 1) / tw + margin)
    y0 = max(0.0, ys.min() / th - margin)
    y1 = min(1.0, (ys.max() + 1) / th + margin)
    if (x1 - x0) * (y1 - y0) > 0.7:  # casi toda la pantalla: mejor la captura completa
        return None
    return int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)


PIXEL_CHANGED = 0.08  # diferencia de brillo (0..1) a partir de la cual un píxel "cambió"


def scene_change(a, b) -> float:
    """Fracción de la pantalla (0..1) que cambió entre dos miniaturas; 1.0 si no hay referencia.

    Se mide área cambiada y no diferencia media: un vídeo que ocupa el 20 % de la pantalla
    apenas mueve la media (~0.02), pero su área cambiada es ~0.2, mientras que un reloj o una
    línea de texto nueva no pasan de ~0.01."""
    if a is None or b is None:
        return 1.0
    import numpy as np

    return float(np.mean(np.abs(a - b) > PIXEL_CHANGED))


class VisionMode:
    def __init__(
        self,
        cfg: Config,
        decision,
        avatar: Avatar,
        emit: Callable[[dict], Awaitable[None]],
        is_busy: Callable[[], bool],
    ):
        self.cfg = cfg
        self.decision = decision
        self.avatar = avatar
        self.emit = emit
        self.is_busy = is_busy
        self._task: asyncio.Task | None = None
        self._mic: MicListener | None = None
        self._meter: asyncio.Task | None = None
        self._heard: list[str] = []
        self._wake = asyncio.Event()
        self._last_reaction_at = 0.0
        self._last_reaction = "none"
        self._last_thumb = None  # miniatura de la última captura analizada
        self._focused = False  # la última imagen era solo la zona en movimiento
        self._last_gesture: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "type": "vision_status",
            "enabled": self.running,
            "mic": self._mic is not None and self._mic.error is None,
            "mic_error": self._mic.error if self._mic else None,
        }

    async def start(self) -> dict:
        if self.running:
            return self.status()
        if not getattr(self.decision, "supports_images", False):
            raise RuntimeError("El modo visión necesita [decision] backend = \"startlux\"")
        loop = asyncio.get_running_loop()
        if self.cfg.vision.mic_enabled:
            self._mic = MicListener(self.cfg.vision, self._on_heard, loop, on_unclear=self._on_unclear)
            try:
                self._mic.start()
                self._meter = asyncio.create_task(self._meter_loop(), name="mic-meter")
            except Exception as exc:  # noqa: BLE001 - sin micro sigue funcionando con la pantalla
                log.warning("Micrófono no disponible: %s", exc)
                self._mic.error = str(exc)
        self._task = asyncio.create_task(self._run(), name="vision-mode")
        st = self.status()
        await self.emit(st)
        return st

    async def stop(self) -> dict:
        if self._meter:
            self._meter.cancel()
            self._meter = None
        if self._mic:
            self._mic.stop()
            self._mic = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        st = self.status()
        await self.emit(st)
        return st

    async def _meter_loop(self) -> None:
        """Medidor de micrófono para la UI (4 veces por segundo)."""
        while self._mic is not None:
            await asyncio.sleep(0.25)
            mic = self._mic
            if mic is None:
                break
            await self.emit({"type": "vision_level", "level": round(mic.take_peak(), 4),
                             "threshold": round(mic.threshold, 4)})

    def _on_unclear(self, seconds: float) -> None:
        asyncio.get_running_loop().create_task(self.emit({"type": "vision_unclear", "seconds": round(seconds, 1)}))

    def _on_heard(self, text: str) -> None:
        self._heard.append(text)
        asyncio.get_running_loop().create_task(self.emit({"type": "vision_heard", "text": text}))
        self._wake.set()

    # -- bucle ---------------------------------------------------------------------
    def _state(self, heard: str) -> str:
        v = self.cfg.vision
        ago = int(time.monotonic() - self._last_reaction_at) if self._last_reaction_at else None
        last = f"{self._last_reaction} ({ago} s ago)" if ago is not None else "none yet"
        where = (
            "the part of the streamer's screen where something is moving (e.g. a video)"
            if self._focused else "the streamer's screen"
        )
        return (
            f"{self.cfg.persona.name}, a VTuber character, is watching {where} live: <image>\n"
            f"What the streamer just said out loud: {heard if heard else '(nothing)'}\n"
            f"The character's previous reaction: {last}\n"
            f"Language of the stream: {v.language or 'unknown'}"
        )

    async def _run(self) -> None:
        v = self.cfg.vision
        while True:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=v.interval_s)
            except TimeoutError:
                pass
            if self.is_busy() or not self.avatar.director.idle:
                continue  # hay un turno de chat o un gesto sonando: no interrumpir
            heard = " ".join(self._heard).strip()
            self._heard.clear()
            try:
                await self._tick(heard)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Error en el modo visión")

    async def _tick(self, heard: str) -> None:
        v = self.cfg.vision
        t0 = time.perf_counter()
        img, thumb = await asyncio.to_thread(grab_screen, v.monitor)
        change = scene_change(thumb, self._last_thumb)
        if not heard and change < v.scene_change_threshold:
            # La pantalla apenas ha cambiado desde la última vez que se miró y no has hablado:
            # no hay nada nuevo a lo que reaccionar (ni siquiera se consulta a la GPU).
            await self.emit({"type": "vision_tick", "reason": "static", "change": round(change, 3), "reacted": False})
            return
        # Enfocar: si solo se mueve una parte (un vídeo en la web), se envía esa zona ampliada.
        box = motion_box(self._last_thumb, thumb, img.size) if v.focus_motion else None
        self._last_thumb = thumb
        image = await asyncio.to_thread(encode_jpeg, img.crop(box) if box else img, v.max_side, v.jpeg_quality)
        self._focused = box is not None
        qs: dict[str, Question] = {"react": Question(v.react_question, kind="yesno")}
        qs.update(reaction_questions(
            self.cfg, emotion_q=v.emotion_question, gesture_q=v.gesture_question, hand_q=v.hand_question,
        ))
        # Si decide reaccionar, que se VEA: sin "neutral" ni "quedarse quieta" entre las opciones
        # (con ellas las elige casi siempre, y una emoción sola no se nota si el personaje no
        # tiene expresiones configuradas en Warudo).
        # Tampoco repetir el gesto anterior (si no, elige "unsure" una y otra vez).
        for qid, banned in (("emotion", {"neutral"}), ("gesture", {"none", self._last_gesture})):
            if qid in qs:
                opts = {k: l for k, l in qs[qid].options.items() if k not in banned}
                if len(opts) >= 2:
                    qs[qid] = Question(qs[qid].instructions, opts)
        answers = await self.decision.decide_many(self._state(heard), qs, [image])
        react = answers.get("react")
        p = react.p_yes if isinstance(react, YesNo) else 0.0
        emo, ges, hand = (answers.get(k) for k in ("emotion", "gesture", "hand"))
        emo_key = emo.option if isinstance(emo, Decision) else None
        ges_key = ges.option if isinstance(ges, Decision) else None
        hand_key = hand.option if isinstance(hand, Decision) else None

        threshold = v.speech_threshold if heard else v.react_threshold
        cooled = time.monotonic() - self._last_reaction_at >= v.cooldown_s
        # Neutral + sin gesto + sin mano no movería nada: no cuenta como reacción
        visible = (ges_key not in (None, "none")) or (hand_key not in (None, "none")) or (emo_key not in (None, "neutral"))
        do_react = visible and p >= threshold and (cooled or bool(heard))
        if do_react:
            self.avatar.plan_reaction(emo_key, ges_key, hand_key)
            self._last_reaction_at = time.monotonic()
            self._last_reaction = f"{emo_key}/{ges_key}"
            self._last_gesture = ges_key
        ms = int((time.perf_counter() - t0) * 1000)
        log.info(
            "Visión: cambio=%.3f p=%.2f%s emoción=%s gesto=%s mano=%s (%d ms)%s",
            change, p, " (hablaste)" if heard else "", emo_key, ges_key, hand_key, ms, " → REACCIONA" if do_react else "",
        )
        reason = "react" if do_react else ("low" if p < threshold else "nothing" if not visible else "cooldown")
        await self.emit({
            "type": "vision_tick", "p": round(p, 2), "threshold": threshold, "reacted": do_react, "reason": reason,
            "heard": heard, "emotion": emo_key, "gesture": ges_key, "hand": hand_key, "ms": ms,
        })


__all__ = ["VisionMode", "capture_screen", "options_of"]

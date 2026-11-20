"""Micrófono → frases transcritas (Whisper en CPU).

Un hilo de audio (sounddevice) detecta voz por energía: una frase empieza cuando el
volumen supera ``energy_threshold`` y termina tras ``silence_ms`` de silencio (o al
llegar a ``max_phrase_s``). Cada frase se transcribe con faster-whisper en otro hilo
y el texto llega al bucle asyncio con ``on_text``.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from .config import PROJECT_ROOT, VisionConfig

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_MS = 30
PREROLL_BLOCKS = 10  # 300 ms antes de detectar voz
NOISE_WINDOW_BLOCKS = 200  # ~6 s para estimar el ruido de fondo


def resolve_input_device(spec: str | int | None) -> int | None:
    """"" -> predeterminado; número -> ese índice; texto -> primer micrófono cuyo nombre lo contiene.

    Un mismo micro aparece varias veces (una por API de audio de Windows); se elige la primera,
    que es la de MME y acepta 16 kHz sin problemas."""
    if spec in (None, ""):
        return None
    if isinstance(spec, int) or str(spec).isdigit():
        return int(spec)
    import sounddevice as sd

    needle = str(spec).lower()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and needle in d["name"].lower():
            return i
    raise ValueError(f"No hay ningún micrófono que contenga «{spec}» (mira `uv run vtuber-cli mics`)")


class MicListener:
    def __init__(
        self,
        cfg: VisionConfig,
        on_text: Callable[[str], None],
        loop: asyncio.AbstractEventLoop,
        on_unclear: Callable[[float], None] | None = None,
    ):
        self.cfg = cfg
        self.on_text = on_text
        self.on_unclear = on_unclear  # se detectó sonido pero Whisper no entendió palabras (duración s)
        self.loop = loop
        self.peak_level = 0.0  # máximo RMS desde la última lectura (para el medidor de la UI)
        self._phrases: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stream = None
        self.level = 0.0  # último RMS (para mostrar en la UI)
        self.error: str | None = None

    # -- ciclo de vida -----------------------------------------------------------
    def start(self) -> None:
        import sounddevice as sd

        self._stop.clear()
        device = resolve_input_device(self.cfg.mic_device)
        block = int(SAMPLE_RATE * BLOCK_MS / 1000)
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block, device=device,
            callback=self._on_audio,
        )
        self._buf: list[np.ndarray] = []
        self._in_phrase = False
        self._silence_blocks = 0
        self._preroll: deque[np.ndarray] = deque(maxlen=PREROLL_BLOCKS)
        self._recent_rms: deque[float] = deque(maxlen=NOISE_WINDOW_BLOCKS)
        self._noise = 0.0
        self._n = 0
        self._stream.start()
        t = threading.Thread(target=self._transcribe_worker, name="whisper", daemon=True)
        t.start()
        self._threads = [t]
        log.info("Micrófono escuchando (%s)", self._stream.device)

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        self._phrases.put(None)

    # -- detección de voz (hilo de audio) ------------------------------------------
    @property
    def threshold(self) -> float:
        """Umbral para empezar una frase: el mínimo de config o 4× el ruido de fondo medido."""
        return max(self.cfg.energy_threshold, self._noise * 4)

    def _on_audio(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block * block)))
        self.level = rms
        self.peak_level = max(self.peak_level, rms)
        # Ruido de fondo: percentil 20 de los últimos ~6 s (se actualiza cada 0,6 s)
        self._recent_rms.append(rms)
        self._n += 1
        if self._n % 20 == 0 and len(self._recent_rms) >= 20:
            self._noise = float(np.percentile(self._recent_rms, 20))
        start_thr = self.threshold
        keep_thr = start_thr * 0.5  # histéresis: dentro de una frase, las sílabas suaves también cuentan
        silence_blocks_needed = max(1, self.cfg.silence_ms // BLOCK_MS)
        max_blocks = int(self.cfg.max_phrase_s * 1000 / BLOCK_MS)
        if not self._in_phrase:
            if rms >= start_thr:
                # Empieza una frase: incluir los 300 ms previos para no cortar la primera sílaba
                self._in_phrase = True
                self._silence_blocks = 0
                self._buf = [*self._preroll, block]
                self._preroll.clear()
            else:
                self._preroll.append(block)
                return
        else:
            self._buf.append(block)
            if rms >= keep_thr:
                self._silence_blocks = 0
            else:
                self._silence_blocks += 1
        if self._in_phrase and (self._silence_blocks >= silence_blocks_needed or len(self._buf) >= max_blocks):
            audio = np.concatenate(self._buf)
            self._buf, self._in_phrase, self._silence_blocks = [], False, 0
            if len(audio) >= SAMPLE_RATE * 0.4:  # descartar golpes cortos
                self._phrases.put(audio)

    # -- transcripción (hilo propio) -------------------------------------------------
    def _transcribe_worker(self) -> None:
        try:
            from faster_whisper import WhisperModel

            t0 = time.perf_counter()
            model = WhisperModel(
                self.cfg.whisper_model, device="cpu", compute_type="int8",
                download_root=str(PROJECT_ROOT / "models" / "whisper"),
            )
            log.info("Whisper '%s' cargado en %.1fs", self.cfg.whisper_model, time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001
            self.error = f"No se pudo cargar Whisper: {exc}"
            log.exception(self.error)
            return
        while not self._stop.is_set():
            audio = self._phrases.get()
            if audio is None:
                break
            # Normalizar volumen: micros con poca ganancia dan picos de 0.01 y Whisper los
            # trataría como silencio.
            peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
            if 0 < peak < 0.5:
                audio = (audio * min(0.9 / peak, 50.0)).astype(np.float32)
            try:
                t0 = time.perf_counter()
                segments, _ = model.transcribe(
                    audio, language=self.cfg.language or None, beam_size=1, vad_filter=False,
                    condition_on_previous_text=False,
                )
                text = " ".join(s.text.strip() for s in segments).strip()
                log.info("Transcrito (%.1fs audio, %.1fs): %s", len(audio) / SAMPLE_RATE, time.perf_counter() - t0, text)
            except Exception:  # noqa: BLE001
                log.exception("Error transcribiendo")
                continue
            if text and not _is_hallucination(text):
                self.loop.call_soon_threadsafe(self.on_text, text)
            elif self.on_unclear:
                self.loop.call_soon_threadsafe(self.on_unclear, len(audio) / SAMPLE_RATE)

    def take_peak(self) -> float:
        """Máximo nivel desde la última llamada (y lo reinicia)."""
        p, self.peak_level = self.peak_level, 0.0
        return p


# Frases que Whisper suele inventar con ruido o silencio
_HALLUCINATIONS = {
    "subtítulos realizados por la comunidad de amara.org",
    "suscríbete", "¡suscríbete!", "gracias por ver el video.", "...",
}


def _is_hallucination(text: str) -> bool:
    t = text.strip().lower()
    return t in _HALLUCINATIONS or "amara.org" in t or len(t) < 2

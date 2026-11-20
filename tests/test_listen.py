from collections import deque

import numpy as np

from vtuber_warudo import listen
from vtuber_warudo.config import VisionConfig
from vtuber_warudo.listen import BLOCK_MS, SAMPLE_RATE, MicListener

BLOCK = int(SAMPLE_RATE * BLOCK_MS / 1000)
rng = np.random.default_rng(0)


def block(rms: float) -> np.ndarray:
    return (rng.standard_normal(BLOCK) * rms).astype(np.float32).reshape(-1, 1)


def make() -> MicListener:
    m = MicListener(VisionConfig(energy_threshold=0.004, silence_ms=600), on_text=lambda t: None, loop=None)
    m._buf, m._in_phrase, m._silence_blocks = [], False, 0
    m._preroll = deque(maxlen=listen.PREROLL_BLOCKS)
    m._recent_rms = deque(maxlen=listen.NOISE_WINDOW_BLOCKS)
    m._noise, m._n = 0.0, 0
    return m


def feed(m: MicListener, rms: float, seconds: float) -> None:
    for _ in range(int(seconds * 1000 / BLOCK_MS)):
        m._on_audio(block(rms), BLOCK, None, None)


def test_quiet_mic_phrase_is_one_piece_with_preroll():
    m = make()
    feed(m, 0.0005, 2.0)  # ruido de fondo
    # frase de un micro flojo (como la FaceCam): sílabas fuertes y suaves alternadas
    for _ in range(6):
        feed(m, 0.012, 0.25)
        feed(m, 0.003, 0.15)  # sílaba suave: por debajo del umbral de inicio
    feed(m, 0.0005, 1.0)  # silencio: cierra la frase
    phrases = []
    while not m._phrases.empty():
        phrases.append(m._phrases.get())
    assert len(phrases) == 1  # no se trocea en las sílabas suaves
    dur = len(phrases[0]) / SAMPLE_RATE
    assert 2.4 + 0.25 <= dur <= 2.4 + 0.3 + 0.7  # frase + pre-roll + silencio de cierre


def test_threshold_adapts_to_noisy_room():
    m = make()
    feed(m, 0.003, 3.0)  # sala ruidosa: el ruido supera el mínimo de config
    assert m.threshold > 0.01  # 4× el ruido, no 0.004
    assert m._phrases.empty()  # el ruido no se toma como voz

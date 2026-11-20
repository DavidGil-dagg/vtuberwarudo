import time

from vtuber_warudo.config import SpeechConfig
from vtuber_warudo.speech import SpeechPacer, speech_seconds

CFG = SpeechConfig(enabled=True, chars_per_second=100, pause_comma_ms=50, pause_sentence_ms=100)


def test_speech_seconds_adds_punctuation_pauses():
    assert abs(speech_seconds(CFG, "abcd") - 0.04) < 1e-9
    assert abs(speech_seconds(CFG, "a, b.") - (0.05 + 0.05 + 0.1)) < 1e-9


async def test_pacer_keeps_order_and_pace():
    out: list[dict] = []

    async def send(d):
        out.append(d)

    p = SpeechPacer(CFG, send)
    p.feed_text("Hola, ")
    p.feed_event({"type": "tool", "name": "perform_action"})
    p.feed_text("¡vamos allá!")
    p.feed_event({"type": "reaction"})
    p.finish()
    t0 = time.perf_counter()
    await p.run()
    elapsed = time.perf_counter() - t0

    text = "".join(d["text"] for d in out if d["type"] == "token")
    assert text == "Hola, ¡vamos allá!"
    kinds = [d["type"] for d in out if d["type"] != "token"]
    assert kinds == ["tool", "reaction"]
    # la herramienta aparece después de "Hola, " y antes de "¡vamos..."
    idx_tool = next(i for i, d in enumerate(out) if d["type"] == "tool")
    assert "".join(d["text"] for d in out[:idx_tool] if d["type"] == "token") == "Hola, "
    assert elapsed >= speech_seconds(CFG, "Hola, ¡vamos allá!") * 0.9
    assert p.remaining_seconds() == 0


async def test_pacer_disabled_sends_immediately():
    out = []

    async def send(d):
        out.append(d)

    p = SpeechPacer(SpeechConfig(enabled=False), send)
    p.feed_text("x" * 500)
    p.finish()
    t0 = time.perf_counter()
    await p.run()
    assert time.perf_counter() - t0 < 0.1
    assert out == [{"type": "token", "text": "x" * 500}]

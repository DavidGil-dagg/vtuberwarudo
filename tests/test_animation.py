import asyncio
import time

import vtuber_warudo.animation as anim
from vtuber_warudo.animation import AnimationDirector
from vtuber_warudo.config import TalkStyle, load_config

# duraciones (s) a velocidad 1
DUR = {"start": 0.1, "loop": 0.2, "end": 0.15, "g1": 0.3, "g2": 0.1}


class FakeWarudo:
    connected = True

    def __init__(self):
        self.played: list[tuple[float, str, float]] = []  # (inicio, uri, duración real)
        self.transitions: list[float | None] = []
        self.t0 = time.perf_counter()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    async def animation(self, uri, speed=1.0, mask="full", transition=None):
        dur = DUR[uri] / speed
        self.played.append((self.now(), uri, dur))
        self.transitions.append(transition)
        return {"ok": True, "durationMs": dur * 1000}

    async def clip_lengths(self, uris):
        return {u: DUR[u] for u in uris if u in DUR}

    def uris(self):
        return [u for _, u, _ in self.played]


async def make(crossfade_ms=0, max_wait_ms=10000):
    cfg = load_config()
    cfg.animation.crossfade_ms = crossfade_ms
    cfg.animation.max_wait_ms = max_wait_ms
    w = FakeWarudo()
    d = AnimationDirector(cfg, w)
    await d.prefetch_lengths(list(DUR))
    return d, w


STYLE = TalkStyle(key="normal", label="", uri="loop", start="start", end="end")


def assert_chained(w: FakeWarudo, xf=0.0, tol=0.03, allow_preempt=()):
    """Cada clip empieza al acabar el anterior (menos el fundido): ni solapes de más ni huecos."""
    for (t1, u1, d1), (t2, u2, _) in zip(w.played, w.played[1:]):
        if u1 in allow_preempt:
            continue
        assert t2 >= t1 + d1 - xf - tol, f"{u2} cortó a {u1}"


async def test_clips_chain_with_crossfade_without_idle_gaps():
    d, w = await make(crossfade_ms=50)
    d.start_talking(STYLE)
    d.insert_in_talk("g1")
    d.set_talk_deadline(asyncio.get_running_loop().time() + 1.0)
    await d.wait_idle(3)
    assert all(t == 0.05 for t in w.transitions)  # Warudo funde con el mismo tiempo
    for (t1, u1, d1), (t2, u2, _) in zip(w.played, w.played[1:]):
        gap = t2 - (t1 + d1)
        assert -0.08 < gap < 0.0, f"entre {u1} y {u2}: {gap:+.3f}s (debe solaparse ~50 ms, sin hueco)"
    await d.close()


async def test_gesture_goes_right_after_start_inside_the_talk():
    d, w = await make()
    d.start_talking(STYLE)
    d.insert_in_talk("g1")
    d.set_talk_deadline(asyncio.get_running_loop().time() + 1.0)
    await d.wait_idle(3)
    uris = w.uris()
    assert uris[:2] == ["start", "g1"]  # el gesto va al principio, no al final
    assert uris[-1] == "end"
    assert "loop" in uris  # los loops rellenan entre el gesto y el End
    assert_chained(w)
    await d.close()


async def test_late_gesture_replaces_the_filler_loop(monkeypatch):
    monkeypatch.setattr(anim, "PREEMPT_MIN_LEFT_S", 0.05)
    d, w = await make()
    d.start_talking(STYLE)
    await asyncio.sleep(0.15)  # Start (0.1 s) terminó: suena un Loop mientras Gemma genera
    assert w.uris() == ["start", "loop"]
    t_insert = w.now()
    d.insert_in_talk("g1")
    d.set_talk_deadline(asyncio.get_running_loop().time() + 0.6)
    await d.wait_idle(3)
    t_g1 = next(t for t, u, _ in w.played if u == "g1")
    assert t_g1 - t_insert < 0.05  # entra ya (fundido sobre el Loop), sin esperar a que acabe
    assert_chained(w, allow_preempt=("loop",))
    await d.close()


async def test_start_and_gestures_are_never_preempted(monkeypatch):
    monkeypatch.setattr(anim, "PREEMPT_MIN_LEFT_S", 0.0)
    d, w = await make()
    d.enqueue("g1")  # 0.3 s
    await asyncio.sleep(0.02)
    d.start_talking(STYLE)
    d.insert_in_talk("g2")
    await asyncio.sleep(0.05)
    d.set_talk_deadline(asyncio.get_running_loop().time() + 0.6)
    await d.wait_idle(3)
    assert w.uris()[:3] == ["g1", "start", "g2"]
    assert_chained(w, allow_preempt=("loop",))
    await d.close()


async def test_end_finishes_with_the_speech():
    d, w = await make(crossfade_ms=30)
    t_deadline = w.now() + 1.0
    d.start_talking(STYLE)
    d.insert_in_talk("g1")
    d.set_talk_deadline(asyncio.get_running_loop().time() + 1.0)
    await d.wait_idle(3)
    t_end, uri, dur_end = w.played[-1]
    assert uri == "end"
    assert abs((t_end + dur_end) - t_deadline) < 0.12  # el End acaba con el último carácter
    await d.close()


async def test_short_speech_gesture_closes_the_turn_without_end():
    d, w = await make()
    d.start_talking(STYLE)
    d.insert_in_talk("g1")
    d.set_talk_deadline(asyncio.get_running_loop().time() + 0.35)
    await d.wait_idle(2)
    # start(0.1) + g1(0.3) ya cubren la voz: nada después del texto
    assert w.uris() == ["start", "g1"]
    await d.close()


async def test_gesture_longer_than_remaining_speech_is_sped_up():
    d, w = await make()
    d.start_talking(STYLE)
    d.insert_in_talk("g1")  # 0.3 s
    d.set_talk_deadline(asyncio.get_running_loop().time() + 0.33)  # tras Start quedan ~0.23 s
    await d.wait_idle(2)
    _, uri, dur = w.played[1]
    assert uri == "g1" and dur < 0.3  # acelerado (máx. x1.3)
    await d.close()


async def test_long_speech_uses_whole_loops():
    d, w = await make()
    d.start_talking(STYLE)
    d.set_talk_deadline(asyncio.get_running_loop().time() + 1.2)
    await d.wait_idle(3)
    uris = w.uris()
    assert uris[0] == "start" and uris[-1] == "end"
    assert uris.count("loop") >= 3
    assert_chained(w)
    await d.close()


async def test_gesture_waits_for_previous_gesture():
    d, w = await make()
    d.enqueue("g1")
    await asyncio.sleep(0.02)
    await d.enqueue("g2")
    assert_chained(w)
    await d.close()


async def test_stale_gesture_is_dropped():
    d, w = await make(max_wait_ms=100)
    d.enqueue("g1")  # 300 ms
    await asyncio.sleep(0.02)
    assert await d.enqueue("g2") is False
    await d.close()


async def test_stop_without_talk_playing_adds_no_end():
    d, w = await make()
    d.enqueue("g1")
    await asyncio.sleep(0.02)
    d.start_talking(STYLE)  # queda en cola detrás de g1
    d.stop_talking()  # se cancela antes de empezar: ni start ni end
    await asyncio.sleep(0.4)
    assert w.uris() == ["g1"]
    await d.close()


async def test_on_start_runs_when_gesture_starts():
    d, w = await make()
    started_at: list[float] = []
    d.start_talking(STYLE)
    d.insert_in_talk("g1", on_start=lambda: started_at.append(w.now()))
    d.set_talk_deadline(asyncio.get_running_loop().time() + 0.4)
    await d.wait_idle(2)
    t_g1 = next(t for t, u, _ in w.played if u == "g1")
    assert started_at and abs(started_at[0] - t_g1) < 0.03  # cara y mano a la vez que el gesto
    await d.close()

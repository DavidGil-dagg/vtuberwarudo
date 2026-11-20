import numpy as np

import vtuber_warudo.vision as vision
from vtuber_warudo.config import load_config
from vtuber_warudo.decision import Decision, YesNo
from vtuber_warudo.vision import VisionMode, scene_change


class FakeDecision:
    supports_images = True

    def __init__(self, p_yes=0.9):
        self.p_yes = p_yes
        self.calls = 0
        self.states: list[str] = []

    async def decide_many(self, state, questions, images=None):
        self.calls += 1
        self.states.append(state)
        out = {}
        for qid, q in questions.items():
            out[qid] = YesNo(self.p_yes, 5, True) if q.kind == "yesno" else Decision(1, list(q.options)[1], "", 5, True)
        return out


class FakeDirector:
    idle = True


class FakeAvatar:
    def __init__(self):
        self.director = FakeDirector()
        self.planned: list[tuple] = []

    def plan_reaction(self, emotion, gesture, hand):
        self.planned.append((emotion, gesture, hand))
        return {"gesture": True}


def make(monkeypatch, frames, p_yes=0.9):
    from PIL import Image

    it = iter(frames)
    monkeypatch.setattr(vision, "grab_screen", lambda *a: (Image.new("RGB", (1920, 1080)), next(it)))
    monkeypatch.setattr(vision, "encode_jpeg", lambda img, *a: f"data:image/jpeg;base64,{img.size[0]}x{img.size[1]}")
    events: list[dict] = []

    async def emit(e):
        events.append(e)

    cfg = load_config()
    cfg.vision.cooldown_s = 0
    dec, av = FakeDecision(p_yes), FakeAvatar()
    return VisionMode(cfg, dec, av, emit=emit, is_busy=lambda: False), dec, av, events


def test_scene_change_is_changed_area():
    a = np.zeros((36, 64), np.float32)
    assert scene_change(a, None) == 1.0
    assert scene_change(a, a) == 0.0
    video = a.copy()
    video[8:28, 24:40] = 0.6  # un vídeo en el centro (~14 % de la pantalla)
    assert 0.12 < scene_change(a, video) < 0.16
    clock = a.copy()
    clock[34:36, 60:64] = 0.9  # el reloj de la barra de tareas
    assert scene_change(a, clock) < 0.01
    assert scene_change(a, a + 0.02) == 0.0  # ruido de compresión/brillo: no cuenta


def test_motion_box_crops_a_video_but_not_a_fullscreen_game():
    from vtuber_warudo.vision import motion_box

    a = np.zeros((36, 64), np.float32)
    video = a.copy()
    video[8:28, 24:40] = 0.6  # vídeo en el centro
    x0, y0, x1, y1 = motion_box(a, video, (1920, 1080))
    assert 0.25 < (x1 - x0) / 1920 < 0.45 and 0.5 < (y1 - y0) / 1080 < 0.8  # recorte en torno al vídeo
    assert motion_box(a, a + 0.6, (1920, 1080)) is None  # juego a pantalla completa: captura entera
    assert motion_box(None, video, (1920, 1080)) is None


async def test_moving_video_is_sent_cropped(monkeypatch):
    a = np.zeros((36, 64), np.float32)
    video = a.copy()
    video[8:28, 24:40] = 0.6
    vm, dec, av, events = make(monkeypatch, [a, video])
    images = []

    async def capture(state, questions, imgs=None):
        images.extend(imgs or [])
        return await FakeDecision.decide_many(dec, state, questions, imgs)

    dec.decide_many = capture
    await vm._tick("")  # primera: pantalla completa
    await vm._tick("")  # segunda: solo la zona del vídeo
    assert images[0].endswith("1920x1080")
    assert not images[1].endswith("1920x1080")
    assert "where something is moving" in dec.states[-1]


async def test_static_screen_does_not_repeat_reactions(monkeypatch):
    same = np.zeros((36, 64), np.float32)
    vm, dec, av, events = make(monkeypatch, [same, same, same])
    for _ in range(3):
        await vm._tick("")
    assert dec.calls == 1  # solo la primera vez se consulta al modelo
    assert len(av.planned) == 1
    assert [e.get("reason") for e in events] == ["react", "static", "static"]


async def test_speech_always_gets_a_look_even_if_screen_is_static(monkeypatch):
    same = np.zeros((36, 64), np.float32)
    vm, dec, av, events = make(monkeypatch, [same, same], p_yes=0.45)
    await vm._tick("")  # p=0.45 < 0.5 (pantalla): no reacciona
    await vm._tick("¡he ganado!")  # p=0.45 >= 0.35 (voz): reacciona
    assert dec.calls == 2
    assert len(av.planned) == 1
    assert "¡he ganado!" in dec.states[-1]


async def test_vision_reactions_always_move_the_body(monkeypatch):
    a = np.zeros((36, 64), np.float32)
    vm, dec, av, events = make(monkeypatch, [a])
    seen = {}

    async def capture(state, questions, images=None):
        seen.update(questions)
        return await FakeDecision.decide_many(dec, state, questions, images)

    dec.decide_many = capture
    await vm._tick("")
    assert "none" not in seen["gesture"].options  # siempre hay gesto si reacciona
    assert "neutral" not in seen["emotion"].options


async def test_changed_screen_is_looked_at_again(monkeypatch):
    a = np.zeros((36, 64), np.float32)
    vm, dec, av, events = make(monkeypatch, [a, a + 0.2])
    await vm._tick("")
    await vm._tick("")
    assert dec.calls == 2

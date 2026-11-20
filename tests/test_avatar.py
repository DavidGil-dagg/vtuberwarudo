from vtuber_warudo.avatar import resolve_expression

VRM0 = ["Joy", "Angry", "Sorrow", "Fun", "Surprised"]
VRM1 = ["happy", "angry", "sad", "relaxed", "surprised"]


def test_vrm1_names_work_on_vrm0_expressions():
    assert resolve_expression("happy", VRM0) == "Joy"
    assert resolve_expression("sad", VRM0) == "Sorrow"
    assert resolve_expression("relaxed", VRM0) == "Fun"
    assert resolve_expression("angry", VRM0) == "Angry"


def test_vrm0_names_work_on_vrm1_expressions():
    assert resolve_expression("Joy", VRM1) == "happy"
    assert resolve_expression("Sorrow", VRM1) == "sad"
    assert resolve_expression("Surprised", VRM1) == "surprised"


def test_custom_and_missing_expressions():
    assert resolve_expression("Blush", ["blush", "Joy"]) == "blush"  # nombre propio, sin distinguir mayúsculas
    assert resolve_expression("happy", []) is None
    assert resolve_expression("", VRM0) is None

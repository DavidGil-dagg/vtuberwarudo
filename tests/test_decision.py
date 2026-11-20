import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from vtuber_warudo.config import load_config
from vtuber_warudo.decision import HEADER, build_prompt, clip_state, parse_letter
from vtuber_warudo.reaction import _last_turn


def test_build_prompt_exact_format():
    p = build_prompt("The film was excellent.", "What is the sentiment of this review?", ["negative", "positive"])
    assert p == (
        f"{HEADER}\n\n"
        "[State]\nThe film was excellent.\n\n"
        "[Question]\nWhat is the sentiment of this review?\n\n"
        "[Options]\nA. negative\nB. positive\n\n"
        "Answer:"
    )


@pytest.mark.parametrize("opts", [["a"], [str(i) for i in range(27)], ["x", "x"]])
def test_build_prompt_rejects_bad_options(opts):
    with pytest.raises(ValueError):
        build_prompt("s", "q", opts)


@pytest.mark.parametrize(
    ("raw", "n", "expected"),
    [(" C", 5, 2), ("A", 2, 0), ("b.", 3, 1), (" (D)", 4, 3), (" E", 4, None), ("", 3, None), (" Answer", 3, None), ("7", 3, None)],
)
def test_parse_letter(raw, n, expected):
    assert parse_letter(raw, n) == expected


def test_clip_state_keeps_end():
    s = "x" * 50 + "END"
    out = clip_state(s, 10)
    assert len(out) == 10 and out.endswith("END") and out.startswith("…")


def test_last_turn_detects_tools_and_final_reply():
    msgs = [
        HumanMessage("hola"),
        AIMessage("¡hola!"),
        HumanMessage("baila"),
        AIMessage("", tool_calls=[{"name": "perform_action", "args": {"action": "dance"}, "id": "1"}]),
        ToolMessage("ok", tool_call_id="1", name="perform_action"),
        AIMessage("¡Allá voy!"),
    ]
    human, reply, used = _last_turn(msgs)
    assert human.text == "baila"
    assert reply.text == "¡Allá voy!"
    assert used == {"perform_action"}


def test_config_loads_and_orders_options():
    cfg = load_config()
    assert list(cfg.emotions)[0] == "neutral"
    assert list(cfg.gestures)[0] == "none"
    assert 2 <= len(cfg.gestures) <= 26

"""Cliente del modelo de decisión Jev-Style (clasificador por letra).

El modelo se entrenó con un prompt crudo (sin chat template):

    You are a decision function. Read the state, then answer the question by choosing exactly one option.

    [State]
    ...

    [Question]
    ...

    [Options]
    A. ...
    B. ...

    Answer:

y responde con la letra de la opción elegida.
"""

from __future__ import annotations

import asyncio
import logging
import re
import string
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

HEADER = "You are a decision function. Read the state, then answer the question by choosing exactly one option."
LETTERS = string.ascii_uppercase
_LETTER_RE = re.compile(r"^\s*\(?([A-Za-z])(?![A-Za-z])")


def clip_state(state: str, max_chars: int) -> str:
    """Recorta el estado conservando el final (lo más reciente)."""
    state = state.strip()
    if len(state) <= max_chars:
        return state
    return "…" + state[-(max_chars - 1):]


def build_prompt(state: str, question: str, options: list[str]) -> str:
    if not 2 <= len(options) <= len(LETTERS):
        raise ValueError(f"Se necesitan entre 2 y 26 opciones, hay {len(options)}")
    if len(set(options)) != len(options):
        raise ValueError("Las opciones deben ser únicas")
    opts = "\n".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(options))
    return "\n\n".join(
        [HEADER, f"[State]\n{state.strip()}", f"[Question]\n{question.strip()}", f"[Options]\n{opts}", "Answer:"]
    )


def parse_letter(text: str, n_options: int) -> int | None:
    """Devuelve el índice de la opción elegida o None si la salida no es válida."""
    m = _LETTER_RE.match(text or "")
    if not m:
        return None
    idx = LETTERS.index(m.group(1).upper())
    return idx if idx < n_options else None


@dataclass
class Decision:
    index: int
    option: str
    raw: str
    ms: int
    valid: bool
    error: str | None = None
    confidence: float | None = None  # probabilidad de la opción elegida (StartLux)


@dataclass
class Question:
    """Pregunta de decisión. kind="choice": options {clave: descripción}. kind="yesno": sin opciones."""

    instructions: str
    options: dict[str, str] | None = None
    kind: str = "choice"


@dataclass
class YesNo:
    p_yes: float
    ms: int
    valid: bool
    error: str | None = None


class DecisionClient:
    """Modelo jev-style (LM Studio /v1/completions, una letra por pregunta)."""

    supports_images = False
    def __init__(self, base_url: str, model: str, timeout_s: float = 30.0):
        self.model = model
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def decide(self, state: str, question: str, options: list[str], default_index: int = 0) -> Decision:
        prompt = build_prompt(state, question, options)
        t0 = time.perf_counter()
        try:
            r = await self._http.post(
                "/completions",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "max_tokens": 1,
                    "temperature": 0,
                    "stream": False,
                },
            )
            r.raise_for_status()
            raw = r.json()["choices"][0].get("text", "")
        except Exception as exc:  # noqa: BLE001 - nunca romper el chat por una decisión
            ms = int((time.perf_counter() - t0) * 1000)
            log.warning("Decisión fallida (%s): %s", question, exc)
            return Decision(default_index, options[default_index], "", ms, False, str(exc))
        ms = int((time.perf_counter() - t0) * 1000)
        idx = parse_letter(raw, len(options))
        if idx is None:
            log.info("Salida no válida del modelo de decisión: %r", raw)
            return Decision(default_index, options[default_index], raw, ms, False)
        return Decision(idx, options[idx], raw, ms, True)

    async def decide_many(
        self, state: str, questions: dict[str, Question], images: list[str] | None = None
    ) -> dict[str, Decision | YesNo]:
        """Varias preguntas sobre el mismo estado (en paralelo). jev no admite imágenes."""

        async def one(q: Question):
            if q.kind == "yesno":
                d = await self.decide(state, q.instructions, ["yes", "no"])
                return YesNo(1.0 if d.index == 0 else 0.0, d.ms, d.valid, d.error)
            keys = list(q.options)
            d = await self.decide(state, q.instructions, [q.options[k] for k in keys])
            return Decision(d.index, keys[d.index], d.raw, d.ms, d.valid, d.error)

        results = await asyncio.gather(*(one(q) for q in questions.values()))
        return dict(zip(questions, results))


def make_decision_client(cfg) -> "DecisionClient | StartLuxClient":
    """Cliente según [decision] backend de config.toml."""
    if cfg.decision.backend == "jev":
        return DecisionClient(cfg.lmstudio.base_url, cfg.lmstudio.decision_model, cfg.lmstudio.timeout_s)
    return StartLuxClient(cfg.decision.startlux_url, cfg.lmstudio.timeout_s)


class StartLuxClient:
    """StartLux Decision (servidor startlux-decide, API /v1/systemone). Admite capturas.

    Todas las preguntas van en una sola petición; el servidor lee cada una como su propio
    prompt con el estado completo y devuelve probabilidades por opción."""

    supports_images = True

    def __init__(self, base_url: str, timeout_s: float = 30.0):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def healthy(self) -> bool:
        try:
            return (await self._http.get("/health", timeout=2)).status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def decide_many(
        self, state: str, questions: dict[str, Question], images: list[str] | None = None
    ) -> dict[str, Decision | YesNo]:
        body: dict = {"state": state, "questions": {}}
        for qid, q in questions.items():
            if q.kind == "yesno":
                body["questions"][qid] = {"type": "noul", "instructions": q.instructions}
            else:
                body["questions"][qid] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.options)}
        if images:
            body["images"] = images
        t0 = time.perf_counter()
        try:
            r = await self._http.post("/v1/systemone", json=body)
            r.raise_for_status()
            answers = r.json()["answers"]
            error = None
        except Exception as exc:  # noqa: BLE001 - nunca romper el chat por una decisión
            answers, error = {}, f"{type(exc).__name__}: {exc}"
            log.warning("StartLux falló: %s", error)
        ms = int((time.perf_counter() - t0) * 1000)

        out: dict[str, Decision | YesNo] = {}
        for qid, q in questions.items():
            a = answers.get(qid) or {}
            if q.kind == "yesno":
                p = a.get("noul")
                out[qid] = YesNo(float(p) if p is not None else 0.0, ms, p is not None, error)
                continue
            keys = list(q.options)
            choice = a.get("choice")
            if choice in keys:
                out[qid] = Decision(keys.index(choice), choice, str(choice), ms, True, None, a.get("confidence"))
            else:
                out[qid] = Decision(0, keys[0], str(choice), ms, False, error)
        return out

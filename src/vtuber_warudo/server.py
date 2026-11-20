"""Servidor FastAPI: sirve la UI y conecta el chat con el Deep Agent."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .agent import build_agent
from .avatar import Avatar
from .config import PROJECT_ROOT, Config, load_config
from .decision import make_decision_client
from .speech import SpeechPacer
from .vision import VisionMode
from .warudo import WarudoClient

log = logging.getLogger("vtuber_warudo")
WEB_DIR = PROJECT_ROOT / "web"


@dataclass
class AppState:
    cfg: Config
    warudo: WarudoClient
    avatar: Avatar
    decision: object  # DecisionClient | StartLuxClient
    agent: object
    vision: VisionMode | None = None
    turns_active: int = 0  # turnos de chat en curso (el modo visión espera)
    sockets: set[WebSocket] = field(default_factory=set)

    async def broadcast(self, data: dict) -> None:
        for ws in list(self.sockets):
            await _safe_send(ws, data)


async def lmstudio_ok(cfg: Config) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get(cfg.lmstudio.base_url.rstrip("/") + "/models")
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()

    def on_warudo_status(connected: bool) -> None:
        st: AppState | None = getattr(app.state, "vt", None)
        if connected:
            # Duración real de los clips: la coreografía del turno la necesita
            loop.create_task(avatar.prefetch())
        if st:
            for ws in list(st.sockets):
                loop.create_task(_safe_send(ws, {"type": "status", "warudo": connected}))

    warudo = WarudoClient(cfg.warudo.url, on_status=on_warudo_status)
    avatar = Avatar(cfg, warudo)
    await warudo.start()
    decision = make_decision_client(cfg)

    async with AsyncSqliteSaver.from_conn_string(str(cfg.data_dir / "checkpoints.sqlite")) as saver:
        agent = build_agent(cfg, avatar, decision, checkpointer=saver)
        st = AppState(cfg, warudo, avatar, decision, agent)
        st.vision = VisionMode(cfg, decision, avatar, emit=st.broadcast, is_busy=lambda: st.turns_active > 0)
        app.state.vt = st
        log.info("Listo en http://%s:%s (decisiones: %s)", cfg.server.host, cfg.server.port, cfg.decision.backend)
        yield
        await st.vision.stop()

    await avatar.director.close()
    await warudo.close()
    await decision.aclose()


app = FastAPI(title="VtuberWarudo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/status")
async def status():
    st: AppState = app.state.vt
    return {"lmstudio": await lmstudio_ok(st.cfg), "warudo": st.warudo.connected, "name": st.cfg.persona.name}


@app.get("/api/vision")
async def vision_status():
    st: AppState = app.state.vt
    return st.vision.status()


@app.post("/api/vision")
async def vision_toggle(body: dict):
    """{"enabled": true|false} — enciende o apaga el modo visión (pantalla + micrófono)."""
    st: AppState = app.state.vt
    try:
        return await (st.vision.start() if body.get("enabled") else st.vision.stop())
    except Exception as exc:  # noqa: BLE001
        log.warning("Modo visión: %s", exc)
        return {"type": "vision_status", "enabled": False, "error": str(exc)}


@app.get("/api/history")
async def history(thread: str):
    st: AppState = app.state.vt
    snap = await st.agent.aget_state({"configurable": {"thread_id": thread}})
    out = []
    for m in (snap.values or {}).get("messages", []):
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "text": m.text})
        elif isinstance(m, AIMessage) and not m.tool_calls and m.text.strip():
            out.append({"role": "assistant", "text": m.text})
    return {"messages": out}


async def _safe_send(ws: WebSocket, data: dict) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(data)


async def run_turn(st: AppState, ws: WebSocket, text: str, thread: str) -> None:
    if st.cfg.reaction.on_user_message:
        await st.avatar.gesture(st.cfg.reaction.on_user_message)

    config = {"configurable": {"thread_id": thread}}
    inputs = {"messages": [{"role": "user", "content": text}]}

    # El texto se reenvía al navegador a ritmo de habla; la gesticulación dura lo
    # mismo y el gesto final espera a speech_finished().
    pacer = SpeechPacer(st.cfg.speech, lambda d: _safe_send(ws, d))
    st.avatar.event_sink = lambda d: _safe_send(ws, d)  # "reaction_applied" al empezar el gesto
    st.turns_active += 1  # el modo visión no reacciona mientras se habla
    speaking = asyncio.create_task(pacer.run())
    try:
        async for mode, data in st.agent.astream(inputs, config=config, stream_mode=["messages", "updates", "custom"]):
            if mode == "messages":
                token, meta = data
                if (
                    isinstance(token, AIMessageChunk)
                    and meta.get("langgraph_node") == "model"
                    and not token.tool_call_chunks
                    and token.text
                ):
                    pacer.feed_text(token.text)
            elif mode == "updates":
                for update in (data or {}).values():
                    if not isinstance(update, dict):
                        continue
                    msgs = update.get("messages") or []
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    for m in msgs:
                        if isinstance(m, ToolMessage):
                            pacer.feed_event({"type": "tool", "name": m.name, "content": m.text})
                        elif isinstance(m, AIMessage) and m.tool_calls:
                            # separa el texto previo a una tool call del texto final
                            pacer.feed_event({"type": "segment"})
            elif mode == "custom" and isinstance(data, dict):
                # "talk" y "reaction" van ya: la coreografía ocurre durante el texto
                await _safe_send(ws, data)
        # Gemma ya terminó; la "voz" aún no. Avisar cuándo acaba para cuadrar la gesticulación.
        remaining = pacer.remaining_seconds()
        log.info("Voz: faltan %.1fs por mostrar", remaining)
        st.avatar.speech_will_end_in(remaining)
    except Exception as exc:  # noqa: BLE001
        log.exception("Error en el turno")
        hint = "" if await lmstudio_ok(st.cfg) else " (¿está LM Studio abierto con el servidor activo?)"
        pacer.feed_event({"type": "error", "text": f"{type(exc).__name__}: {exc}{hint}"})
    finally:
        pacer.finish()
        with contextlib.suppress(Exception):
            await speaking
        log.info("Voz: terminada")
        st.avatar.speech_finished()  # normalmente ya cerró sola (End ajustado al final del texto)
        # El chat no se desbloquea hasta que termine la última animación: así el siguiente
        # mensaje no la pisa.
        await st.avatar.director.wait_idle(timeout=15)
        st.avatar.event_sink = None
        st.turns_active -= 1
        await _safe_send(ws, {"type": "done"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    st: AppState = app.state.vt
    st.sockets.add(ws)
    await _safe_send(
        ws, {"type": "status", "warudo": st.warudo.connected, "lmstudio": await lmstudio_ok(st.cfg), "name": st.cfg.persona.name}
    )
    await _safe_send(ws, {**st.vision.status(), "available": getattr(st.decision, "supports_images", False)})
    busy = asyncio.Lock()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "user" and (text := str(msg.get("text", "")).strip()):
                thread = str(msg.get("thread") or "default")
                if busy.locked():
                    await ws.send_json({"type": "error", "text": "Espera a que termine la respuesta anterior."})
                    continue
                async with busy:
                    await run_turn(st, ws, text, thread)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - p.ej. WebSocketDisconnected al recargar la página
        log.debug("WebSocket cerrado: %s", exc)
    finally:
        st.sockets.discard(ws)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    uvicorn.run("vtuber_warudo.server:app", host=cfg.server.host, port=cfg.server.port, log_level="info")


if __name__ == "__main__":
    main()

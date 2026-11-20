"""Cliente WebSocket hacia el asset "AI Chat Bridge" (script Playground en Warudo).

Protocolo: un JSON por mensaje, p.ej. {"cmd":"expression","name":"Joy","holdMs":4000,"id":3}.
El bridge responde {"ok":true|false,"id":3,...}.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Callable

import websockets

log = logging.getLogger(__name__)


class WarudoClient:
    def __init__(self, url: str, on_status: Callable[[bool], None] | None = None):
        self.url = url
        self._ws = None
        self._task: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._on_status = on_status
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="warudo-client")

    async def wait_connected(self, timeout: float = 3.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.connected and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        return self.connected

    async def close(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def _set_ws(self, ws) -> None:
        changed = (ws is None) != (self._ws is None)
        self._ws = ws
        if changed and self._on_status:
            try:
                self._on_status(ws is not None)
            except Exception:  # noqa: BLE001
                log.exception("on_status falló")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                async with websockets.connect(self.url, open_timeout=3, ping_interval=20) as ws:
                    log.info("Conectado a Warudo en %s", self.url)
                    self._set_ws(ws)
                    backoff = 1.0
                    async for raw in ws:
                        self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("Warudo no disponible (%s); reintento en %.0fs", exc, backoff)
            finally:
                if self._ws is not None:
                    log.info("Desconectado de Warudo")
                self._set_ws(None)
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_result(None)
                self._pending.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    def _on_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            log.debug("Mensaje no JSON de Warudo: %r", raw)
            return
        if not msg.get("ok", True):
            log.warning("Warudo respondió error: %s", msg)
        fut = self._pending.pop(msg.get("id"), None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def send(self, cmd: dict, wait: bool = False, timeout: float = 3.0) -> dict | None:
        """Envía un comando. Si Warudo no está conectado devuelve None sin fallar."""
        ws = self._ws
        if ws is None:
            log.debug("Warudo desconectado; se descarta %s", cmd)
            return None
        msg_id = next(self._ids)
        payload = {**cmd, "id": msg_id}
        fut: asyncio.Future | None = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self._pending[msg_id] = fut
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo enviar a Warudo: %s", exc)
            self._pending.pop(msg_id, None)
            return None
        if fut is None:
            return {"ok": True, "sent": True}
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            return None

    # Atajos -----------------------------------------------------------------
    # Esperan la respuesta del bridge (1 frame) para saber si Warudo lo aplicó de verdad.
    ACK_TIMEOUT = 1.5

    async def expression(self, name: str, hold_ms: int = 0) -> dict | None:
        return await self.send({"cmd": "expression", "name": name, "holdMs": hold_ms}, wait=True, timeout=self.ACK_TIMEOUT)

    async def neutral(self) -> dict | None:
        return await self.send({"cmd": "expression", "name": "", "holdMs": 0}, wait=True, timeout=self.ACK_TIMEOUT)

    async def animation(self, uri: str, speed: float = 1.0, mask: str = "full", transition: float | None = None) -> dict | None:
        cmd = {"cmd": "animation", "uri": uri, "speed": speed, "mask": mask or "full"}
        if transition is not None:
            cmd["transition"] = transition
        return await self.send(cmd, wait=True, timeout=self.ACK_TIMEOUT)

    async def hand_pose(self, hand: str, pose: str, weight: float = 1.0, hold_ms: int = 0) -> dict | None:
        return await self.send(
            {"cmd": "handPose", "hand": hand, "pose": pose, "weight": weight, "holdMs": hold_ms},
            wait=True,
            timeout=self.ACK_TIMEOUT,
        )

    async def look_at(self, mode: str) -> dict | None:
        return await self.send({"cmd": "lookAt", "mode": mode}, wait=True, timeout=self.ACK_TIMEOUT)

    async def clip_lengths(self, uris: list[str]) -> dict[str, float]:
        """Duración (s) de cada clip, sin reproducirlo. Los que no existen no aparecen."""
        r = await self.send({"cmd": "clipInfo", "uris": uris}, wait=True, timeout=30)
        clips = (r or {}).get("clips") or {}
        return {u: c["lengthMs"] / 1000 for u, c in clips.items() if c}

    async def info(self) -> dict | None:
        return await self.send({"cmd": "info"}, wait=True)

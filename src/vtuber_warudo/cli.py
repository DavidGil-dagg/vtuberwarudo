"""Herramientas de línea de comandos para probar cada pieza por separado.

    uv run vtuber-cli decisions            # prueba el modelo de decisión con diálogos de ejemplo
    uv run vtuber-cli info                 # lista las expresiones del personaje en Warudo
    uv run vtuber-cli animations [filtro]  # lista las animaciones disponibles (URIs)
    uv run vtuber-cli send expression Joy  # envía un comando a Warudo
    uv run vtuber-cli send animation <uri> [speed]
    uv run vtuber-cli send look camera|away|default
    uv run vtuber-cli send emotion happy   # usa el mapa de config.toml
    uv run vtuber-cli send gesture wave
    uv run vtuber-cli send hand peace      # pose de dedos ([hand_poses])
    uv run vtuber-cli send talk excited    # brazos al hablar ([talk_styles])
    uv run vtuber-cli handposes            # lista las poses de mano de Warudo
    uv run vtuber-cli chat                 # chat por terminal con el agente completo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid

from .avatar import Avatar
from .config import load_config
from .decision import make_decision_client
from .reaction import decide_reaction, decide_talk_style
from .warudo import WarudoClient

SAMPLES: list[tuple[str, str]] = [
    ("¡Hola! ¿Qué tal estás hoy?", "¡Hola, hola! Estoy genial, muchas gracias por pasarte por aquí."),
    ("Adiós, me voy a dormir ya.", "¡Buenas noches! Que descanses mucho, ¡nos vemos mañana!"),
    ("¿Te gusta el anime?", "¡Sí, me encanta! Sobre todo los de aventuras y fantasía."),
    ("Cuéntame un chiste.", "¿Por qué el libro de matemáticas está triste? ¡Porque tiene demasiados problemas! Jajaja."),
    ("Hoy murió mi perro...", "Lo siento muchísimo... Perder a un compañero así duele mucho. Estoy aquí contigo."),
    ("Eres la peor streamer del mundo.", "¡Oye! Eso no ha sido nada amable. No pienso aguantar insultos."),
    ("¡He aprobado el examen!", "¡¿En serio?! ¡Felicidades! ¡Sabía que lo conseguirías!"),
    ("¿Cuánto es la raíz cuadrada de 1764?", "Mmm, déjame pensar... creo que es 42."),
    ("¿Quieres casarte conmigo?", "¿Q-qué? ¡No digas esas cosas tan de repente! Me da mucha vergüenza..."),
    ("Baila un poco para mí.", "¡Vale, vale! Allá va mi mejor baile."),
    ("Gracias por el stream de hoy.", "¡Gracias a ti por acompañarme! De verdad, os lo agradezco muchísimo."),
    ("¿Crees que lloverá mañana?", "No tengo ni idea, la verdad. ¡Quizás sí, quizás no!"),
    ("Mira, ¡un gato con sombrero!", "¡¿Qué?! ¡Qué cosa más adorable! ¡No me lo puedo creer!"),
    ("¿Estás de acuerdo en que la pizza con piña es buena?", "Sí, totalmente de acuerdo. ¡La piña en la pizza está riquísima!"),
    ("No me gusta tu peinado.", "No, no y no. Mi peinado es perfecto, gracias."),
]


async def cmd_decisions(cfg) -> None:
    decision = make_decision_client(cfg)
    total = 0
    print(f"{'usuario':<38} {'al hablar':<11} {'emoción':<10} {'gesto':<11} {'mano':<10} ms")
    print("-" * 92)
    for user, reply in SAMPLES:
        talk = await decide_talk_style(cfg, decision, user)
        r = await decide_reaction(cfg, decision, user, reply)
        decs = [c.decision for c in (talk, r.emotion, r.gesture, r.hand) if c.decision]
        ms = (talk.decision.ms if talk.decision else 0) + max(d.ms for d in decs)
        total += ms
        bad = [d.raw for d in decs if not d.valid]
        flag = f"  (!) salida inválida: {bad!r}" if bad else ""
        print(f"{user[:37]:<38} {talk.key or '-':<11} {r.emotion.key:<10} {r.gesture.key:<11} {r.hand.key or '-':<10} {ms}{flag}")
    print("-" * 92)
    print(f"media por turno: {total // len(SAMPLES)} ms (estilo al hablar + 3 preguntas en paralelo)")
    await decision.aclose()


async def _with_warudo(cfg, fn):
    w = WarudoClient(cfg.warudo.url)
    await w.start()
    if not await w.wait_connected(4):
        print(f"No se pudo conectar a Warudo en {cfg.warudo.url}. ¿Está abierto con el asset 'AI Chat Bridge' activo?")
        await w.close()
        sys.exit(1)
    try:
        return await fn(w)
    finally:
        await w.close()


async def cmd_info(cfg) -> None:
    async def run(w: WarudoClient):
        print(json.dumps(await w.info(), indent=2, ensure_ascii=False))

    await _with_warudo(cfg, run)


async def cmd_animations(cfg, filt: str, out: str | None, rtype: str = "CharacterAnimation") -> None:
    async def run(w: WarudoClient):
        res = await w.send({"cmd": "listResources", "type": rtype, "filter": filt}, wait=True, timeout=15)
        if not res or not res.get("ok"):
            print("Error:", res)
            return
        anims = res.get("animations", [])
        for a in anims:
            print(f"{a.get('category') or '':<28} {a.get('label') or '':<40} {a['uri']}")
        print(f"\n{len(anims)} resultados")
        if out:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(anims, f, ensure_ascii=False, indent=1)
            print(f"Guardado en {out}")

    await _with_warudo(cfg, run)


async def cmd_send(cfg, what: str, args: list[str]) -> None:
    async def run(w: WarudoClient):
        av = Avatar(cfg, w)
        if what == "expression":
            name = args[0] if args else ""
            res = await w.send({"cmd": "expression", "name": name, "holdMs": cfg.warudo.expression_hold_ms}, wait=True)
        elif what == "animation":
            res = await w.send({"cmd": "animation", "uri": args[0], "speed": float(args[1]) if len(args) > 1 else 1.0}, wait=True)
        elif what == "look":
            res = await w.send({"cmd": "lookAt", "mode": args[0]}, wait=True)
        elif what == "idle":
            res = await w.send({"cmd": "idle", "uri": args[0]}, wait=True)
        elif what == "emotion":
            res = await av.emote(args[0])
        elif what == "gesture":
            res = await av.gesture(args[0], wait_start=True)
        elif what == "hand":
            res = await av.hand_pose(args[0])
        elif what == "talk":
            res = await av.talk_once(args[0])
        else:
            res = await w.send(json.loads(" ".join([what, *args])), wait=True)
        print(res)
        await asyncio.sleep(0.3)

    await _with_warudo(cfg, run)


def cmd_mics() -> None:
    import sounddevice as sd

    default_in = sd.default.device[0]
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            mark = "*" if i == default_in else " "
            print(f"{mark} {i:>3}  {d['name']}  ({int(d['default_samplerate'])} Hz)")
    print("\n* = predeterminado. Pon el número en [vision] mic_device de config.toml.")


async def cmd_mic_test(cfg, seconds: float) -> None:
    from .listen import MicListener

    loop = asyncio.get_running_loop()
    heard: list[str] = []
    mic = MicListener(cfg.vision, heard.append, loop)
    mic.start()
    print(f"Habla durante {seconds:.0f} s (Whisper '{cfg.vision.whisper_model}' en CPU; la 1ª vez se descarga)...")
    t_end = loop.time() + seconds
    peak = 0.0
    while loop.time() < t_end:
        peak = max(peak, mic.level)
        await asyncio.sleep(0.1)
    print(f"Volumen máximo: {peak:.3f} (umbral de voz: {cfg.vision.energy_threshold})")
    print("Esperando a que termine la transcripción...")
    await asyncio.sleep(cfg.vision.silence_ms / 1000 + 0.5)
    mic.stop()
    for _ in range(100):  # hasta 10 s más
        await asyncio.sleep(0.1)
        if not mic._phrases.qsize():
            break
    await asyncio.sleep(3)
    print("Transcrito:" if heard else "No se transcribió nada.", *[f"  «{h}»" for h in heard], sep="\n")
    if mic.error:
        print("Error:", mic.error)


async def cmd_vision_test(cfg, heard: str) -> None:
    import time

    from .decision import Decision, Question, YesNo
    from .reaction import reaction_questions
    from .vision import capture_screen

    v = cfg.vision
    decision = make_decision_client(cfg)
    if not getattr(decision, "supports_images", False):
        print('El modo visión necesita [decision] backend = "startlux".')
        return
    t0 = time.perf_counter()
    img = capture_screen(v.monitor, v.max_side, v.jpeg_quality)
    t1 = time.perf_counter()
    qs = {"react": Question(v.react_question, kind="yesno")}
    qs.update(reaction_questions(cfg, emotion_q=v.emotion_question, gesture_q=v.gesture_question, hand_q=v.hand_question))
    for qid, banned in (("emotion", "neutral"), ("gesture", "none")):
        if qid in qs:
            qs[qid] = Question(qs[qid].instructions, {k: l for k, l in qs[qid].options.items() if k != banned})
    state = (f"{cfg.persona.name}, a VTuber character, is watching the streamer's screen live: <image>\n"
             f"What the streamer just said out loud: {heard or '(nothing)'}")
    ans = await decision.decide_many(state, qs, [img])
    t2 = time.perf_counter()
    for k, a in ans.items():
        if isinstance(a, YesNo):
            print(f"{k:<8} P(sí)={a.p_yes:.2f}")
        elif isinstance(a, Decision):
            print(f"{k:<8} {a.option:<12} conf={a.confidence if a.confidence is not None else '?'}")
    print(f"captura {int((t1 - t0) * 1000)} ms, decisión {int((t2 - t1) * 1000)} ms")
    await decision.aclose()


async def cmd_chat(cfg) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langchain_core.messages import AIMessageChunk

    from .agent import build_agent

    warudo = WarudoClient(cfg.warudo.url)
    await warudo.start()
    avatar = Avatar(cfg, warudo)
    decision = make_decision_client(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    thread = f"cli-{uuid.uuid4().hex[:8]}"
    async with AsyncSqliteSaver.from_conn_string(str(cfg.data_dir / "checkpoints.sqlite")) as saver:
        agent = build_agent(cfg, avatar, decision, checkpointer=saver)
        print(f"Chat con {cfg.persona.name} (hilo {thread}). Escribe 'salir' para terminar.")
        while True:
            text = (await asyncio.to_thread(input, "\nTú: ")).strip()
            if text.lower() in {"salir", "exit", "quit"}:
                break
            if not text:
                continue
            print(f"{cfg.persona.name}: ", end="", flush=True)
            async for mode, data in agent.astream(
                {"messages": [{"role": "user", "content": text}]},
                config={"configurable": {"thread_id": thread}},
                stream_mode=["messages", "custom", "updates"],
            ):
                if mode == "messages":
                    tok, meta = data
                    if isinstance(tok, AIMessageChunk) and meta.get("langgraph_node") == "model" and tok.text:
                        print(tok.text, end="", flush=True)
                elif mode == "custom" and data.get("type") == "talk":
                    print(f"[hablando: {data.get('style')}] ", end="", flush=True)
                elif mode == "custom":
                    print(f"\n  [reacción] {data.get('emotion')} / {data.get('gesture')} / mano {data.get('hand')} "
                          f"({data.get('ms')} ms, warudo={data.get('applied')})", end="")
                elif mode == "updates":
                    for upd in (data or {}).values():
                        for m in (upd or {}).get("messages", []) if isinstance(upd, dict) else []:
                            if getattr(m, "type", "") == "tool":
                                print(f"\n  [tool {m.name}] {m.text}", end="")
            print()
    await warudo.close()
    await decision.aclose()


def main() -> None:
    p = argparse.ArgumentParser(prog="vtuber-cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("decisions")
    sub.add_parser("info")
    an = sub.add_parser("animations", help="lista las animaciones de Warudo (con filtro opcional)")
    an.add_argument("filter", nargs="?", default="")
    an.add_argument("-o", "--out", help="guarda la lista en un JSON")
    hp = sub.add_parser("handposes", help="lista las poses de mano de Warudo")
    hp.add_argument("filter", nargs="?", default="")
    sub.add_parser("mics", help="lista los micrófonos")
    mt = sub.add_parser("mic-test", help="graba y transcribe con Whisper")
    mt.add_argument("seconds", nargs="?", type=float, default=8)
    vt = sub.add_parser("vision-test", help="una decisión del modo visión con la pantalla actual")
    vt.add_argument("heard", nargs="?", default="", help="texto como si lo hubieras dicho")
    s = sub.add_parser("send")
    s.add_argument("what")
    s.add_argument("args", nargs="*")
    sub.add_parser("chat")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING)
    cfg = load_config()
    if a.cmd == "decisions":
        asyncio.run(cmd_decisions(cfg))
    elif a.cmd == "info":
        asyncio.run(cmd_info(cfg))
    elif a.cmd == "animations":
        asyncio.run(cmd_animations(cfg, a.filter, a.out))
    elif a.cmd == "handposes":
        asyncio.run(cmd_animations(cfg, a.filter, None, "CharacterHandPose"))
    elif a.cmd == "mics":
        cmd_mics()
    elif a.cmd == "mic-test":
        asyncio.run(cmd_mic_test(cfg, a.seconds))
    elif a.cmd == "vision-test":
        asyncio.run(cmd_vision_test(cfg, a.heard))
    elif a.cmd == "send":
        asyncio.run(cmd_send(cfg, a.what, a.args))
    elif a.cmd == "chat":
        asyncio.run(cmd_chat(cfg))


if __name__ == "__main__":
    main()

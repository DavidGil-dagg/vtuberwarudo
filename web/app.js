"use strict";

const EMOJI = {
  neutral: "😐", happy: "😊", fun: "😆", sad: "😢", angry: "😠", surprised: "😲",
  shy: "😳",
  none: "", wave: "👋", nod: "🙂‍↕️", shake_head: "🙂‍↔️", think: "🤔", explain: "🤲", laugh: "😂",
  unsure: "🤷", bow: "🙇", cheer: "🎉", cute: "🥰", point: "👉", dance: "💃",
};
const HAND_EMOJI = {
  peace: "✌️", thumbs_up: "👍", ok: "👌", point: "☝️", fist: "✊", open: "🖐️", rock: "🤘",
};

const params = new URLSearchParams(location.search);
if (params.get("overlay")) document.body.classList.add("overlay");

const $log = document.getElementById("log");
const $input = document.getElementById("input");
const $send = document.getElementById("send");
const $form = document.getElementById("composer");
const $name = document.getElementById("name");
const $stLm = document.getElementById("st-lm");
const $stWarudo = document.getElementById("st-warudo");

function storageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function storageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

function newThreadId() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
}
let thread = params.get("thread") || storageGet("vt-thread") || newThreadId();
storageSet("vt-thread", thread);

let ws = null;
const $visionBtn = document.getElementById("vision-btn");
const $visionPanel = document.getElementById("vision-panel");
const $visionLog = document.getElementById("vision-log");
const $visionMic = document.getElementById("vision-mic");
const $meterFill = document.getElementById("meter-fill");
const $meterThr = document.getElementById("meter-thr");
let visionOn = false;

function visionLog(text, cls) {
  const li = document.createElement("li");
  if (cls) li.className = cls;
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const s = document.createElement("span");
  s.textContent = text;
  li.append(t, s);
  $visionLog.prepend(li);
  while ($visionLog.children.length > 30) $visionLog.lastChild.remove();
}

$visionBtn.addEventListener("click", async () => {
  $visionBtn.disabled = true;
  try {
    const r = await fetch("/api/vision", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !visionOn }),
    });
    handle(await r.json());
  } catch (e) {
    console.error(e);
  } finally {
    $visionBtn.disabled = false;
  }
});

let current = null;   // {el, bubble, tags, text}
let reactionTags = {}; // part -> <span> de la última reacción
let busy = false;

function setPill(el, ok) {
  if (ok === undefined) return;
  el.classList.toggle("ok", !!ok);
  el.classList.toggle("bad", !ok);
}

function scrollDown() { $log.scrollTop = $log.scrollHeight; }

function clearEmpty() { const e = $log.querySelector(".empty"); if (e) e.remove(); }

function showEmpty() {
  if ($log.children.length) return;
  const d = document.createElement("div");
  d.className = "empty";
  d.textContent = "Di hola para empezar la conversación ✨";
  $log.appendChild(d);
}

function addMessage(role, text = "") {
  clearEmpty();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  const tags = document.createElement("div");
  tags.className = "tags";
  el.append(bubble, tags);
  $log.appendChild(el);
  scrollDown();
  return { el, bubble, tags, text };
}

function addTag(msg, text, dim = false) {
  if (!msg) return null;
  const t = document.createElement("span");
  t.className = "tag" + (dim ? " dim" : "");
  t.textContent = text;
  msg.tags.appendChild(t);
  scrollDown();
  return t;
}

function setBusy(b) {
  busy = b;
  $send.disabled = b;
}

function ensureBot() {
  if (!current) {
    current = addMessage("bot");
    current.bubble.classList.add("typing");
  }
  return current;
}

function handle(msg) {
  switch (msg.type) {
    case "status":
      if (msg.name) { $name.textContent = msg.name; document.title = `${msg.name} · Chat`; }
      setPill($stLm, msg.lmstudio);
      setPill($stWarudo, msg.warudo);
      break;
    case "token": {
      const m = ensureBot();
      m.text += msg.text;
      m.bubble.textContent = m.text.replace(/^\s+/, "");
      scrollDown();
      break;
    }
    case "segment":
      // el modelo va a llamar una herramienta: el texto que venga después es la respuesta final
      if (current && !current.text.trim()) break;
      if (current) { current.bubble.classList.remove("typing"); current = null; }
      break;
    case "tool": {
      const m = ensureBot();
      addTag(m, `🛠 ${msg.name}`);
      break;
    }
    case "talk": {
      const m = ensureBot();
      addTag(m, `🗣️ ${msg.style}`, !msg.applied);
      break;
    }
    case "reaction": {
      // Lo decidido; se ejecuta durante el texto. "reaction_applied" marca lo que Warudo no aplicó.
      const m = current || [...$log.querySelectorAll(".msg.bot")].map(e => ({ tags: e.querySelector(".tags") })).pop();
      reactionTags = {};
      if (msg.emotion) reactionTags.emotion = addTag(m, `${EMOJI[msg.emotion] || ""} ${msg.emotion}`.trim());
      if (msg.gesture && msg.gesture !== "none") reactionTags.gesture = addTag(m, `${EMOJI[msg.gesture] || ""} ${msg.gesture}`.trim());
      if (msg.hand && msg.hand !== "none") reactionTags.hand = addTag(m, `${HAND_EMOJI[msg.hand] || "✋"} ${msg.hand}`);
      if (m && m.tags) m.tags.title = `decisión en ${msg.ms} ms · las etiquetas atenuadas no se aplicaron en Warudo`;
      break;
    }
    case "vision_status": {
      if (msg.available !== undefined) $visionBtn.hidden = !msg.available;
      visionOn = !!msg.enabled;
      $visionBtn.classList.toggle("on", visionOn);
      $visionBtn.textContent = visionOn ? "👁 Visión activa" : "👁 Modo visión";
      $visionPanel.hidden = !visionOn;
      $visionMic.textContent = !visionOn ? "" : msg.mic_error ? `· micrófono no disponible: ${msg.mic_error}`
        : msg.mic ? "· escuchando micrófono y mirando la pantalla" : "· mirando la pantalla";
      if (msg.error) visionLog(`⚠️ ${msg.error}`, "react");
      break;
    }
    case "vision_heard":
      visionLog(`🎤 «${msg.text}»`, "heard");
      break;
    case "vision_unclear":
      visionLog(`🎤 (sonido de ${msg.seconds}s, sin palabras entendibles)`, "");
      break;
    case "vision_level": {
      // escala logarítmica 0.0005 → 0.3
      const pct = (v) => Math.max(0, Math.min(100, (Math.log10(Math.max(v, 0.0005)) + 3.3) / 2.78 * 100));
      $meterFill.style.width = pct(msg.level) + "%";
      $meterFill.classList.toggle("voice", msg.level >= msg.threshold);
      $meterThr.style.left = pct(msg.threshold) + "%";
      break;
    }
    case "vision_tick": {
      const parts = [msg.emotion && `${EMOJI[msg.emotion] || ""} ${msg.emotion}`,
        msg.gesture && msg.gesture !== "none" && `${EMOJI[msg.gesture] || ""} ${msg.gesture}`,
        msg.hand && msg.hand !== "none" && `${HAND_EMOJI[msg.hand] || "✋"} ${msg.hand}`].filter(Boolean).join(" · ");
      if (msg.reason === "static") break; // pantalla sin cambios: no ensuciar el registro
      if (msg.reacted) visionLog(`▶ ${parts}  (p=${msg.p}, ${msg.ms} ms)`, "react");
      else if (msg.reason === "cooldown") visionLog(`· esperando (reaccionó hace poco; p=${msg.p})`, "");
      else if (msg.reason === "nothing") visionLog(`· mira tranquila (😐 sin gesto; p=${msg.p})`, "");
      else visionLog(`· nada que reaccionar (p=${msg.p} < ${msg.threshold})`, "");
      break;
    }
    case "reaction_applied": {
      for (const [part, ok] of Object.entries(msg.applied || {})) {
        if (reactionTags[part] && !ok) reactionTags[part].classList.add("dim");
      }
      break;
    }
    case "error": {
      const m = addMessage("bot error", msg.text);
      void m;
      break;
    }
    case "done":
      if (current) {
        current.bubble.classList.remove("typing");
        if (!current.text.trim()) current.bubble.textContent = "…";
      }
      current = null;
      setBusy(false);
      $input.focus();
      break;
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => { try { handle(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
  ws.onclose = () => {
    setPill($stLm, false);
    setPill($stWarudo, false);
    if (busy) handle({ type: "done" });
    setTimeout(connect, 1500);
  };
}

async function loadHistory() {
  try {
    const r = await fetch(`/api/history?thread=${encodeURIComponent(thread)}`);
    const data = await r.json();
    for (const m of data.messages || []) addMessage(m.role === "user" ? "user" : "bot", m.text);
  } catch { /* sin historial */ }
  showEmpty();
}

function send() {
  const text = $input.value.trim();
  if (!text || busy || !ws || ws.readyState !== WebSocket.OPEN) return;
  addMessage("user", text);
  $input.value = "";
  autosize();
  setBusy(true);
  current = null;
  ensureBot();
  ws.send(JSON.stringify({ type: "user", text, thread }));
}

function autosize() {
  $input.style.height = "auto";
  $input.style.height = Math.min($input.scrollHeight, 160) + "px";
}

$form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
$input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$input.addEventListener("input", autosize);
document.getElementById("new-chat").addEventListener("click", () => {
  if (busy) return;
  thread = newThreadId();
  storageSet("vt-thread", thread);
  $log.innerHTML = "";
  showEmpty();
  $input.focus();
});

// En modo overlay el chat se refresca solo mostrando los últimos mensajes de este hilo
loadHistory().then(connect);

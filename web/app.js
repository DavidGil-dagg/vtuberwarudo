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
const NOT_APPLIED = "No se aplicó en Warudo";

const params = new URLSearchParams(location.search);
if (params.get("overlay")) document.body.classList.add("overlay");

const $ = (id) => document.getElementById(id);
const $log = $("log");
const $input = $("input");
const $send = $("send");
const $form = $("composer");
const $badge = $("badge");
const $character = $("character-name");
const $user = $("user-name");
const $stLm = $("st-lm");
const $stWarudo = $("st-warudo");
const $stDec = $("st-dec");
const $themeBtn = $("theme-btn");

function storageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function storageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }
function storageDel(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } }

// ---------- tema: automático → claro → oscuro ----------
const THEMES = ["auto", "light", "dark"];
const THEME_NAMES = { auto: "automático", light: "claro", dark: "oscuro" };
const THEME_ICONS = {
  auto: '<path fill="currentColor" d="M12 3a9 9 0 1 0 0 18V3Z"/><circle cx="12" cy="12" r="8.25" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  light: '<circle cx="12" cy="12" r="4.5" fill="currentColor"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></g>',
  dark: '<path fill="currentColor" d="M20.5 14.6A8.5 8.5 0 0 1 9.4 3.5a8.5 8.5 0 1 0 11.1 11.1Z"/>',
};
let theme = THEMES.includes(storageGet("vt-theme")) ? storageGet("vt-theme") : "auto";
function applyTheme() {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  $themeBtn.querySelector("svg").innerHTML = THEME_ICONS[theme];
  $themeBtn.title = `Tema: ${THEME_NAMES[theme]}`;
  $themeBtn.setAttribute("aria-label", `Cambiar tema (ahora: ${THEME_NAMES[theme]})`);
}
$themeBtn.addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  if (theme === "auto") storageDel("vt-theme"); else storageSet("vt-theme", theme);
  applyTheme();
});
applyTheme();

// ---------- nombres: el del personaje y el tuyo ----------
// Se guardan en este navegador y viajan con cada mensaje; el servidor usa
// config.toml [persona] name si el del personaje está vacío.
let defaultCharacter = "Personaje";
const names = {
  character: storageGet("vt-character") || "",
  user: storageGet("vt-user") || "",
};
const characterName = () => names.character || defaultCharacter;
const userName = () => names.user || "Tú";

function renderNames() {
  const c = characterName();
  if (document.activeElement !== $character) $character.value = c;
  if (document.activeElement !== $user) $user.value = names.user;
  $character.placeholder = defaultCharacter;
  $badge.textContent = (c.trim()[0] || "?").toUpperCase();
  document.title = `${c} · Chat`;
  $input.placeholder = `Escribe a ${c}…`;
  for (const el of $log.querySelectorAll(".msg.bot:not(.error) .who-says")) el.textContent = c;
  for (const el of $log.querySelectorAll(".msg.user .who-says")) el.textContent = userName();
  const empty = $log.querySelector(".empty strong");
  if (empty) empty.textContent = `Saluda a ${c}`;
}

function cleanName(v) { return v.replace(/\s+/g, " ").trim().slice(0, 40); }

function commitName(which, input) {
  const v = cleanName(input.value);
  // El del personaje igual al de config.toml no se guarda: así sigue el valor por defecto
  names[which] = which === "character" && v === defaultCharacter ? "" : v;
  if (names[which]) storageSet(`vt-${which}`, names[which]); else storageDel(`vt-${which}`);
  input.value = which === "character" ? characterName() : names.user;
  renderNames();
  sendNames();
}

for (const [which, input] of [["character", $character], ["user", $user]]) {
  input.addEventListener("change", () => commitName(which, input));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") {
      input.value = which === "character" ? characterName() : names.user;
      input.blur();
    }
  });
  input.addEventListener("input", () => {
    if (which === "user") renderNames();
  });
}

function newThreadId() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
}
let thread = params.get("thread") || storageGet("vt-thread") || newThreadId();
storageSet("vt-thread", thread);

let ws = null;
const $visionBtn = $("vision-btn");
const $visionPanel = $("vision-panel");
const $visionLog = $("vision-log");
const $visionMic = $("vision-mic");
const $meter = $visionPanel.querySelector(".meter");
const $meterFill = $("meter-fill");
const $meterThr = $("meter-thr");
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

function setLamp(el, ok) {
  if (ok === undefined) return;
  el.classList.toggle("ok", !!ok);
  el.classList.toggle("bad", !ok);
  el.querySelector(".lamp-state").textContent = ok ? "conectado" : "sin conexión";
}

function scrollDown() { $log.scrollTop = $log.scrollHeight; }

function clearEmpty() { const e = $log.querySelector(".empty"); if (e) e.remove(); }

function showEmpty() {
  if ($log.children.length) return;
  const d = document.createElement("div");
  d.className = "empty";
  const s = document.createElement("strong");
  s.textContent = `Saluda a ${characterName()}`;
  const p = document.createElement("span");
  p.textContent = "Escribe abajo; te contestará y se moverá en Warudo mientras habla.";
  d.append(s, p);
  $log.appendChild(d);
}

function addMessage(role, text = "") {
  clearEmpty();
  const el = document.createElement("article");
  el.className = `msg ${role}`;
  const who = document.createElement("div");
  who.className = "who-says";
  who.textContent = role.startsWith("user") ? userName() : role.includes("error") ? "Error" : characterName();
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  const tags = document.createElement("div");
  tags.className = "tags";
  el.append(who, bubble, tags);
  $log.appendChild(el);
  scrollDown();
  return { el, bubble, tags, text };
}

function addTag(msg, text, dim = false, cls = "") {
  if (!msg || !msg.tags) return null;
  const t = document.createElement("span");
  t.className = "tag" + (cls ? ` ${cls}` : "");
  t.textContent = text;
  if (dim) markDim(t);
  msg.tags.appendChild(t);
  scrollDown();
  return t;
}

function markDim(tag) {
  tag.classList.add("dim");
  tag.title = NOT_APPLIED;
  const sr = document.createElement("span");
  sr.className = "sr-only";
  sr.textContent = ` (${NOT_APPLIED.toLowerCase()})`;
  tag.appendChild(sr);
}

function setBusy(b) {
  busy = b;
  $send.disabled = b;
  $log.setAttribute("aria-busy", b ? "true" : "false");
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
      if (msg.name) { defaultCharacter = msg.name; renderNames(); }
      setLamp($stLm, msg.lmstudio);
      setLamp($stWarudo, msg.warudo);
      setLamp($stDec, msg.decision);
      if (msg.decision_backend) {
        $stDec.querySelector(".lamp-name").textContent = msg.decision_backend === "jev" ? "jev" : "StartLux";
      }
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
      addTag(m, `🛠 ${msg.name}`, false, "tool");
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
      if (m && m.tags) m.tags.title = `Decidido en ${msg.ms} ms. Las acotaciones tachadas no se aplicaron en Warudo.`;
      break;
    }
    case "vision_status": {
      if (msg.available !== undefined) $visionBtn.hidden = !msg.available;
      visionOn = !!msg.enabled;
      $visionBtn.setAttribute("aria-pressed", String(visionOn));
      $visionBtn.querySelector(".label").textContent = visionOn ? "Visión activa" : "Modo visión";
      $visionPanel.hidden = !visionOn;
      $visionMic.textContent = !visionOn ? "" : msg.mic_error ? `micrófono no disponible: ${msg.mic_error}`
        : msg.mic ? "escuchando el micrófono y mirando la pantalla" : "mirando la pantalla";
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
      const p = pct(msg.level);
      $meterFill.style.width = p + "%";
      $meterFill.classList.toggle("voice", msg.level >= msg.threshold);
      $meterThr.style.left = pct(msg.threshold) + "%";
      $meter.setAttribute("aria-valuenow", String(Math.round(p)));
      break;
    }
    case "vision_tick": {
      const parts = [msg.emotion && `${EMOJI[msg.emotion] || ""} ${msg.emotion}`,
        msg.gesture && msg.gesture !== "none" && `${EMOJI[msg.gesture] || ""} ${msg.gesture}`,
        msg.hand && msg.hand !== "none" && `${HAND_EMOJI[msg.hand] || "✋"} ${msg.hand}`].filter(Boolean).join(", ");
      if (msg.reason === "static") break; // pantalla sin cambios: no ensuciar el registro
      if (msg.reacted) visionLog(`▶ ${parts}  (p=${msg.p}, ${msg.ms} ms)`, "react");
      else if (msg.reason === "cooldown") visionLog(`Esperando: reaccionó hace poco (p=${msg.p})`, "");
      else if (msg.reason === "nothing") visionLog(`Mira tranquila, sin gesto (p=${msg.p})`, "");
      else visionLog(`Nada a lo que reaccionar (p=${msg.p} < ${msg.threshold})`, "");
      break;
    }
    case "reaction_applied": {
      for (const [part, ok] of Object.entries(msg.applied || {})) {
        if (reactionTags[part] && !ok && !reactionTags[part].classList.contains("dim")) markDim(reactionTags[part]);
      }
      break;
    }
    case "error": {
      addMessage("bot error", msg.text);
      break;
    }
    case "done":
      if (current) {
        current.bubble.classList.remove("typing");
        if (!current.text.trim()) current.bubble.textContent = "…";
      }
      current = null;
      setBusy(false);
      if (!document.body.classList.contains("overlay")) $input.focus();
      break;
  }
}

function sendNames() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "names", character: names.character, user_name: names.user }));
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = sendNames;
  ws.onmessage = (ev) => { try { handle(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
  ws.onclose = () => {
    setLamp($stLm, false);
    setLamp($stWarudo, false);
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
  ws.send(JSON.stringify({ type: "user", text, thread, character: names.character, user_name: names.user }));
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
$("new-chat").addEventListener("click", () => {
  if (busy) return;
  thread = newThreadId();
  storageSet("vt-thread", thread);
  $log.innerHTML = "";
  showEmpty();
  $input.focus();
});

renderNames();
// En modo overlay el chat se refresca solo mostrando los últimos mensajes de este hilo
loadHistory().then(connect);

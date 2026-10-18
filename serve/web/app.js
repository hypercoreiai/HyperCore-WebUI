// serve/web/app.js - the HyperCore web app: Chat, Monitor, About. No framework, no network beyond this server.
// The Monitor tab rebuilds PR #22's dashboard idea (code-martin) on the server's own /metrics.
"use strict";

const $ = (id) => document.getElementById(id);
const SPRITE = "web/sprite.svg";
const icon = (name, cls = "st-icon") => `<svg class="${cls}" aria-hidden="true"><use href="${SPRITE}#i-${name}"/></svg>`;
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "–" : Number(n).toLocaleString(undefined, {maximumFractionDigits: d, minimumFractionDigits: d}));
const kfmt = (n) => (n == null ? "–" : n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : fmt(n));
// a context size: 32768 -> "32K" (powers of two), else like kfmt
const ctxfmt = (n) => (n && n % 1024 === 0 ? `${fmt(n / 1024)}K` : kfmt(n));
const gb = (b, d = 1) => (b == null ? "–" : fmt(b / 1073741824, d));   // memory: binary GB, as Windows shows it

const store = {
  get(k, d) { try { const v = localStorage.getItem("hypercore." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("hypercore." + k, JSON.stringify(v)); } catch (e) { /* private mode: in memory only */ } },
};

// ------------------------------------------------------------------ toasts
function toast(kind, title, text = "", ms = 3500, action = null) {
  const names = {info: "info", success: "check", warn: "warning", error: "error"};
  const el = document.createElement("div");
  el.className = `st-toast st-toast--${kind}`;
  el.innerHTML = `${icon(names[kind] || "info")}<div><div class="st-toast__title"></div><div class="t-text"></div></div>`;
  el.querySelector(".st-toast__title").textContent = title;
  el.querySelector(".t-text").textContent = text;
  if (action) {
    const b = document.createElement("button");
    b.className = "st-btn st-btn--secondary";
    b.style.height = "32px";
    b.style.marginLeft = "auto";
    b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.appendChild(b);
  }
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {                                   // http on another host: no async clipboard
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  if (btn) {
    const use = btn.querySelector("use");
    use.setAttribute("href", `${SPRITE}#i-check`);
    setTimeout(() => use.setAttribute("href", `${SPRITE}#i-copy`), 1500);
  }
  toast("success", "Copied to clipboard", "", 1800);
}

// ------------------------------------------------------------------ theme and tabs
// the system's theme until the user picks one (only a click is saved)
function setTheme(t, save) {
  document.documentElement.dataset.theme = t;
  if (save) try { localStorage.setItem("hypercore.theme", t); } catch (e) { /* ignore */ }
  $("theme-icon").setAttribute("href", `${SPRITE}#i-${t === "dark" ? "sun" : "moon"}`);
  $("dark-toggle").setAttribute("aria-checked", String(t === "dark"));
}
const flipTheme = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
$("theme-btn").onclick = flipTheme;
$("dark-toggle").onclick = flipTheme;
setTheme(document.documentElement.dataset.theme || "light", false);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  let saved = null;
  try { saved = localStorage.getItem("hypercore.theme"); } catch (err) { /* ignore */ }
  if (!saved) setTheme(e.matches ? "dark" : "light", false);
});

// ------------------------------------------------------------------ surface (page background)
// The Sampling drawer's Surface slider walks the page background along one continuous metallic
// ramp: pure white, polished chrome, brushed silver, matte onyx. The ramp is sampled at the
// slider position and every token the kit uses (background, panels, lines, ink) is derived from
// that sample, so the UI stays readable at both ends. Piecewise-linear interpolation over nine
// anchors is what keeps the walk free of banding; the sheen layers in app.css sit on top of the
// sampled colour and peak in the metallic middle.
// The slider is a browser-local setting: it is stored in localStorage and never sent to the
// server, so it does not touch sampling or any other app's defaults.
const METAL_STOPS = [
  [0.00, [255, 255, 255]],   // pure white
  [0.12, [238, 241, 243]],   // white, lit
  [0.24, [223, 227, 231]],   // polished chrome, specular
  [0.36, [207, 212, 216]],   // chrome
  [0.48, [182, 188, 194]],   // brushed silver
  [0.60, [158, 163, 168]],   // silver, deeper
  [0.72, [120, 125, 130]],   // graphite
  [0.84, [72, 77, 82]],      // near onyx
  [1.00, [20, 23, 26]],      // matte onyx black
];
const DARK_INK = [34, 30, 31];    // tokens.css --st-ink, light theme
const LIGHT_INK = [238, 241, 243]; // tokens.css --st-ink, dark theme

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const mixRGB = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
const lum = (c) => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
const hex = (c) => "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");
// WCAG contrast, so legibility is a number rather than an opinion
const srgb = (v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
const relLum = (c) => 0.2126 * srgb(c[0]) + 0.7152 * srgb(c[1]) + 0.0722 * srgb(c[2]);
const contrast = (a, b) => { const l1 = relLum(a), l2 = relLum(b); return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05); };

// the most muted version of an ink that still clears `min` contrast against the panel
function soften(ink, surface, min) {
  let out = ink.slice();
  for (let t = 0.05; t <= 0.60; t += 0.05) {
    const c = mixRGB(ink, surface, t);
    if (contrast(c, surface) >= min) out = c; else break;
  }
  return out;
}

// the ramp at t (0..1), clamped: linear between the anchors, no gaps
function metalColor(t) {
  const x = clamp01(Number(t) || 0);
  for (let i = 1; i < METAL_STOPS.length; i++) {
    if (x <= METAL_STOPS[i][0]) {
      const [t0, c0] = METAL_STOPS[i - 1];
      const [t1, c1] = METAL_STOPS[i];
      return mixRGB(c0, c1, (x - t0) / (t1 - t0));
    }
  }
  return METAL_STOPS[METAL_STOPS.length - 1][1].slice();
}

// pos is the slider value, 0..100. Everything the page is painted with, from that one number.
function metalPalette(pos) {
  const t = clamp01((Number(pos) || 0) / 100);
  const base = metalColor(t);
  const baseLum = lum(base);
  // panels sit above the surface. The lift falls off smoothly as the surface darkens, so the
  // metallic middle keeps depth instead of going flat, and there is no step where it flips.
  const lift = 0.10 + 0.50 * clamp01((baseLum - 0.35) / 0.30);
  const surface = mixRGB(base, [255, 255, 255], lift);
  // the ink is whichever theme ink reads better on that panel; if a mid-tone panel is too flat
  // for either, fall back to pure black or pure white, which always clears 4.5:1
  let ink = contrast(DARK_INK, surface) >= contrast(LIGHT_INK, surface) ? DARK_INK.slice() : LIGHT_INK.slice();
  if (contrast(ink, surface) < 4.5) ink = relLum(surface) > 0.18 ? [0, 0, 0] : [255, 255, 255];
  const towardsDark = baseLum > 0.5 ? [0, 0, 0] : [255, 255, 255];
  return {
    pos: Math.round(t * 100),
    bg: hex(base),
    surface: hex(surface),
    surface2: hex(mixRGB(surface, base, 0.45)),
    line: hex(mixRGB(base, towardsDark, 0.22)),
    lineSoft: hex(mixRGB(base, towardsDark, 0.12)),
    ink: hex(ink),
    inkSoft: hex(soften(ink, surface, 4.5)),
    inkMuted: hex(soften(ink, surface, 3.0)),
    // the specular highlight is strongest in the metallic middle and gone at the two flat ends
    sheen: Math.max(0, 1 - Math.abs(t - 0.5) / 0.5),
  };
}

const SURFACE_NAMES = (p) => (p === 0 ? "default" : p <= 35 ? "polished chrome" : p <= 65 ? "brushed silver"
  : p >= 90 ? "onyx black" : "graphite");

function applySurface(pos) {
  const p = metalPalette(pos);
  const root = document.documentElement;
  if (p.pos === 0) {                       // the untouched theme: no override at all
    root.dataset.surface = "flat";
    for (const k of ["--st-bg", "--st-surface", "--st-surface-2", "--st-line", "--st-line-soft",
                     "--st-ink", "--st-ink-soft", "--st-ink-muted", "--st-metal-spec",
                     "--st-metal-streak", "--st-metal-top", "--st-surface-pos"])
      root.style.removeProperty(k);
  } else {
    root.dataset.surface = "metal";
    root.style.setProperty("--st-surface-pos", String(p.pos));
    root.style.setProperty("--st-bg", p.bg);
    root.style.setProperty("--st-surface", p.surface);
    root.style.setProperty("--st-surface-2", p.surface2);
    root.style.setProperty("--st-line", p.line);
    root.style.setProperty("--st-line-soft", p.lineSoft);
    root.style.setProperty("--st-ink", p.ink);
    root.style.setProperty("--st-ink-soft", p.inkSoft);
    root.style.setProperty("--st-ink-muted", p.inkMuted);
    root.style.setProperty("--st-metal-spec", `rgba(255,255,255,${(0.30 * p.sheen).toFixed(3)})`);
    root.style.setProperty("--st-metal-streak", `rgba(255,255,255,${(0.07 * p.sheen).toFixed(3)})`);
    root.style.setProperty("--st-metal-top", `rgba(255,255,255,${(0.10 * p.sheen).toFixed(3)})`);
  }
  const out = $("o-surface");
  if (out) out.textContent = SURFACE_NAMES(p.pos);
  return p;
}

function setSurface(pos, save = true) {
  const p = applySurface(pos);
  if (save) store.set("surface", p.pos);
  return p;
}

$("s-surface").oninput = (e) => setSurface(e.target.value);
const savedSurface = store.get("surface", 0);
$("s-surface").value = savedSurface;
applySurface(savedSurface);


// ------------------------------------------------------------------ the engine
// The red button in the banner stops the engine, not the app. The port stays bound, every
// tab keeps working, and the button turns into "Start Engine" so the model comes back
// without restarting anything. Stopping takes two clicks so a stray click cannot end a
// model that is mid-request.
// POST /v1/stop and POST /v1/start are the engine-only pair. POST /v1/shutdown closes the
// whole server, which is what Ctrl+C and SIGTERM do; that one is on the Monitor tab under
// Model state, so closing the app is still one click from the page.
const RESUME_PROMPT = "Complete the open tasks";   // what the composer's Start button asks for
let engineStopped = false;   // the banner's Stop was accepted: the engine is down, the app is not
let engineArmed = false;     // the first click arms the stop, the second one stops
let engineBusy = false;      // a stop or a start request is in flight
let serverClosed = false;    // the Monitor's "Close this server" was pressed

const STOP_TITLE = "Stop the engine: the model, the image encoder and the MCP servers. " +
                   "This app keeps serving; press it again to bring the model back.";
const ARM_TITLE = "Click again to stop the engine. The app keeps serving.";
const START_TITLE = "Start the engine again. The app has kept serving while it was down.";

// The engine is down when this app stopped it, or when /health says it never came up.
function engineDown() {
  return engineStopped || Boolean(health.engine && health.engine.state === "offline");
}

function paintEngineButton() {
  const btn = $("shutdown-btn");
  if (serverClosed) {
    btn.disabled = true;
    delete btn.dataset.armed;
    $("shutdown-text").textContent = "Server closed";
    btn.title = "This server is closed. Start it again with run.sh.";
    return;
  }
  btn.disabled = false;
  if (engineDown()) {
    delete btn.dataset.armed;
    $("shutdown-text").textContent = engineBusy ? "Starting..." : "Start Engine";
    btn.title = START_TITLE;
    btn.setAttribute("aria-label", "Start the engine");
    return;
  }
  $("shutdown-text").textContent = engineArmed ? "Confirm" : "Stop Engine";
  btn.title = engineArmed ? ARM_TITLE : STOP_TITLE;
  btn.setAttribute("aria-label", "Stop the engine");
  if (engineArmed) btn.dataset.armed = "1"; else delete btn.dataset.armed;
}

function disarmEngineButton() {
  engineArmed = false;
  paintEngineButton();
}

$("shutdown-btn").onclick = async () => {
  const btn = $("shutdown-btn");
  if (serverClosed || engineBusy) return;
  if (!engineDown()) {
    if (!engineArmed) {
      engineArmed = true;
      paintEngineButton();
      setTimeout(() => { if (!engineBusy) disarmEngineButton(); }, 8000);   // let it go back on its own
      return;
    }
    engineBusy = true;
    delete btn.dataset.armed;
    $("shutdown-text").textContent = "Stopping...";
    setPill("idle", "Stopping the engine");
    try {
      const r = await fetch("v1/stop", { method: "POST", headers: headers(true), body: "{}" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error?.message || `HTTP ${r.status}`);
      engineStopped = true;
      engineArmed = false;
      toast("success", "Engine stopped",
           "Stopped " + (body.stopped || []).join(", ") + ". The app is still serving; " +
           "Start Engine brings the model back.", 8000);
      setPill("idle", "Engine stopped");
      await loadHealth();
    } catch (e) {
      engineBusy = false;
      disarmEngineButton();
      toast("error", "Could not stop the engine", String(e.message || e));
      return;
    }
    engineBusy = false;
    paintEngineButton();
    return;
  }
  engineBusy = true;
  $("shutdown-text").textContent = "Starting...";
  setPill("idle", "Starting the engine");
  try {
    const r = await fetch("v1/start", { method: "POST", headers: headers(true), body: "{}" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error?.message || `HTTP ${r.status}`);
    engineStopped = false;
    toast("success", "Engine starting", "The model loads in a minute or two; the chat works when it is ready.", 8000);
    setPill("idle", "Idle");
    await loadHealth();
  } catch (e) {
    toast("error", "The engine did not come back", String(e.message || e), 8000);
    setPill("error", "Engine not running");
  } finally {
    engineBusy = false;
    paintEngineButton();
  }
};

// Closing the whole server is the Monitor tab's job; the banner button is about the engine.
$("close-server-btn").onclick = async () => {
  const btn = $("close-server-btn");
  btn.disabled = true;
  setPill("idle", "Shutting down");
  try {
    const r = await fetch("v1/shutdown", { method: "POST", headers: headers(true), body: "{}" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error?.message || `HTTP ${r.status}`);
    serverClosed = true;
    toast("success", "Shutting down", "Closing " + (body.closing || []).join(", ") +
          ". Start it again with run.sh.", 6000);
    setPill("idle", "Stopped");
    document.querySelectorAll(".st-tab, .st-composer .st-btn").forEach(b => b.disabled = true);
    $("input").disabled = true;
    paintEngineButton();
  } catch (e) {
    btn.disabled = false;
    toast("error", "Could not close the server", String(e.message || e));
  }
};
let tab = "chat";
const TABS = ["chat", "assist", "monitor", "tune", "build", "about"];
function showTab(name) {
  tab = TABS.includes(name) ? name : "chat";
  for (const b of document.querySelectorAll(".st-tab")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const v of TABS) $(`view-${v}`).hidden = v !== tab;
  if (location.hash.slice(1) !== tab) history.replaceState(null, "", tab === "chat" ? location.pathname : `#${tab}`);
  if (tab === "chat") $("input").focus();
  if (tab === "monitor") { loadMcp(); loadAssistMonitor(); loadHistory(); }
  if (tab === "about") loadConfig();
  if (tab === "assist") { loadAssistModels(); renderAssist(); $("assist-input").focus(); }
  if (tab === "tune") loadTune();
  if (tab === "build") loadBuild();
  if (lastMetrics) render(lastMetrics);
}
for (const b of document.querySelectorAll(".st-tab")) b.onclick = () => showTab(b.dataset.tab);
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// ------------------------------------------------------------------ server access
// The page is served by the server it talks to, so a key the server was started with
// (a default "0000", or one set in the model's config) is carried in and used without
// being typed in. A key typed here still wins, and is kept in this browser only.
if (window.HYPERCORE_API_KEY && !store.get("apikey", "")) store.set("apikey", window.HYPERCORE_API_KEY);

function headers(json = false) {
  const h = {};
  // A key typed in Settings wins. Otherwise use the key this page was served with:
  // a browser in private mode, or one whose storage was cleared, has nothing stored
  // and would otherwise 401 on every call while the page itself knows the key.
  const key = store.get("apikey", "") || (typeof window !== "undefined" && window.HYPERCORE_API_KEY) || "";
  if (key) h.Authorization = "Bearer " + key;
  if (json) h["Content-Type"] = "application/json";
  return h;
}
$("api-key").value = store.get("apikey", "");
$("api-key").onchange = () => { store.set("apikey", $("api-key").value.trim()); toast("success", "API key saved", "Kept in this browser only.");
  // #14: a key saved after the page loaded is the reason /mcp answered 401 and the tools never appeared.
  // Re-run the calls that need a key, so the tools show up without a reload.
  loadMcp(); loadPrompts(true); loadAssistMonitor(); };

let health = {model: "hypercore", images: false, max_context: 0};

// The engine is not the app. When it cannot start - a sweep, a build or another server
// holds the GPU - every tab still works and the chat says so plainly instead of failing.
function renderEngineState() {
  const state = health.engine || {};
  const offline = state.state === "offline";
  $("engine-offline").hidden = !offline;
  if (!offline) return;
  const retry = state.retry || {};
  const why = state.reason || state.detail || "no engine is running";
  // A retry is paused while a tune sweep or a build has the GPU: saying "retrying in 30 s"
  // then would be a lie, and the attempt would disturb the work holding the card.
  const next = retry.paused
    ? ` ${retry.paused_reason || "Nothing is retried while another job has the GPU."}`
    : retry.state === "retrying"
      ? ` Retrying in ${Math.max(1, Math.round(retry.next_attempt_s || 0))} s (attempt ${retry.attempts || 0}).`
      : "";
  $("engine-offline-title").textContent = "The model is not running";
  $("engine-offline-why").textContent = `${why}${next}`;
  // the engine's own error, in full, for anyone who wants to see why
  $("engine-offline-why").title = state.detail || retry.detail || why;
  $("engine-load-btn").textContent = state.stopped ? "Start the engine"
                                : retry.paused ? "Try anyway"
                                : retry.state === "retrying" ? "Try now" : "Load it";
  $("chat-empty-sub").textContent = "The model is not loaded on this PC. The tabs below still work.";
  paintEngineButton();
}

async function loadHealth() {
  try {
    health = await (await fetch("health")).json();
    if (health.images) { $("attach-btn").title = "Attach a text file or a picture (or drop it here)"; }
    else { $("attach-btn").title = "Attach a text file (or drop it here)"; }
    if (!health.engine || health.engine.state !== "offline") {
      $("chat-empty-sub").textContent = `${health.model} runs on this PC. Nothing leaves it.`;
    }
    renderEngineState();
  } catch (e) {
    setTimeout(loadHealth, 2000);
  }
}

$("engine-load-btn").onclick = async () => {
  const btn = $("engine-load-btn");
  // A stop from the banner is not an unload: /v1/load refuses while the engine is stopped
  // (ensure_loaded raises EngineStopped), so bring the process back with /v1/start.
  const endpoint = (health.engine || {}).stopped ? "v1/start" : "v1/load";
  btn.disabled = true;
  btn.textContent = "Loading...";
  try {
    const res = await fetch(endpoint, {method: "POST", headers: headers(true), body: "{}"});
    const out = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error((out.error && out.error.message) || `the server said ${res.status}`);
    }
    toast("success", "The engine is loading", "A model load takes a minute or two; the chat " +
          "will work when it is ready.", 8000);
  } catch (e) {
    toast("error", "The engine could not be loaded", e.message || String(e), 8000);
  } finally {
    btn.disabled = false;
    btn.textContent = "Try again";
    loadHealth();
  }
};

// ------------------------------------------------------------------ Monitor
const METRICS = [
  {key: "speed", label: "Speed", icon: "gauge", unit: "t/s", series: "tok_s"},
  {key: "gpu", label: "GPU load", icon: "gpu", unit: "%", series: "gpu_util", max: 100},
  {key: "vram", label: "VRAM", icon: "layers", unit: "GB", series: "gpu_mem_used"},
  {key: "temp", label: "GPU temp", icon: "thermometer", unit: "°C", series: "gpu_temp", tone: "warn"},
  {key: "power", label: "Power", icon: "bolt", unit: "W", series: "gpu_power"},
  {key: "pcie", label: "PCIe", icon: "link", unit: "", series: "gpu_pcie_rx_mb", tone: "info"},
  {key: "cpu", label: "CPU", icon: "cpu", unit: "%", series: "cpu", max: 100},
  {key: "disk", label: "Disk read", icon: "disk", unit: "MB/s", series: "disk_read_mb", tone: "info"},
];
$("metrics").innerHTML = METRICS.map((m) => `
  <div class="st-card metric-card"><div class="st-metric">
    <span class="st-metric__label">${icon(m.icon, "st-icon st-icon--sm")}${esc(m.label)}</span>
    ${m.key === "speed" ? `<div class="speed-values">
      <div><span class="st-metric__value" id="mv-speed">-</span><span class="st-metric__sub" id="ms-speed">Decode</span></div>
      <div class="speed-prefill"><span class="st-metric__value" id="mv-prefill">-</span><span class="st-metric__sub" id="ms-prefill">Prefill</span></div>
    </div>` : `<span class="st-metric__value" id="mv-${m.key}">–</span>
    <span class="st-metric__sub" id="ms-${m.key}"></span>`}
    <svg class="st-metric__spark" id="sp-${m.key}" viewBox="0 0 100 32" preserveAspectRatio="none"${m.tone ? ` data-tone="${m.tone}"` : ""}>
      <path class="area" fill="currentColor" opacity=".12"/><path class="line" fill="none" stroke="currentColor"
      stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      ${m.key === "speed" ? `<g id="sp-prefill" class="speed-prefill"><path class="area" fill="currentColor" opacity=".12"/>
        <path class="line" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"
        stroke-linecap="round" vector-effect="non-scaling-stroke"/></g>` : ""}</svg>
  </div></div>`).join("");

function spark(id, values, max) {
  const svg = $(id);
  const v = (values || []).map((x) => (x == null ? 0 : x));
  if (v.length < 2) { svg.querySelector(".line").setAttribute("d", ""); svg.querySelector(".area").setAttribute("d", ""); return; }
  const top = Math.max(max || 0, ...v, 1e-9);
  const pts = v.map((x, i) => [(i / (v.length - 1)) * 100, 30 - (x / top) * 26]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join("");
  svg.querySelector(".line").setAttribute("d", line);
  svg.querySelector(".area").setAttribute("d", `${line}L100,32L0,32Z`);
}
function setMetric(key, value, unit, sub) {
  $(`mv-${key}`).innerHTML = value == null ? "–" : `${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ""}`;
  $(`ms-${key}`).textContent = sub || "";
}

let lastMetrics = null, metricsFailures = 0, keyWarned = false, mcpTick = 0;
let reqShowAll = false;   // the Monitor's request table: the last 12, or every one the server keeps (issue #35)
async function poll() {
  try {
    const r = await fetch(reqShowAll ? "metrics?requests=all" : "metrics", {headers: headers()});
    if (r.status === 401) {
      setPill("error", "API key needed");
      if (!keyWarned) { keyWarned = true; toast("warn", "API key needed", "This server needs a key: add it under About > Settings.", 6000); }
    } else if (r.ok) {
      lastMetrics = await r.json();
      metricsFailures = 0;
      render(lastMetrics);
    } else {
      throw new Error(`HTTP ${r.status}`);
    }
  } catch (e) {
    if (++metricsFailures === 3) setPill("error", "Server not reachable");
  }
  if (tab === "monitor" && ++mcpTick % 10 === 0) { loadMcp(); loadAssistMonitor(); loadHistory(); }       // server states change rarely: every 10 s
  if (!serverClosed) setTimeout(poll, 1000);   // after Close this server the port is gone: stop asking it
}

function setPill(state, text) {
  $("pill").dataset.state = state === "error" ? "queued" : state;
  $("pill-text").textContent = text;
}

function render(m) {
  const live = m.live || {}, hw = m.hardware || {}, st = m.hardware_static || {}, eng = m.engine || {}, h = m.history || {};
  const last = (m.requests || [])[0];
  // the header pill
  if (live.state === "reading") {
    const pct = live.prompt_total ? Math.round((100 * live.prompt_read) / live.prompt_total) : null;
    setPill("reading", pct != null ? `Reading prompt · ${pct}%` : "Reading prompt");
  } else if (live.state === "generating") {
    setPill("generating", `Generating · ${fmt(live.tok_s, 1)} tok/s`);
  } else {
    setPill("idle", "Idle");
  }
  if (live.queued > 0) setPill("queued", `${live.queued} queued`);
  if (tab === "monitor") renderMonitor(live, hw, st, eng, h, last, m.requests || [], m.totals, m.requests_kept);
  if (tab === "monitor") renderConvCache(m.conversation_cache);
  if (tab === "about") renderAbout(eng, hw, st);
}

// #596: the conversation cache - the prompt's state the engine keeps between requests (always), and the whole
// conversations it parks in RAM when "--conversation-cache-mib N" is in the run config's args (opt-in)
function since(t) {
  if (!t) return "";
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? "just now" : s < 3600 ? `${fmt(s / 60)} min ago` : `${fmt(s / 3600, 1)} h ago`;
}
function renderConvCache(c) {
  $("cc-card").hidden = !c;
  if (!c) return;                                  // an older server
  const pct = (a, b) => (b ? `${Math.min(100, (100 * a) / b)}%` : "0%");
  $("cc-bars").hidden = !c.enabled;
  if (c.enabled) {
    $("cc-slots-text").textContent = `${fmt(c.parked)} / ${fmt(c.slots)}`;
    $("cc-slots-bar").style.width = pct(c.parked, c.slots);
    const budget = c.budget_mib * 1048576;
    $("cc-mem-text").textContent = `${gb(c.bytes)} / ${gb(budget)} GB`;
    $("cc-mem-bar").style.width = pct(c.bytes, budget);
  }
  $("cc-sum").textContent = c.requests ? `${fmt(c.requests_reused)} of ${fmt(c.requests)} requests reused part of their prompt` : "";
  const share = c.prompt_tokens ? ` (${fmt((100 * c.reused_tokens) / c.prompt_tokens)}% of all prompt tokens)` : "";
  const event = c.last_event ? `${c.last_event === "parked" ? "Parked" : "Restored"} ${fmt(c.last_tokens)} tokens, ${since(c.last_at)}` : null;
  facts($("cc-facts"), [
    ["Last request", c.last_prompt != null ? `${fmt(c.last_reused || 0)} of ${fmt(c.last_prompt)} prompt tokens reused` : null],
    ["Reused since start", c.requests ? `${fmt(c.reused_tokens)} tokens${share}` : null],
    ["Parked / restored", c.enabled ? `${fmt(c.parks)} / ${fmt(c.restores)}${c.evictions ? ` · ${fmt(c.evictions)} evicted` : ""}` : null],
    ["Last switch", c.enabled ? event : null],
  ]);
  $("cc-note").textContent = c.enabled
    ? "A request that continues a parked conversation gets its state back instead of reading it again; the oldest goes when the slots or the memory are full."
    : "The engine keeps the last conversation's state, so a follow-up reads only what is new. To keep several conversations (agents taking turns), add \"--conversation-cache-mib\", \"8192\" to the run config's args (docs/DETAILS.md).";
}

function renderTotals(t) {
  if (!t || !t.requests) return "";
  const since = new Date(t.since * 1000).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
  const read = t.prompt_tokens - t.reused;
  const pSpeed = t.prompt_ms > 0 && read > 0 ? ` at ${fmt(read / (t.prompt_ms / 1000))} tok/s` : "";
  const oSpeed = t.decode_ms > 0 && t.output_tokens > 0 ? ` at ${fmt(t.output_tokens / (t.decode_ms / 1000), 1)} tok/s` : "";
  return `Since ${since}: ${fmt(t.requests)} requests · ${fmt(read)} prompt tokens read${pSpeed} (${fmt(t.reused)} reused) · ` +
         `${fmt(t.output_tokens)} written${oSpeed}`;
}
function renderMonitor(live, hw, st, eng, h, last, requests, totals, kept) {
  // model state
  const on = live.queued > 0 ? "queued" : live.state;
  for (const b of document.querySelectorAll("#state-badges .st-badge")) b.classList.toggle("on", b.dataset.s === on || b.dataset.s === live.state);
  const prog = $("state-progress");
  let label = "Waiting for a request", detail = "", pct = 0;
  if (live.state === "reading") {
    label = "Reading prompt";
    prog.dataset.tone = "info";
    if (live.prompt_total) {
      pct = (100 * live.prompt_read) / live.prompt_total;
      detail = `${fmt(live.prompt_read)} / ${fmt(live.prompt_total)} tokens · ${fmt(pct)}%`;
    } else {
      detail = `${fmt(live.prompt_tokens)} tokens`;
    }
  } else if (live.state === "generating") {
    label = live.phase ? live.phase[0].toUpperCase() + live.phase.slice(1) : "Generating";
    delete prog.dataset.tone;
    pct = live.max_tokens ? Math.min(100, (100 * live.generated) / live.max_tokens) : 0;
    detail = `${fmt(live.generated)} tokens · ${fmt(live.tok_s, 1)} tok/s`;
  } else if (last) {
    delete prog.dataset.tone;
    detail = `last: ${fmt(last.output_tokens)} tokens${last.decode_tok_s ? ` at ${fmt(last.decode_tok_s, 1)} tok/s` : ""}`;
  }
  $("state-label").textContent = label;
  $("state-detail").textContent = detail;
  $("state-bar").style.width = `${pct}%`;

  // the eight cards
  const speed = live.state === "generating" ? live.tok_s : last ? last.decode_tok_s : null;
  setMetric("speed", speed == null ? null : fmt(speed, 1), "t/s",
            live.state === "generating" ? "Decode now" : last ? "Decode last request" : "Decode");
  // The engine's own prefill rate: the prompt tokens it read over the time it took to read
  // them. A prompt still being read has no figure on the wire until it finishes, so the card
  // says which request the number came from rather than implying it is live. A 0 here means
  // "the prompt is already read", not "this prompt took no time", so it falls back to the
  // last finished request rather than showing a false zero.
  const livePrefill = live.prefill_tok_s_mean || null;
  const lastPrefill = last && last.prompt_ms > 0
                    ? Math.max(0, (last.prompt_read ?? last.prompt_tokens - (last.reused || 0))) / (last.prompt_ms / 1000)
                    : null;
  const prefill = livePrefill || lastPrefill;
  setMetric("prefill", prefill == null ? null : fmt(prefill), "t/s",
            prefill == null ? "Prefill" : (livePrefill ? "Prefill now" : "Prefill last request"));
  spark("sp-speed", h.tok_s);
  spark("sp-prefill", h.prefill_tok_s_mean);
  // a model split across several cards (issue #112): the cards show their total / mean / hottest, and each card's own
  const per = (f) => (hw.gpus || []).map((g) => `GPU ${g.index} ${f(g)}`).join(" · ");
  const multi = (hw.gpus || []).length > 1;
  setMetric("gpu", hw.gpu_util == null ? null : fmt(hw.gpu_util), "%",
            multi ? per((g) => (g.util == null ? "–" : `${fmt(g.util)}%`)) : st.gpu_name || "");
  spark("sp-gpu", h.gpu_util, 100);
  setMetric("vram", hw.gpu_mem_used == null ? null : gb(hw.gpu_mem_used), hw.gpu_mem_total ? `/ ${gb(hw.gpu_mem_total, 0)} GB` : "GB",
            multi ? per((g) => (g.mem_used == null ? "–" : `${gb(g.mem_used)} GB`))
                  : eng.expert_slots ? `${fmt(eng.expert_slots)} experts cached` : "");
  spark("sp-vram", h.gpu_mem_used, hw.gpu_mem_total);
  setMetric("temp", hw.gpu_temp == null ? null : fmt(hw.gpu_temp), "°C",
            multi ? per((g) => (g.temp == null ? "–" : `${fmt(g.temp)}°`)) : "");
  spark("sp-temp", h.gpu_temp, 90);
  setMetric("power", hw.gpu_power == null ? null : fmt(hw.gpu_power), "W", hw.gpu_power_limit ? `of ${fmt(hw.gpu_power_limit)} W limit` : "");
  spark("sp-power", h.gpu_power, hw.gpu_power_limit);
  const gen = hw.gpu_pcie_gen_max || hw.gpu_pcie_gen;
  setMetric("pcie", gen ? `Gen${gen}` : null, hw.gpu_pcie_width ? `x${hw.gpu_pcie_width}` : "",
            hw.gpu_pcie_rx_mb == null ? "" : `to GPU ${fmt(hw.gpu_pcie_rx_mb, hw.gpu_pcie_rx_mb < 10 ? 1 : 0)} MB/s` +
            (hw.gpu_pcie_gen && gen && hw.gpu_pcie_gen < gen ? ` · idle Gen${hw.gpu_pcie_gen}` : ""));
  spark("sp-pcie", h.gpu_pcie_rx_mb);
  setMetric("cpu", hw.cpu == null ? null : fmt(hw.cpu), "%", st.threads ? `${st.cores ? `${st.cores} cores · ` : ""}${st.threads} threads` : "");
  spark("sp-cpu", h.cpu, 100);
  if (hw.disk_read_mb == null) {
    setMetric("disk", null, "", st.psutil ? "" : "needs psutil (setup installs it)");
  } else {
    const big = hw.disk_read_mb >= 1000;
    setMetric("disk", big ? fmt(hw.disk_read_mb / 1024, 2) : fmt(hw.disk_read_mb, hw.disk_read_mb < 10 ? 1 : 0), big ? "GB/s" : "MB/s",
              hw.disk_write_mb == null ? "" : `write ${fmt(hw.disk_write_mb, 1)} MB/s`);
  }
  spark("sp-disk", h.disk_read_mb);

  // context fill: the running request, else the last one
  const ctx = eng.max_context || 0;
  let used = 0;
  if (live.state !== "idle") used = (live.prompt_tokens || 0) + (live.generated || 0);
  else if (last) used = (last.prompt_tokens || 0) + (last.output_tokens || 0);
  const frac = ctx ? Math.min(1, used / ctx) : 0;
  $("ctx-fill").setAttribute("stroke-dasharray", `${(235.6 * frac).toFixed(1)} 314.2`);
  $("ctx-fill").style.opacity = 235.6 * frac >= 3 ? "1" : "0";         // a near-zero arc would draw just its round cap
  $("ctx-pct").textContent = `${Math.round(frac * 100)}%`;
  $("ctx-sub").textContent = ctx ? `${kfmt(used)} / ${ctxfmt(ctx)}` : "–";
  const cacheBytes = (eng.expert_cache_mib || 0) * 1048576;
  $("slots-text").textContent = eng.expert_slots ? `${fmt(eng.expert_slots)} · ${gb(cacheBytes)} GB` : "–";
  $("slots-bar").style.width = hw.gpu_mem_total ? `${Math.min(100, (100 * cacheBytes) / hw.gpu_mem_total)}%` : "0%";
  $("ram-text").textContent = hw.ram_total ? `${gb(hw.ram_used)} / ${gb(hw.ram_total, 0)} GB` : "–";
  const ramPct = hw.ram_total ? (100 * hw.ram_used) / hw.ram_total : 0;
  $("ram-bar").style.width = `${ramPct}%`;
  if (ramPct > 92) $("ram-progress").dataset.tone = "danger"; else delete $("ram-progress").dataset.tone;
  $("temp-text").textContent = hw.gpu_temp == null ? "–" : `${fmt(hw.gpu_temp)} °C`;
  $("temp-bar").style.width = hw.gpu_temp == null ? "0%" : `${Math.min(100, hw.gpu_temp)}%`;

  // recent requests
  const body = $("req-body");
  if (!requests.length) {
    body.innerHTML = `<tr><td colspan="8" class="muted">No requests yet</td></tr>`;
  } else {
    const badge = {stop: ["", "Done"], length: ["", "Max tokens"], cancel: ["st-badge--queued", "Stopped"],
                   disconnect: ["st-badge--queued", "Closed"], error: ["st-badge--error", "Error"]};
    body.innerHTML = requests.slice(0, reqShowAll ? requests.length : 12).map((r) => {
      const [cls, text] = badge[r.finish] || ["", r.finish || "–"];
      const t = new Date(r.time * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
      const proj = r.projection == null ? "" : ` <span class="st-badge${r.projection ? " st-badge--reading" : ""}" title="experimental speed projection ${r.projection ? "on" : "off"}">${r.projection ? "ESP" : "stock"}</span>`;
      // #588: the VRAM share; the PCIe share (--pcie-frac) beside it when there is one
      const hit = r.hit_rate == null ? "–" : `${(r.hit_rate * 100).toFixed(1)}%` +
        (r.pcie_share ? ` <span class="muted" title="routed experts the GPU read over PCIe (--pcie-frac) or another GPU computed">+${(r.pcie_share * 100).toFixed(1)}% PCIe</span>` : "");
      return `<tr><td>${esc(t)}</td><td><span class="st-badge ${cls}">${esc(text)}</span>${proj}</td><td class="num">${fmt(r.prompt_tokens)}</td>
        <td class="num">${fmt(r.reused)}</td><td class="num">${fmt(r.output_tokens)}</td><td class="num">${fmt(r.decode_tok_s, 1)}</td>
        <td class="num">${hit}</td><td class="num">${fmt(r.duration_s, 1)} s</td></tr>`;
    }).join("");
  }
  const all = $("req-all");
  kept = kept == null ? requests.length : kept;
  all.hidden = kept <= 12;
  all.textContent = reqShowAll ? "Show fewer" : `Show all (${kept})`;
  $("req-wrap").classList.toggle("all", reqShowAll);
  $("req-totals").textContent = renderTotals(totals);
}

function facts(el, rows) {
  el.innerHTML = rows.filter((r) => r[1] != null && r[1] !== "").map(([k, v, copy]) =>
    `<dt>${esc(k)}</dt><dd>${copy ? `<code>${esc(v)}</code><button class="st-btn st-btn--icon" data-copy="${esc(v)}" aria-label="Copy">${icon("copy")}</button>` : esc(v)}</dd>`).join("");
}
// INFO cvec=project:4-44[:singleL] | add:A-B | 0
function projectionText(c) {
  if (!c || c === "0" || c === 0) return null;
  const [mode, range, single] = String(c).split(":");
  const [a, b] = (range || "").split("-");
  return `${mode === "project" ? "Projection" : "Additive"} control vector on layers ${a}–${b}` +
         `${single ? ` (layer ${single.replace("single", "")}'s direction)` : ""}. Per chat in Sampling. Its package ` +
         "describes the vector as a refusal-direction projection; measure the speed yourself";
}
function renderAbout(eng, hw, st) {
  const kv = {int8: "8-bit", q4_0: "4-bit (Hadamard-rotated)", fp16: "16-bit"}[eng.kv] || eng.kv;
  facts($("facts-engine"), [
    ["Model", eng.model],
    ["Engine", eng.attached ? `v${eng.version || "?"} - attached, not started here` : (eng.version ? `v${eng.version}` : "built from source")],
    ...(eng.attached ? [["Engine address", eng.engine_url, true],
                       ["Engine owner", "the server that loaded it; this page only reads its answers"]] : []),
    ["Context", eng.max_context ? `${fmt(eng.max_context)} tokens` : null],
    ["KV cache", kv ? `${kv}${eng.kv_resident ? `, streamed: ${fmt(eng.kv_resident)} positions per layer in VRAM, the rest in RAM` : ", all in VRAM"}` : null],
    ["Experts in VRAM", eng.expert_slots ? `${fmt(eng.expert_slots)} (${gb((eng.expert_cache_mib || 0) * 1048576)} GB)` : null],
    ["Speculation", eng.spec ? `MTP drafts up to ${Math.max(0, (eng.mtp_max || eng.spec) - 1)} tokens${eng.lookup ? ", prompt lookup on" : ""}` : null],
    ["Images", eng.images ? "on" : "off"],
    ["Experimental speed projection", projectionText(eng.cvec)],
  ]);
  facts($("facts-hw"), [
    ["GPU", st.gpu_name ? `${st.gpu_name}${hw.gpu_mem_total ? `, ${gb(hw.gpu_mem_total, 0)} GB` : ""}` : "not readable (NVML)"],
    ["CPU", st.cpu_name ? `${st.cpu_name}${st.threads ? `, ${st.threads} threads` : ""}` : null],
    ["RAM", hw.ram_total ? `${gb(hw.ram_total, 0)} GB` : null],
  ]);
  const base = location.origin;
  facts($("facts-api"), [
    ["OpenAI base URL", `${base}/v1`, true],
    ["Anthropic base URL", base, true],
    ["Model name", eng.model, true],
    ["Agent profiles", `${base}/prompts`, true],
  ]);
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) copyText(b.dataset.copy, b);
});
$("req-all").addEventListener("click", () => { reqShowAll = !reqShowAll; if (lastMetrics) render(lastMetrics); });

// ------------------------------------------------------------------ MCP servers (GET /mcp)
// Tools from the MCP servers in the run config: the chat offers them to the model (opt-in per request,
// "hypercore_mcp": true, which only this page sends); the Monitor lists the servers and what they offer.
let mcpInfo = {servers: [], tools: 0}, mcpRetry = null;
let mcpContext = null;   // GET /mcp "context": what this server does with an over-cap prompt
async function loadMcp() {
  try {
    const r = await fetch("mcp", {headers: headers()});
    if (!r.ok) return;
    mcpInfo = await r.json();
    // The page cannot count the tool block the template writes; the server can. Take it whenever
    // it is reported, including 0: a chat with no servers has no tool block at all.
    if (mcpInfo && mcpInfo.prefix && Number.isFinite(mcpInfo.prefix.tokens)) prefix = mcpInfo.prefix;
    if (mcpInfo && mcpInfo.context && Number.isFinite(mcpInfo.context.max_context)) mcpContext = mcpInfo.context;
  } catch (e) { return; /* an older server: no MCP */ }
  renderMcp();
  clearTimeout(mcpRetry);                          // right after the start, servers may still be starting (npx downloads)
  if ((mcpInfo.servers || []).some((s) => s.status === "starting")) mcpRetry = setTimeout(loadMcp, 3000);
}

// ------------------------------------------------------------------ agent profiles (GET /prompts)
// The menu is built at run time from prompt.json: adding a profile to that file adds a choice here with
// nothing changed in the page and no restart. The catalog is re-fetched only when it might have changed
// (the menu gaining focus, a new chat, a saved key); a fetch that returns the same set of keys leaves
// the menu - and the user's selection - alone.
let promptCatalog = {system: "", agents: []}, promptStamp = null;
async function loadPrompts(quiet = false) {
  try {
    const r = await fetch("prompts", {headers: headers()});
    if (!r.ok) return;                                   // an older server has no /prompts: nothing to offer
    const cat = await r.json();
    if (cat.error) {                                     // prompt.json is there but unreadable: say so
      if (!quiet) toast("error", "Agent profiles", cat.error, 8000);
      return;
    }
    const stamp = (cat.agents || []).map((a) => `${a.key}:${a.name}`).join("|");
    if (stamp === promptStamp) return;                   // unchanged: do not touch the menu
    promptStamp = stamp;
    promptCatalog = cat;
    renderAgents();
    if (!quiet) toast("success", "Agent profiles", `${(cat.agents || []).length} profile(s) ready.`, 4000);
  } catch (e) { /* not reachable: the dropdown keeps what it has */ }
}
function renderAgents() {
  const sel = $("agent-select");
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = "";
  const only = document.createElement("option"); only.value = ""; only.textContent = "System prompt only";
  sel.appendChild(only);
  for (const a of promptCatalog.agents || []) {
    const o = document.createElement("option");
    o.value = a.key;
    o.textContent = a.name + (a.role ? ` (${a.role})` : "");
    sel.appendChild(o);
  }
  sel.value = keep;
}
$("agent-select").addEventListener("focus", () => loadPrompts(true));   // silent refresh when it is opened
const MCP_STATE = {ready: ["st-badge--generating", "Connected"], starting: ["st-badge--reading", "Starting"],
                   failed: ["st-badge--error", "Failed"], stopped: ["st-badge--queued", "Stopped"],
                   disabled: ["", "Off"], idle: ["", "Waiting"]};
function renderMcp() {
  const servers = mcpInfo.servers || [];
  $("mcp-card").hidden = !servers.length;
  $("mcp-row").hidden = !servers.length;
  const ready = servers.filter((s) => s.enabled !== false && (s.status === "ready" || s.status === "stopped"));
  const off = servers.filter((s) => s.enabled === false).length;
  $("mcp-sum").textContent = servers.length ? `${fmt(mcpInfo.tools)} tools · ${ready.length} of ${servers.length} servers connected` +
    (off ? ` · ${off} off` : "") : "";
  $("mcp-row-sub").textContent = mcpInfo.tools ? `${fmt(mcpInfo.tools)} tools from ${ready.map((s) => s.name).join(", ")}; the model calls them when it decides to`
                                               : "no server is connected yet (see the Monitor)";
  $("mcp-list").innerHTML = servers.map((s) => {
    const [cls, text] = MCP_STATE[s.status] || ["", s.status];
    const info = s.info && s.info.name ? ` · ${s.info.name}${s.info.version ? ` ${s.info.version}` : ""}` : "";
    const on = s.enabled !== false;
    return `<div class="mcp-server"><div class="mcp-server__head"><span class="st-badge ${cls}">${esc(text)}</span>` +
      `<strong>${esc(s.name)}</strong><span class="muted small">${esc(s.transport)} · ${fmt(s.tools.length)} tools${esc(info)}</span>` +
      `<button type="button" class="st-toggle" data-mcp-server="${esc(s.name)}" role="switch" aria-checked="${on ? "true" : "false"}" aria-label="${esc(on ? "Turn off " + s.name : "Turn on " + s.name)}"></button></div>` +
      (s.error ? `<div class="msg-error">${esc(s.error)}</div>` : "") +
      (s.tools.length ? `<div class="mcp-server__tools">${s.tools.map((t) => `<span class="chip" title="${esc(t.description || "")}">${esc(t.tool)}</span>`).join("")}</div>` : "") +
      `</div>`;
  }).join("");
}
$("mcp-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-mcp-server]");
  if (!btn || btn.disabled) return;
  const name = btn.dataset.mcpServer;
  const enabled = btn.getAttribute("aria-checked") !== "true";
  btn.disabled = true;
  try {
    const r = await fetch("mcp", {method: "POST", headers: headers(true),
                                  body: JSON.stringify({server: name, enabled})});
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((body.error && body.error.message) || `HTTP ${r.status}`);
    mcpInfo = body;
    renderMcp();
    toast("info", enabled ? `${name} on` : `${name} off`,
      enabled ? "Starting that server. The model stays loaded." : "Its tools are left out of the next request. The model stays loaded.", 4000);
    if ((mcpInfo.servers || []).some((s) => s.status === "starting")) {
      clearTimeout(mcpRetry);
      mcpRetry = setTimeout(loadMcp, 1000);
    }
  } catch (err) {
    btn.disabled = false;
    toast("error", "Could not change the server", err.message || String(err), 6000);
  }
});

// ---------------------------------------------------------------- External assistants (GET /assist/log)
// The MCP card above is about the servers on this PC. This one is about what leaves it: the endpoint the
// Assist tab talks to, which models its dropdown is pinned to, which ones the last probe timed and left
// out, and every call that was made - the same visibility the MCP servers have.
let assistLog = null;
async function loadAssistMonitor() {
  try {
    const r = await fetch("assist/log", {headers: headers()});
    if (!r.ok) return;                                   // no key yet, or an older server without /assist/log
    assistLog = await r.json();
  } catch (err) { return; }
  renderAssistMonitor();
}

function renderAssistMonitor() {
  const a = assistLog;
  const card = $("assist-card");
  if (!card) return;
  if (!a) { card.hidden = true; return; }
  const models = a.models || [], over = a.over_limit || [], calls = a.calls || [], f = a.filter || {};
  card.hidden = false;
  $("assist-sum").textContent = a.configured
    ? `${models.length} pinned · ${over.length} not pinned · ${calls.length} call${calls.length === 1 ? "" : "s"} logged`
    : "no endpoint configured";
  $("assist-facts").innerHTML =
    `<dt>Endpoint</dt><dd><code>${esc(a.endpoint || "not set")}</code></dd>` +
    `<dt>API key</dt><dd>${a.key_present ? "read from .env" : "missing"}</dd>` +
    `<dt>Pinned list</dt><dd><code>${esc(f.file || "none: every model is offered")}</code>` +
      (f.updated ? ` · updated ${esc(timeStr(f.updated))}` : "") + `</dd>` +
    `<dt>Lag limit</dt><dd>${fmt(f.max_lag_s ?? 1.0, 1)} s over ${f.rounds ?? 2} rounds</dd>` +
    `<dt>Call log</dt><dd><code>${esc(a.log_file || "off (HYPERCORE_ASSIST_LOG_KEEP=0)")}</code></dd>` +
    (f.note ? `<dt>Path</dt><dd class="msg-error">${esc(f.note)}</dd>` : "") +
    (a.last_error ? `<dt>Last error</dt><dd class="msg-error">${esc(a.last_error)}</dd>` : "");
  $("assist-calls").innerHTML = calls.length ? calls.map((c) => {
    const bits = [timeStr(c.ts), `${fmt(c.latency_s, 2)} s`];
    if (c.completion_tokens) bits.push(`${fmt(c.completion_tokens)} tokens`);
    if (c.chars) bits.push(`${fmt(c.chars)} chars`);
    return `<div class="assist-call"><span class="st-badge ${c.ok ? "st-badge--generating" : "st-badge--error"}">${esc(c.kind)}</span>` +
      `<strong>${esc(c.model || "-")}</strong><span class="muted small">${esc(bits.join(" · "))}</span>` +
      (c.truncated ? `<span class="muted small">cut off at the token limit</span>` : "") +
      (c.prompt_head ? `<div class="muted small">“${esc(c.prompt_head)}”</div>` : "") +
      (c.error || c.note ? `<div class="msg-error">${esc(c.error || c.note)}</div>` : "") + `</div>`;
  }).join("") : `<p class="muted small">No Assist request has reached this server yet. Send one from the Assist tab.</p>`;
  if (over.length) {
    $("assist-calls").innerHTML += `<div class="assist-over"><span class="muted small">Not pinned:</span>` +
      over.map((m) => `<span class="chip" title="${esc(m.reason || "not measured")}: ${esc(m.error || `median first byte ${fmt(m.ttft_s, 2)} s`)}">${esc(m.id)}</span>`).join("") + `</div>`;
  }
  const unverified = models.filter((m) => m.ok === false);
  if (unverified.length) {
    $("assist-calls").innerHTML += `<div class="assist-over"><span class="muted small">Pinned without a measurement:</span>` +
      unverified.map((m) => `<span class="chip" title="${esc(m.error || "no measurement")}">${esc(m.id)}</span>`).join("") + `</div>`;
  }
}

// ------------------------------------------------------------------ Chat history (GET /history/status)
// The archive the Chat tab writes, and the state of the distillation that turns it into
// topic files. The numbers are counts of files, so this stays cheap enough to poll.
let historyState = null;
async function loadHistory() {
  try {
    const r = await fetch("history/status", {headers: headers()});
    if (!r.ok) return;                                      // no key yet, or an older server
    historyState = await r.json();
  } catch (e) { return; }
  renderHistory();
}

function renderHistory() {
  const card = $("history-card");
  if (!card || !historyState) return;
  const h = historyState, d = h.distil || {}, dist = h.distilled || {};
  $("history-sum").textContent = d.running ? "rebuilding" : `${h.topics} topics`;
  const last = d.last || {};
  $("history-facts").innerHTML = [
    ["Transcripts here", h.transcripts],
    ["Distilled from", dist.transcripts || h.summaries || 0],
    ["Chat summaries", h.summaries],
    ["Topic files", h.topics],
    ["Notes", h.notes],
    ["Last archived", h.newest || "none yet"],
    ["Last rebuild", last.started ? `${last.ok ? "ok" : "failed"} in ${fmt(last.seconds, 1)} s` : "none this session"],
  ].map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(String(v))}</dd></div>`).join("");
  $("history-rebuild").disabled = !!d.running;
  card.hidden = false;
}

$("history-rebuild").onclick = async () => {
  const btn = $("history-rebuild");
  btn.disabled = true; btn.textContent = "Rebuilding";
  try {
    const res = await fetch("/history/distil", {method: "POST", headers: headers(true), body: "{}"});
    const out = await res.json();
    toast(out.ok ? "success" : "error", "History rebuild", out.ok ? (out.output || "done") : (out.error || "failed"), 8000);
  } catch (e) {
    toast("error", "Could not rebuild the history", e.message || String(e), 8000);
  } finally {
    btn.textContent = "Rebuild now"; loadHistory();
  }
};

// ------------------------------------------------------------------ Model settings (GET / POST /config, #564)
// A few documented keys of the run config (hypercore-<model>.json), for every client, from the next start on.  The
// server lists them, checks every value and keeps every other key of the file as it is.
let cfgKeys = [];
async function loadConfig() {
  let r;
  try { r = await fetch("config", {headers: headers()}); } catch (e) { return; }
  if (!r.ok) { $("cfg-card").hidden = true; return; }       // no run config, an older server, or no key yet
  const c = await r.json();
  cfgKeys = c.keys || [];
  $("cfg-file").textContent = c.file || "";
  $("cfg-form").innerHTML = cfgKeys.map((k, i) => {
    const id = `cfg-${i}`, v = k.value;
    let input;
    if (k.kind === "bool" || k.kind === "enum") {
      const opts = k.kind === "bool" ? [["true", "on"], ["false", "off"]] : k.choices.map((x) => [x, x]);
      const cur = v == null ? "" : String(v);
      input = `<select class="st-input" id="${id}"><option value=""${cur === "" ? " selected" : ""}>default</option>` +
        opts.map(([val, text]) => `<option value="${esc(val)}"${cur === val ? " selected" : ""}>${esc(text)}</option>`).join("") + `</select>`;
    } else {
      const text = v == null ? "" : Array.isArray(v) ? v.join(", ") : String(v);
      input = `<input class="st-input" id="${id}" ${k.kind === "number" ? 'type="number" step="any" min="0"' : 'type="text"'} ` +
        `value="${esc(text)}" placeholder="default" autocomplete="off">`;
    }
    return `<label for="${id}" title="${esc(k.help)}">${esc(k.help)}<code>${esc(k.key)}</code></label>${input}`;
  }).join("");
  $("cfg-card").hidden = false;
}
function configValue(k, el) {
  const s = el.value.trim();
  if (s === "") return null;
  if (k.kind === "bool") return s === "true";
  if (k.kind === "number") return Number(s);
  return s;                                         // enum, or names (the server splits them at commas)
}
$("cfg-save").addEventListener("click", async () => {
  const set = {};
  cfgKeys.forEach((k, i) => {
    const v = configValue(k, $(`cfg-${i}`));
    const old = Array.isArray(k.value) ? k.value.join(", ") : k.value;
    if (JSON.stringify(v) !== JSON.stringify(old ?? null)) set[k.key] = v;
  });
  if (!Object.keys(set).length) { $("cfg-msg").textContent = "Nothing changed."; return; }
  try {
    const r = await fetch("config", {method: "POST", headers: headers(true), body: JSON.stringify({set})});
    const b = await r.json();
    if (!r.ok) throw new Error((b.error || {}).message || `HTTP ${r.status}`);
    $("cfg-msg").textContent = b.changed.length
      ? `Saved (${b.changed.join(", ")}); the earlier file is ${b.file}.bak. Start the model again to use it.` : "Nothing changed.";
    loadConfig();
  } catch (e) {
    $("cfg-msg").textContent = "";
    toast("error", "Not saved", String(e.message || e), 6000);
  }
});

// ------------------------------------------------------------------ Markdown (escaped first, then formatted)
function inline(s) {
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = esc(s)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code class="inline">${esc(codes[+i])}</code>`);
}
function codeBlock(lang, code) {
  return `<div class="st-code"><div class="st-code__head"><span>${esc(lang || "code")}</span>` +
    `<button class="st-btn st-btn--icon" data-code-copy aria-label="Copy code">${icon("copy")}</button></div>` +
    `<pre><code>${esc(code)}</code></pre></div>`;
}
function blocks(text) {
  const out = [], lines = text.split("\n");
  let para = [], list = null;
  const flushPara = () => { if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; };
  const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    let m;
    if (!l.trim()) { flushPara(); flushList(); continue; }
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { flushPara(); flushList(); out.push(`<${m[1].length <= 2 ? "h3" : "h4"}>${inline(m[2])}</${m[1].length <= 2 ? "h3" : "h4"}>`); continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(l)) { flushPara(); flushList(); out.push("<hr>"); continue; }
    if ((m = l.match(/^>\s?(.*)$/))) { flushPara(); flushList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
      flushPara(); flushList();
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      let html = `<table><thead><tr>${cells(l).map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) html += `<tr>${cells(lines[i++]).map((c) => `<td>${c}</td>`).join("")}</tr>`;
      i--;
      out.push(html + "</tbody></table>");
      continue;
    }
    if ((m = l.match(/^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/))) {
      flushPara();
      const tag = m[1] ? "ol" : "ul";
      if (!list || list.tag !== tag) { flushList(); list = {tag, items: []}; }
      list.items.push(m[2]);
      continue;
    }
    if (list && /^\s{2,}\S/.test(l)) { list.items[list.items.length - 1] += " " + l.trim(); continue; }
    flushList();
    para.push(l);
  }
  flushPara(); flushList();
  return out.join("");
}
function markdown(text) {
  let html = "", rest = text;
  for (;;) {
    const m = rest.match(/(^|\n)```([^\n`]*)\n/);
    if (!m) { html += blocks(rest); break; }
    html += blocks(rest.slice(0, m.index));
    rest = rest.slice(m.index + m[0].length);
    const end = rest.match(/(^|\n)```[ \t]*(\n|$)/);
    if (!end) { html += codeBlock(m[2].trim(), rest); break; }         // still streaming
    html += codeBlock(m[2].trim(), rest.slice(0, end.index));
    rest = rest.slice(end.index + end[0].length);
  }
  return html;
}

// ------------------------------------------------------------------ Chat
const DEFAULTS = {thinking: "high", temperature: 0.6, top_p: 0.95, top_k: 20, max: "", seed: "", show: true, esp: true, mcp: true, ctx: 132000};
let settings = {...DEFAULTS, ...store.get("sampling", {})};
let messages = store.get("chat", []);
// The handoff the server (or an earlier send) substituted for the older turns.
// The on-screen transcript stays whole; the next request sends this prefix plus
// the newest user message and what followed it.
let compactState = store.get("compact", null);
let compactedDuringTurn = false;
function saveCompact() {
  if (compactState) store.set("compact", compactState);
  else store.set("compact", null);
}
let attachments = [];                 // {name, url}
let busy = null;                      // {controller, msg}

function saveChat() {
  store.set("chat", messages.map((m) => ({...m, images: (m.images || []).map((i) => ({name: i.name})),
                                           files: (m.files || []).map((f) => ({name: f.name}))})));
}
function timeStr(t) { return new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }

function msgEl(m, i) {
  const el = document.createElement("div");
  el.className = `st-msg st-msg--${m.role}`;
  el.dataset.i = i;
  if (m.role === "user") {
    if (m.files && m.files.length) {
      const wrap = document.createElement("div");
      wrap.className = "msg-images";
      for (const f of m.files) {
        const c = document.createElement("span");
        c.className = "chip";
        c.innerHTML = icon("attach", "st-icon st-icon--sm");
        c.append(f.name);
        wrap.appendChild(c);
      }
      el.appendChild(wrap);
    }
    if (m.images && m.images.length) {
      const wrap = document.createElement("div");
      wrap.className = "msg-images";
      for (const im of m.images) {
        if (im.url) { const img = document.createElement("img"); img.src = im.url; img.alt = im.name || "image"; wrap.appendChild(img); }
        else { const c = document.createElement("span"); c.className = "chip"; c.innerHTML = icon("image", "st-icon st-icon--sm"); c.append(im.name || "image"); wrap.appendChild(c); }
      }
      el.appendChild(wrap);
    }
    const b = document.createElement("div");
    b.className = "st-bubble";
    b.textContent = m.text;
    el.appendChild(b);
    const meta = document.createElement("div");
    meta.className = "st-msg__meta";
    meta.textContent = `You · ${timeStr(m.time)}`;
    el.appendChild(meta);
  } else {
    el.innerHTML = `<details class="st-collapse think" hidden><summary>${icon("thinking", "st-icon st-icon--sm")}<span class="think-title"></span>` +
      `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary><div class="st-collapse__body thinking"></div></details>` +
      `<div class="st-bubble"></div><div class="st-msg__meta"><span class="meta-text"></span>` +
      `<button class="st-btn st-btn--icon" data-msg-copy aria-label="Copy the answer" title="Copy">${icon("copy")}</button></div>`;
    updateAssistant(el, m, false);
  }
  return el;
}
// One MCP tool call in the answer: a compact block (name, state, a one-line preview) that opens to the arguments and
// the result as the model read it.  Its body is built only while open: a result can be 20,000 characters.
const TOOL_STATE = {writing: ["st-badge--reading", "Writing"], running: ["st-badge--generating", "Running"], done: ["", "Done"],
                    error: ["st-badge--error", "Error"], skipped: ["st-badge--queued", "Not run"]};
function toolHtml(t, k) {
  const [cls, label] = TOOL_STATE[t.state] || ["", t.state];
  const args = t.arguments == null ? "" : JSON.stringify(t.arguments, null, 2);
  const preview = t.result != null ? t.result : args.replace(/\s+/g, " ");
  let body = "";
  if (t.open) {
    body = `<div class="tool-call__label">Arguments</div><pre class="tool-call__pre">${esc(args || "(being written)")}</pre>`;
    if (t.result != null) {
      body += `<div class="tool-call__label">${t.ok ? "Result" : "Error"}${t.chars ? ` · ${fmt(t.chars)} characters` : ""}` +
              `${t.truncated ? ", cut for the model" : ""}</div><pre class="tool-call__pre">${esc(t.result)}</pre>`;
    }
  }
  return `<details class="st-collapse tool-call" data-tool="${k}" data-state="${esc(t.state)}"${t.open ? " open" : ""}>` +
    `<summary>${icon("tool", "st-icon st-icon--sm")}<span class="tool-call__name" title="${esc(t.name || "")}">${esc(t.tool || t.name || "tool")}</span>` +
    (t.server ? `<span class="muted small">${esc(t.server)}</span>` : "") +
    `<span class="tool-call__preview muted">${esc(preview.slice(0, 200))}</span>` +
    `<span class="st-badge ${cls}">${esc(label)}</span>${t.ms != null && t.state !== "skipped" ? `<span class="muted small">${fmt(t.ms / 1000, 1)} s</span>` : ""}` +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary><div class="st-collapse__body">${body}</div></details>`;
}
// the answer's text with the tool blocks where the model called them
function answerHtml(m) {
  const note = m.compaction
    ? `<details class="st-collapse"><summary>Context compacted</summary><pre class="tool-call__pre">${esc(m.compaction)}</pre></details>`
    : "";
  if (!m.tools || !m.tools.length) return note + markdown(m.text || "");
  let html = "", pos = 0;
  m.tools.forEach((t, k) => {
    const at = Math.min(Math.max(t.at || 0, pos), m.text.length);
    if (at > pos) html += markdown(m.text.slice(pos, at));
    pos = at;
    html += toolHtml(t, k);
  });
  return note + html + markdown(m.text.slice(pos));
}
// a tool event from the stream (the `hypercore_mcp` field of a chunk)
function onTool(m, x) {
  if (x.event === "tail_check") {                    // the end-of-turn "is the work done?" check (turn_tail_check)
    m.tailChecks = (m.tailChecks || 0) + 1;          // not a tool call: no row, just the hint
    beat = x.result === "continue" ? {check: true, since: Date.now()} : null;
    paintBeat();
    return;
  }
  if (x.event === "limit") {
    // Which cap was hit: the round cap (mcp.max_rounds) or the call cap (mcp.max_tool_calls).
    m.limit = x.max_rounds;
    m.limitCalls = x.max_tool_calls || 0;
    m.limitRounds = x.rounds ?? 0;
    m.limitCallsRun = x.calls ?? 0;
    beat = null; paintBeat(); return;
  }
  if (x.event === "condensed") {                    // the condenser shortened earlier results (mcp.yaml)
    m.condensed = (m.condensed || 0) + (x.chars || 0);
    paintBeat();
    return;
  }
  if (x.event === "compacting") {
    beat = {compact: true, since: Date.now()};
    paintBeat();
    return;
  }
  if (x.event === "compacted") {
    const omit = new Set([...(compactState && compactState.omit || []), ...(x.omit_tool_ids || [])]);
    compactState = {prefix: x.prefix || [], omit: [...omit]};
    compactedDuringTurn = true;
    saveCompact();
    m.compaction = x.summary || "";
    beat = null;
    toast("info", "Context compacted", "Earlier turns were summarized so this one can continue.", 6000);
    paintBeat();
    return;
  }
  m.tools = m.tools || [];
  let t = m.tools.find((y) => y.id === x.id);
  if (!t) { t = {id: x.id, name: x.name, at: m.text.length, rat: m.reasoning.length, state: "writing"}; m.tools.push(t); }
  if (x.event === "call") {
    Object.assign(t, {name: x.name, server: x.server, tool: x.tool, arguments: x.arguments, round: x.round, state: "running"});
    beat = {name: x.name, since: Date.now(), round: x.round};
    paintBeat();
  } else if (x.event === "result") {
    Object.assign(t, {result: x.text, ok: x.ok, chars: x.chars, truncated: x.truncated, ms: x.ms,
                      state: x.skipped ? "skipped" : x.ok ? "done" : "error"});
    beat = null;
    paintBeat();
  }
}
function updateAssistant(el, m, streaming) {
  const det = el.querySelector("details.think");
  if (m.reasoning) {
    det.hidden = false;
    const thinkingNow = streaming && !m.text;
    el.querySelector(".think-title").textContent = thinkingNow ? "Thinking…" :
      m.thinkSecs != null ? `Thought for ${fmt(m.thinkSecs, 1)} s` : "Thoughts";
    const body = el.querySelector(".thinking");
    if (det.open || thinkingNow) body.textContent = m.reasoning;
    else body.dataset.pending = "1";
    // open while it streams (if wanted), closed once the answer starts - unless the user toggled it themselves
    if (thinkingNow && settings.show && !det.dataset.touched && !det.open) { det._auto = true; det.open = true; }
    if (!thinkingNow && det.open && !det.dataset.touched) { det._auto = true; det.open = false; }
  }
  const bubble = el.querySelector(".st-bubble");
  if (m.error) {
    bubble.innerHTML = `<div class="msg-error"></div>`;
    bubble.firstChild.textContent = m.error;
  } else if (!m.text && streaming && !(m.tools && m.tools.length)) {
    bubble.innerHTML = m.reasoning ? `<span class="muted cursor">Writing</span>` : `<span class="cursor"></span>`;
  } else {
    bubble.innerHTML = answerHtml(m);
    if (streaming) bubble.classList.add("cursor"); else bubble.classList.remove("cursor");
  }
  el.querySelector(".meta-text").textContent = m.meta || (streaming ? "" : m.stopped ? "Stopped" : "");
  el.querySelector("[data-msg-copy]").hidden = streaming || !m.text;
}
function renderChat() {
  const chat = $("chat");
  chat.querySelectorAll(".st-msg").forEach((e) => e.remove());
  $("chat-empty").hidden = messages.length > 0;
  messages.forEach((m, i) => chat.appendChild(msgEl(m, i)));
  scrollDown(true);
}
function nearBottom() { const s = $("chat-scroll"); return s.scrollHeight - s.scrollTop - s.clientHeight < 120; }
function scrollDown(force) { const s = $("chat-scroll"); if (force || nearBottom()) s.scrollTop = s.scrollHeight; }

// Two transcripts, one renderer: #chat and #assist-chat both build their messages with msgEl and
// updateAssistant, so every handler has to find the array its own message lives in. Bound to one
// container only, the Assist tab had no listener at all: its thought panel rendered empty and stayed
// empty however many times it was opened, while the copy button read the chat transcript instead.
function transcriptOf(el) {
  return el.closest("#assist-chat") ? assistMessages : messages;
}

function bindTranscript(container) {
  if (!container) return;
  container.addEventListener("click", (e) => {
    const cc = e.target.closest("[data-code-copy]");
    if (cc) { copyText(cc.closest(".st-code").querySelector("pre").textContent, cc); return; }
    const mc = e.target.closest("[data-msg-copy]");
    if (mc) { const arr = transcriptOf(mc); copyText(arr[+mc.closest(".st-msg").dataset.i].text, mc); return; }
    // a tool block: its open state lives in the message (the answer is rebuilt while it streams), so the click sets it
    const sum = e.target.closest(".tool-call > summary");
    if (sum) {
      e.preventDefault();
      const el = sum.closest(".st-msg"), arr = transcriptOf(el);
      const m = arr[+el.dataset.i], t = m && m.tools && m.tools[+sum.parentElement.dataset.tool];
      if (!t) return;
      t.open = !t.open;
      updateAssistant(el, m, !!busy && busy.msg === m);
    }
  });
  container.addEventListener("toggle", (e) => {
    const d = e.target;
    if (d.tagName !== "DETAILS" || !d.classList.contains("think")) return;
    if (d._auto) { d._auto = false; return; }          // our own open/close, not the user's
    d.dataset.touched = "1";
    const body = d.querySelector(".thinking");
    if (!d.open || !body.dataset.pending) return;
    const arr = transcriptOf(d);
    const m = arr[+d.closest(".st-msg").dataset.i];
    if (m) body.textContent = m.reasoning;
    delete body.dataset.pending;
  }, true);
}
bindTranscript($("chat"));
bindTranscript($("assist-chat"));

// ---------------------------------------------------------------- context window
// The engine has no history truncation: a prompt over --max-context is answered 400, never trimmed.
// So the chat bounds its own payload here. Turns that fall out of the window become one handoff,
// and the newest turns stay verbatim, which lets a conversation go on past the window instead of
// failing at it. The on-screen transcript is not cut; only the request is.
//
// Two things make this cheap rather than a re-prefill every turn:
//   hysteresis - once trimming starts it goes down to LOW_WATER, not to the cap, so the window is
//              unchanged for the turns that follow and the engine keeps reusing the whole prefix.
//              Only the turn that crosses the cap pays a re-prefill.
//   whole turns - a tool_call is never separated from its result, and the profile system turn stays.
//
// The browser has no tokenizer, so the count is anchored on the prompt_tokens the server reported for
// the last request and adjusted by characters from there: exact at the moment the cap bites, and
// never worse than a proportion away from it. Before the first request there is no anchor, so the
// template's own overhead comes from GET /mcp, which measures it with the real tokenizer and the real
// tool list. The fallback is only for a server old enough not to report it.
const CHARS_PER_TOKEN = 4;          // this model's BPE, measured on prose
const LOW_WATER = 0.85;             // trim to this fraction of the cap, then stay quiet until it fills
const MIN_TURN_TOKENS = 256;        // the smallest room the newest turn is ever left
const MARKER_CHARS = 72;            // the "characters omitted" line headTail inserts when it cuts
const PREFIX_FALLBACK = 6200;       // the tool block + system prompt the template adds, for a server that says nothing
let prefix = null;                  // what /mcp reported: {tokens, base}; null until it answers
let anchor = {tokens: 0, chars: 0, ratio: 1 / CHARS_PER_TOKEN};   // the payload the last request actually sent, and the tokens per character the engine measured on it

// The tool block is only in the prompt when this request offers the tools ("hypercore_mcp": true).
// `offering` is passed by send() while it is working out whether this turn can carry the tools at
// all; with nothing passed it follows the setting the user chose.
function templatePrefix(offering) {
  if (offering === undefined) offering = settings.mcp !== false;
  if (!prefix) return PREFIX_FALLBACK;   // an older server: keep the number this page used to hardcode
  return offering && mcpInfo.tools > 0 ? prefix.tokens : prefix.base;
}

function payloadChars(payload) {
  return payload.reduce((n, m) => n + String(m.content || "").length
                       + JSON.stringify(m.tool_calls || []).length, 0);
}

function estimateTokens(payload, offering) {
  const chars = payloadChars(payload);
  if (anchor.tokens) {
    // The engine counted this exact kind of content once, so use its ratio rather than the
    // prose constant: a table, a log or a JSON file is denser than four characters a token.
    const ratio = anchor.ratio || 1 / CHARS_PER_TOKEN;
    let tokens = anchor.tokens + Math.round((chars - anchor.chars) * ratio);
    // The anchor was measured with the tools the page offered. Turning them off removes exactly
    // the block /mcp counted with the real tokenizer, so the tools-off fallback can still be tried.
    if (!offering && anchor.offerTools && prefix) tokens -= prefix.tokens - prefix.base;
    return Math.max(0, tokens);
  }
  return Math.round(chars / CHARS_PER_TOKEN) + templatePrefix(offering);
}

// A turn is a user message and everything the model produced before the next user message.
function turnEnd(msgs) {
  for (let i = 1; i < msgs.length; i++) if (msgs[i].role === "user") return i;
  return Math.max(1, msgs.length - 1);
}

const HANDOFF_MARK = "compacted by HyperCore";

function messagePlain(m) {
  const c = m.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map((p) => (p && p.text) || "").join("\n");
  return "";
}
function isHandoff(m) {
  return m.role === "user" && messagePlain(m).includes(HANDOFF_MARK);
}
function summarizeDropped(turns) {
  const lines = [];
  for (const m of turns) {
    const text = messagePlain(m).trim();
    if (m.role === "user") lines.push("User: " + (text.split("\n")[0] || "").slice(0, 300));
    else if (m.role === "assistant") {
      const paras = text.split("\n").map((p) => p.trim()).filter(Boolean);
      if (paras.length) lines.push("Assistant: " + paras[paras.length - 1].slice(0, 400));
    } else if (m.role === "tool" && text) lines.push("Tool result: " + text.replace(/\s+/g, " ").slice(0, 200));
  }
  const body = lines.join("\n") || "No earlier conversation.";
  return "The previous conversation was compacted. Use this summary as context:\n\n" + body +
    "\n\n[" + HANDOFF_MARK + "]";
}
function headTail(text, keep) {
  if (text.length <= keep) return text;
  if (keep <= 0) return "";              // no room at all: the turn contributes nothing
  if (keep < 80) return text.slice(0, keep);   // too short to keep both ends meaningfully
  const head = Math.floor(keep / 2), tail = keep - head;
  const omitted = text.length - head - tail;
  return text.slice(0, head) + `\n\n[... ${omitted} characters omitted; the beginning and the end are kept]\n\n` +
    text.slice(-tail);
}

// How much room a turn has depends on every other turn in the payload, so one slice cannot get it
// right. Clip the largest turn by exactly the amount the estimate is over, then measure again: each
// pass removes characters, so it converges, and it stops when there is nothing left that can be cut
// - which is the signal that the profile and the tool block alone have used the window.
function clipToFit(kept, cap, offering) {
  let clipped = 0;
  for (let pass = 0; pass < 40; pass++) {
    const over = estimateTokens(kept, offering) - cap;
    if (over <= 0) return clipped;
    const cut = (over + 1) * CHARS_PER_TOKEN + MARKER_CHARS;   // the estimate's rounding and the marker
    const cuttable = kept.map((m, i) => [i, m])
      .filter(([i, m]) => m.role !== "system" && typeof m.content === "string" && m.content.length > 0)
      .sort((a, b) => a[1].content.length - b[1].content.length);   // the biggest last
    if (!cuttable.length) return clipped;
    const [i, m] = cuttable[cuttable.length - 1];
    const next = {...m, content: headTail(m.content, Math.max(0, m.content.length - cut))};
    if (next.content.length >= m.content.length) return clipped;    // no progress: stop
    kept[i] = next;
    clipped += m.content.length - next.content.length;
  }
  return clipped;
}

function pruneToContext(payload, cap, offering) {
  if (!cap || cap <= 0) return {messages: payload, cap: 0, dropped: 0, droppedChars: 0, clipped: 0, tokens: 0, summary: "", fits: true};
  const head = [];                                   // leading system turns are the profile: they stay
  let i = 0;
  while (i < payload.length && payload[i].role === "system") head.push(payload[i++]);
  const body = payload.slice(i);
  // Trim to the low-water mark for hysteresis, but never below the room the newest turn needs:
  // when the tool block alone is past the low-water line, clipping to it would empty the turn.
  const target = Math.max(Math.floor(cap * LOW_WATER), Math.min(cap, templatePrefix(offering) + MIN_TURN_TOKENS));
  let dropped = 0, droppedChars = 0, summary = "";
  // Crossing the cap is the only trigger; once it fires, go down to the low-water mark so the
  // window stays byte-identical for the turns that follow. Under the cap the payload is not
  // reordered, or the engine would re-prefill a prompt that had not changed.
  if (estimateTokens(head.concat(body), offering) <= cap) {
    const kept = head.concat(body);
    return {messages: kept, cap, target, dropped: 0, droppedChars: 0, clipped: 0,
            tokens: estimateTokens(kept, offering), summary: "", fits: true};
  }
  // A handoff already in the payload is the memory of turns dropped earlier. Pull it out
  // so the trim cannot drop it while it is dropping the turns around it.
  const handoffs = [];
  for (let j = body.length - 1; j >= 0; j--) if (isHandoff(body[j])) handoffs.unshift(body.splice(j, 1)[0]);
  const estimate = () => estimateTokens(head.concat(handoffs, body), offering);
  const droppedMsgs = [];
  while (body.length > 1 && estimate() > target) {
    const end = turnEnd(body);
    if (end >= body.length) break;                  // only the newest turn is left
    const slice = body.splice(0, end);
    droppedMsgs.push(...slice);
    dropped += 1;
    droppedChars += payloadChars(slice);
  }
  if (droppedMsgs.length) {
    summary = summarizeDropped(droppedMsgs);
    handoffs.unshift({role: "user", content: summary});
  }
  const kept = head.concat(handoffs, body);
  // Whole turns are gone; what is left can still be one turn bigger than the window. Clip it to
  // the low-water mark, not to the cap: the estimate is a character count, and the engine's BPE
  // is not exactly four characters a token.
  const clipped = clipToFit(kept, target, offering);
  return {messages: kept, cap, target, dropped, droppedChars, clipped,
          tokens: estimateTokens(kept, offering), summary, fits: estimateTokens(kept, offering) <= cap};
}

// The turn the page can actually send. The tool block is the largest thing in the prompt and it
// grows with every MCP server added, so a chat that cannot fit with it is sent without it rather
// than answered 400 by the engine: the question still gets an answer, just no tool calls.
function buildRequest(payload, cap) {
  const offering = settings.mcp !== false && mcpInfo.tools > 0;
  const withTools = pruneToContext(payload, cap, offering);
  if (withTools.fits || !offering) return {...withTools, offerTools: offering};
  const without = pruneToContext(payload, cap, false);
  return without.fits ? {...without, offerTools: false} : withTools;
}

// The engine measured the prompt with the real tokenizer and said so in the 400: "prompt (355050
// tokens) leaves no room to answer in the context (132000)". That is an exact characters-per-token
// ratio for the content this page is sending - prose, code, or a table - which the character count
// above can only guess at. Returning it lets the turn be re-cut with the measurement and tried once.
// Whether a turn the page cannot clip into the window is still worth sending. A plain engine
// answers 400 and never truncates, so the page must not send it. A server started with
// --trim-context fits the prompt itself (serve/server.py:2439 drops the oldest turns before
// tokenising), so the turn can go whole and the tokenizer decides. ctxInfo is the "context"
// block GET /mcp reports: {max_context, trim_context, fit_max_tokens}. An older server reports
// no such block, which is the safe case: refuse.
function serverCanFit(kept, ctxInfo) {
  if (!ctxInfo || !ctxInfo.trim_context) return false;
  const max = Number(ctxInfo.max_context) || 0;
  return max > 0 && Number.isFinite(kept.tokens) && kept.tokens <= max;
}

function overflowRatio(message, payload) {
  const m = /prompt \((\d+) tokens\)[\s\S]*?context \((\d+)\)/i.exec(String(message || ""));
  if (!m) return null;
  const chars = payloadChars(payload);
  if (chars < 16) return null;
  return {tokens: +m[1], chars, ctx: +m[2]};
}

// The first request of a chat has no anchor, so its size comes from the character guess. Measured
// against this engine on a real attachment, that guess is off by about a fifth: markdown, tables and
// code fence at 3.14-3.29 characters a token, not the 4 assumed here. The server can count a prompt
// exactly without running the model - POST /v1/messages/count_tokens renders and tokenizes it
// (serve/server.py:4568, ~45 ms). That count carries no tool block, so the tool definitions are
// added from the prefix /mcp measured with the real tokenizer.
function anchorFromCount(count, payload, offering) {
  const chars = payloadChars(payload);
  if (!Number.isFinite(count) || count <= 0 || chars < 16) return false;
  const block = offering && mcpInfo.tools > 0 && prefix ? prefix.tokens - prefix.base : 0;
  const total = count + block;
  const ratio = (total - templatePrefix(offering)) / chars;
  anchor = {tokens: total, chars, ratio: Math.min(1, Math.max(0.05, ratio)), offerTools: offering};
  return true;
}

// Worth measuring only when the turn is close to the window; a small turn cannot overflow it, and
// the count costs a round trip.
function nearCap(payload, cap, offering) {
  if (!cap || cap <= 0) return false;
  return estimateTokens(payload, offering) >= cap * 0.9;
}

function apiMessages() {
  const out = [];
  const prefix = (compactState && compactState.prefix) || [];
  let source = messages;
  if (prefix.length) {
    let start = 0;
    for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === "user") { start = i; break; }
    source = messages.slice(start);
    for (const m of prefix) out.push({role: m.role, content: m.content});
  }
  for (const m of source) {
    if (m.role === "user") {
      const imgs = (m.images || []).filter((i) => i.url);
      const text = userText(m);
      out.push({role: "user", content: imgs.length ? [{type: "text", text},
        ...imgs.map((i) => ({type: "image_url", image_url: {url: i.url}}))] : text});
    } else if (!m.error) {
      out.push(...assistantMessages(m));
    }
  }
  return out;
}
// An answer that used MCP tools goes back as the model wrote it: per round the text before the calls, the calls and
// their results (as the model read them), then the rest - so the next question can build on what the tools found.
function assistantMessages(m) {
  const omit = new Set((compactState && compactState.omit) || []);
  const ran = (m.tools || []).filter((t) => t.round != null && t.result != null && t.state !== "skipped" && !omit.has(t.id));
  if (!ran.length) return m.text ? [{role: "assistant", content: m.text}] : [];
  const out = [];
  let pos = 0;
  for (const r of [...new Set(ran.map((t) => t.round))]) {
    const calls = ran.filter((t) => t.round === r);
    const at = Math.min(Math.max(pos, calls[0].at || 0), m.text.length);
    out.push({role: "assistant", content: m.text.slice(pos, at).trim(),
              tool_calls: calls.map((t) => ({id: t.id, type: "function", function: {name: t.name, arguments: JSON.stringify(t.arguments || {})}}))});
    for (const t of calls) out.push({role: "tool", tool_call_id: t.id, content: t.result});
    pos = at;
  }
  const rest = m.text.slice(pos).trim();
  if (rest) out.push({role: "assistant", content: rest});
  return out;
}

// The composer's Stop button is a two-state control: Stop while a turn runs, Start once it
// has ended. Start sends RESUME_PROMPT, so a chat that stopped mid-work picks up where it
// left off without the user typing anything. turnEnded keeps the button out of the way
// before the first turn of a chat has finished.
let turnEnded = false;

function paintStopButton(mode) {
  const btn = $("stop-btn"), icon = $("stop-icon"), text = $("stop-text");
  btn.dataset.mode = mode;
  if (mode === "stop") {
    btn.hidden = false;
    icon.setAttribute("href", "web/sprite.svg#i-stop");
    text.textContent = "Stop";
    btn.title = "Stop this answer";
    btn.setAttribute("aria-label", "Stop this answer");
    return;
  }
  btn.hidden = !turnEnded;
  icon.setAttribute("href", "web/sprite.svg#i-bolt");
  text.textContent = "Start";
  btn.title = `Start a turn: ask the model to "${RESUME_PROMPT}"`;
  btn.setAttribute("aria-label", "Start: complete and open tasks");
}

function setBusy(on) {
  paintStopButton(on ? "stop" : "start");
  $("send-btn").disabled = on;
  beat = null;
  $("composer-hint").textContent = beatText();
}

// A turn is over: the composer's button becomes Start, and Start has something to do.
function endTurn() {
  turnEnded = true;
  setBusy(false);
}

// ------------------------------------------------------------------ heartbeat
// Between the rounds of a tool-using answer the stream is quiet: the tool is running on the server and
// the model is re-reading its result. That is not a hang, and a silent spinner cannot tell the two
// apart. The page says what it can from the events this request already carries - which tool, which
// round, how long - and ticks on the keep-alive lines the server sends while a tool runs. No new
// endpoint is needed: the Monitor's own /status poll already drives the header pill.
let beat = null;                                   // {name, since, round} while a tool is running
function beatText() {
  if (!beat) return busy ? "Thinking\u2026" : "Shift+Enter: new line";
  if (beat.compact) return "Compacting earlier context\u2026";
  if (beat.check) return "Checking the work against the request\u2026";
  return `Running ${beat.name} \u00b7 round ${beat.round} \u00b7 ${fmt((Date.now() - beat.since) / 1000, 1)} s`;
}
function paintBeat() { const h = $("composer-hint"); if (h) h.textContent = beatText(); }

async function send() {
  const text = $("input").value.trim();
  if ((!text && !attachments.length) || busy) return;
  messages.push({role: "user", text, images: attachments.filter((a) => a.kind !== "file"),
                 files: attachments.filter((a) => a.kind === "file"), time: Date.now()});
  attachments = [];
  renderAttachments();
  $("input").value = "";
  autosize();
  const m = {role: "assistant", text: "", reasoning: "", time: Date.now()};
  messages.push(m);
  renderChat();
  const el = $("chat").lastElementChild;
  const controller = new AbortController();
  compactedDuringTurn = false;
  busy = {controller, msg: m};
  setBusy(true);

  // prompt.json's system prompt opens every chat, and the agent profile follows it; both are system
  // turns, which the chat template merges into one. pruneToContext keeps leading system turns whole.
  const agent = (promptCatalog.agents || []).find((a) => a.key === $("agent-select").value);
  const payload = apiMessages();
  if (agent && agent.prompt) payload.unshift({role: "system", content: agent.prompt});
  if (promptCatalog.system) payload.unshift({role: "system", content: promptCatalog.system});
  // The tool block is the largest thing in the prompt and it grows with every MCP server added.
  // buildRequest offers the tools only when this turn can carry them, and clips the payload to the
  // window. A turn that cannot fit at all is reported, not sent: the engine refuses it with 400
  // and never truncates, so sending it would leave an empty bubble in the chat. A server started
  // with --trim-context is the exception: it fits the prompt itself, so the turn can go whole.
  // A turn near the window is measured with the engine's own tokenizer before anything is cut.
  // Only the first request of a chat needs this: after any response the anchor is already exact.
  const offering = settings.mcp !== false && mcpInfo.tools > 0;
  if (!anchor.tokens && nearCap(payload, settings.ctx, offering)) {
    try {
      const r = await fetch("v1/messages/count_tokens", {method: "POST", headers: headers(true),
        body: JSON.stringify({model: health.model, messages: payload})});
      if (r.ok) anchorFromCount((await r.json()).input_tokens, payload, offering);
    } catch (e) { /* no count endpoint on this server: the character estimate stands */ }
  }
  let kept = buildRequest(payload, settings.ctx);
  if (!kept.fits) {
    if (serverCanFit(kept, mcpContext)) {
      toast("warn", "Sent over the cap",
        `This turn is ${fmt(kept.tokens - settings.ctx)} tokens over the ${ctxfmt(settings.ctx)} cap. ` +
        `This server trims the oldest turns to fit its ${ctxfmt(mcpContext.max_context)} context.`, 8000);
    } else {
      m.error = `Nothing fits the ${ctxfmt(settings.ctx)} token context: the profile and the tool definitions take ${fmt(kept.tokens)} tokens on their own. Turn the tools off under About > Settings, or raise the context in mcp.yaml.`;
      toast("error", "Nothing fits the context", m.error, 8000);
      busy = null;
      endTurn();
      updateAssistant(el, m, false);
      saveChat();
      return;
    }
  }
  if (!kept.offerTools && settings.mcp !== false && mcpInfo.tools > 0) {
    toast("warn", "Tools off for this turn",
      `The ${mcpInfo.tools} tool definitions do not fit in the ${ctxfmt(settings.ctx)} context next to this conversation. The chat runs without them.`, 8000);
  }
  if (kept.summary) m.compaction = kept.summary;
  if (kept.dropped || kept.clipped) toast("info", "Context compacted",
    `cap ${ctxfmt(kept.cap)} tokens: the ${kept.dropped} earliest turn(s) were summarized (${fmt(kept.droppedChars)} characters)` +
    (kept.clipped ? `; ${fmt(kept.clipped)} characters of the newest turn were kept as its beginning and its end.` : " The profile and the newest turns are sent in full."), 6000);
  const chatBody = (kept) => {
    const body = {model: health.model, messages: kept.messages, stream: true,
                  reasoning_effort: settings.thinking};
    if (settings.temperature > 0) {
      Object.assign(body, {temperature: +settings.temperature, top_p: +settings.top_p, top_k: +settings.top_k});
    } else {
      body.temperature = 0;
    }
    if (settings.seed) body.seed = +settings.seed;
    if (settings.max) body.max_tokens = +settings.max;
    if (projectionLoaded()) body.experimental_speed_projection = !!settings.esp;
    if (kept.offerTools) body.hypercore_mcp = true;   // this server may run MCP tools for it
    return body;
  };

  let firstAt = null, thinkStart = null, usage = null, frame = 0;
  let sent = kept;                                   // the payload that actually went out
  const paint = () => { frame = 0; updateAssistant(el, m, true); scrollDown(); };
  try {
    const post = (kept) => fetch("v1/chat/completions", {method: "POST", headers: headers(true),
                                                        body: JSON.stringify(chatBody(kept)),
                                                        signal: controller.signal});
    let r = await post(kept);
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { msg = (await r.json()).error.message || msg; } catch (e) { /* not json */ }
      // The page's own count is a character estimate. When the engine says the prompt is over the
      // window it has measured it with the real tokenizer, so re-cut the payload with that number
      // and try once. A document that is denser than prose is caught here rather than lost.
      const measured = r.status === 400 ? overflowRatio(msg, kept.messages) : null;
      if (measured) {
        anchor = {tokens: measured.tokens, chars: measured.chars,
                  ratio: Math.min(1, Math.max(0.2, (measured.tokens - templatePrefix(kept.offerTools)) / measured.chars))};
        const again = buildRequest(payload, measured.ctx || settings.ctx);
        if (again.fits) {
          kept = again;
          sent = kept;
          if (again.summary) m.compaction = again.summary;
          r = await post(kept);
        }
      }
      if (!r.ok) {
        if (r.status === 401) msg = "This server needs an API key: add it under About > Settings.";
        // a prompt that fills the context is refused, never cut (the engine does not truncate). Say what
        // happened and what to do about it, rather than leaving the raw 400 in the chat.
        if (r.status === 400 && /leaves no room/i.test(msg)) {
          msg = `${msg} - this conversation has used up the context. Start a new chat, or keep the rounds
                 smaller in mcp.yaml (max_result_chars, condense_at, condense_chars).`;
        }
        throw new Error(msg);
      }
    }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) {            // ": keep-alive" comments while a prompt is read or a tool runs
          if (beat) paintBeat();                    // the heartbeat ticks: the tool is still working
          continue;}
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let j;
        try { j = JSON.parse(data); } catch (e) { continue; }
        if (j.error) throw new Error(j.error.message || "the engine reported an error");
        if (j.usage) usage = j.usage;
        if (j.hypercore_mcp) onTool(m, j.hypercore_mcp);
        const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
        const lastTool = m.tools && m.tools.length ? m.tools[m.tools.length - 1] : null;   // a new round after a tool
        if ((d.content || d.reasoning_content) && beat && beat.compact) { beat = null; paintBeat(); }
        if (d.reasoning_content) {
          if (!firstAt) firstAt = performance.now();
          if (!thinkStart) thinkStart = performance.now();
          if (lastTool && m.reasoning && lastTool.rat === m.reasoning.length) m.reasoning += "\n\n";
          m.reasoning += d.reasoning_content;
        }
        if (d.content) {
          if (!firstAt) firstAt = performance.now();
          if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
          if (lastTool && m.text && lastTool.at === m.text.length) m.text += "\n\n";
          m.text += d.content;
        }
        if (!frame) frame = requestAnimationFrame(paint);
      }
    }
  } catch (e) {
    if (e.name === "AbortError") m.stopped = true;
    else { m.error = e.message || String(e); toast("error", "The request failed", m.error, 6000); }
  }
  if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
  const n = usage ? usage.completion_tokens : null;
  if (usage && usage.prompt_tokens) {
    // After a mid-turn compaction the last prompt is the handoff, not the payload this page sent,
    // so its character count is unknown: keep the ratio already measured and back the size out of
    // it, so the next estimate does not treat the dropped turns as still in the window.
    if (compactedDuringTurn) {
      const ratio = anchor.ratio || 1 / CHARS_PER_TOKEN;
      anchor = {tokens: usage.prompt_tokens, chars: Math.round(usage.prompt_tokens / ratio), ratio,
               offerTools: anchor.offerTools};
    } else {
      const chars = payloadChars(sent.messages);
      // The engine counted this content; the template's own block is not part of it. Clamped so a
      // one-line turn cannot make the ratio absurd.
      const ratio = chars > 16 ? Math.min(1, Math.max(0.2,
        (usage.prompt_tokens - templatePrefix(sent.offerTools)) / chars)) : 1 / CHARS_PER_TOKEN;
      anchor = {tokens: usage.prompt_tokens, chars, ratio, offerTools: sent.offerTools};
    }
  }
  if (n && firstAt) {
    const secs = (performance.now() - firstAt) / 1000;
    m.meta = `${fmt(n)} tokens${secs > 0.25 ? ` · ${fmt(n / secs, 1)} tok/s` : ""}${m.stopped ? " · stopped" : ""}` +
             (projectionLoaded() ? (settings.esp ? " · projection on" : " · projection off") : "");
  } else if (m.stopped) {
    m.meta = "Stopped";
  }
  for (const t of m.tools || []) if (t.state === "writing" || t.state === "running") { t.state = "skipped"; t.ms = null; }
  const ran = (m.tools || []).filter((t) => t.state === "done" || t.state === "error").length;
  if (ran) m.meta = `${m.meta ? `${m.meta} · ` : ""}${ran} tool call${ran > 1 ? "s" : ""}`;
  if (m.limit) {
    const byCalls = m.limitCalls && m.limitCallsRun >= m.limitCalls;
    m.meta = `${m.meta || ""} · stopped at the ${byCalls
      ? `${m.limitCalls} tool call limit (mcp.max_tool_calls), ${m.limitRounds} rounds used`
      : `${m.limit} tool round limit (mcp.max_rounds), ${m.limitCallsRun} calls run`}`;
  }
  busy = null;
  endTurn();
  if (frame) cancelAnimationFrame(frame);
  updateAssistant(el, m, false);
  saveChat();
  scrollDown();
}

$("composer").onsubmit = (e) => { e.preventDefault(); send(); };
$("stop-btn").onclick = () => {
  if ($("stop-btn").dataset.mode === "stop") { if (busy) busy.controller.abort(); return; }
  if (busy) return;
  $("input").value = RESUME_PROMPT;   // the Start button injects the prompt and sends it
  send();
};
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
function autosize() { const t = $("input"); t.style.height = "auto"; t.style.height = `${Math.min(t.scrollHeight, innerHeight * 0.4)}px`; }
$("input").addEventListener("input", autosize);

$("new-btn").onclick = () => {
  if (busy) { toast("warn", "Still writing", "Stop the answer first."); return; }
  turnEnded = false;
  paintStopButton("start");           // a new chat has no turn to resume
  loadPrompts(true);                                    // a new chat is a good moment to pick up a new profile
  if (!messages.length && !compactState) return;
  const backup = messages, compactBackup = compactState;
  messages = [];
  compactState = null;
  saveChat();
  saveCompact();
  renderChat();
  toast("info", "New chat", "The last one was cleared.", 6000, {label: "Undo", run: () => { messages = backup; compactState = compactBackup; saveChat(); saveCompact(); renderChat(); }});
};
// The transcript as Markdown. The download button saves it to the browser's own
// downloads folder; the archive button posts the same text to POST /history/archive,
// which writes it to data/history/_old_chats/ (serve/chat_archive.py).
function chatMarkdown() {
  const tools = (m) => (m.tools || []).filter((t) => t.result != null).map((t) =>
    `<details><summary>Tool ${t.server ? `${t.server} / ` : ""}${t.tool || t.name}${t.ok ? "" : " (error)"}</summary>\n\n` +
    `\`\`\`json\n${JSON.stringify(t.arguments || {}, null, 2)}\n\`\`\`\n\n\`\`\`\n${t.result}\n\`\`\`\n\n</details>\n\n`).join("");
  return messages.map((m) => m.role === "user" ? `## You\n\n${m.text}\n` :
    `## ${health.model}\n\n${m.reasoning ? `<details><summary>Thinking</summary>\n\n${m.reasoning}\n\n</details>\n\n` : ""}${tools(m)}${m.text || m.error || ""}\n`).join("\n");
}
function chatStamp() { return new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-"); }
function chatTitle() {
  const first = messages.find((m) => m.role === "user");
  return ((first && first.text) || "").split("\n")[0].slice(0, 60);
}

// The browser's own downloads folder. Used by the download button, and by the
// archive button when the server behind the page has no history route.
function saveChatMarkdown() {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([chatMarkdown()], {type: "text/markdown"}));
  a.download = `hypercore-chat-${chatStamp()}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

$("export-btn").onclick = () => {
  if (!messages.length) { toast("info", "Nothing to save yet"); return; }
  saveChatMarkdown();
};

$("archive-btn").onclick = async () => {
  if (!messages.length) { toast("info", "Nothing to save yet"); return; }
  const btn = $("archive-btn");
  btn.disabled = true;
  try {
    const res = await fetch("/history/archive", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({chat: chatMarkdown(), title: chatTitle()}),
    });
    const out = await res.json().catch(() => ({}));
    if (res.status === 401) {
      throw new Error("this server needs its API key: put the key the server was started with " +
                      "into About > Settings, then try again");
    }
    if (res.status === 404) {
      // The page came from this server, but the process behind it was started before
      // the history route existed: the HTML is read fresh, the Python code is not.
      toast("warn", "This server has no history route yet",
           "Restart the WebUI to load /history/archive. The chat is saved as a file instead.",
           8000, {label: "Save as Markdown", run: saveChatMarkdown});
      return;
    }
    if (!res.ok) throw new Error((out.error && out.error.message) || `the server said ${res.status}`);
    // The transcript is on disk. The distillation into chats/, topics/ and notes/
    // runs behind this response, so say what it did rather than imply it is finished.
    let detail = out.path || out.name;
    if (out.distil && out.distil.queued) detail += " - the topic files are being rebuilt from it";
    else if (out.distil && out.distil.reason) detail += " - " + out.distil.reason;
    toast("success", "Chat archived", detail, 6000);
  } catch (e) {
    toast("error", "Could not archive the chat", e.message || String(e), 6000);
  } finally {
    btn.disabled = false;
  }
};

// pictures and text files: the attach button, dropping them on the chat, or pasting a picture (issue #30)
const TEXT_EXT = /\.(txt|md|markdown|rst|tex|py|pyi|ipynb|js|mjs|cjs|ts|tsx|jsx|vue|svelte|json|jsonl|csv|tsv|log|ya?ml|toml|ini|cfg|conf|env|xml|html?|css|scss|less|c|cc|cpp|cxx|h|hh|hpp|cu|cuh|rs|go|java|kt|kts|swift|rb|php|pl|lua|r|jl|scala|sql|sh|bash|zsh|fish|ps1|psm1|bat|cmd|diff|patch|gradle|cmake|mk|dockerfile|gitignore|proto|graphql)$/i;
const MAX_TEXT_FILE = 2 * 1024 * 1024;   // a guard against reading a whole drive into the page; the context trimmer is what decides how much of it reaches the model
function isTextFile(f) {
  return f.type.startsWith("text/") || /json|xml|javascript|yaml|toml|x-sh|x-python/.test(f.type) ||
         TEXT_EXT.test(f.name) || /(^|[\\/])(makefile|dockerfile|readme|license)$/i.test(f.name);
}
function addFiles(files) {
  for (const f of files) {
    if (f.type.startsWith("image/")) {
      if (!health.images) { toast("warn", "Pictures are off", "This model was set up for text only."); continue; }
      if (f.size > 20e6) { toast("warn", "Picture too large", `${f.name} is over 20 MB.`); continue; }
      const r = new FileReader();
      r.onload = () => { attachments.push({kind: "image", name: f.name || "pasted image", url: r.result}); renderAttachments(); };
      r.readAsDataURL(f);
      continue;
    }
    if (!isTextFile(f)) { toast("warn", "Not a text file", `${f.name}: attach text files (code, notes, logs, data)${health.images ? " or pictures" : ""}.`); continue; }
    if (f.size > MAX_TEXT_FILE) { toast("warn", "File too large", `${f.name} is over 2 MB.`); continue; }
    const r = new FileReader();
    r.onload = () => {
      const text = String(r.result);
      if (text.includes("\u0000")) { toast("warn", "Not a text file", `${f.name} looks like a binary file.`); return; }
      attachments.push({kind: "file", name: f.name, text});
      renderAttachments();
    };
    r.readAsText(f);
  }
}
// a file's text in the message, fenced with more backticks than it contains itself
function fileBlock(f) {
  // The fence must be longer than the longest run of backticks the file itself contains.
  // Spreading the matches into Math.max exhausts the stack on a document with a hundred
  // thousand of them, which is what happens when a big log or a JSON dump is attached.
  let longest = 2;
  for (const run of f.text.match(/`+/g) || []) if (run.length > longest) longest = run.length;
  const fence = "`".repeat(longest + 1);
  return `File: ${f.name}\n${fence}\n${f.text}\n${fence}`;
}
function userText(m) {
  const files = (m.files || []).filter((f) => f.text != null);
  return [m.text, ...files.map(fileBlock)].filter((s) => s).join("\n\n");
}
function renderAttachments() {
  const box = $("attachments");
  box.hidden = !attachments.length;
  box.innerHTML = "";
  attachments.forEach((a, i) => {
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = icon(a.kind === "file" ? "attach" : "image", "st-icon st-icon--sm");
    c.append(a.name);
    const x = document.createElement("button");
    x.type = "button"; x.className = "st-btn st-btn--icon"; x.setAttribute("aria-label", "Remove");
    x.innerHTML = icon("trash");
    x.onclick = () => { attachments.splice(i, 1); renderAttachments(); };
    c.appendChild(x);
    box.appendChild(c);
  });
}
$("attach-btn").onclick = () => $("file").click();
// drop files on the chat or the message box
for (const id of ["chat", "composer"]) {
  const el = $(id);
  el.addEventListener("dragover", (e) => {
    if (![...(e.dataTransfer || {}).types || []].includes("Files")) return;
    e.preventDefault();
    $("composer").classList.add("dragging");
  });
  el.addEventListener("dragleave", () => $("composer").classList.remove("dragging"));
  el.addEventListener("drop", (e) => {
    $("composer").classList.remove("dragging");
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    addFiles(e.dataTransfer.files);
    $("input").focus();
  });
}
$("file").onchange = () => { addFiles($("file").files); $("file").value = ""; };
$("input").addEventListener("paste", (e) => {
  if (!health.images) return;
  const files = [...(e.clipboardData || {}).files || []].filter((f) => f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); addFiles(files); }
});

// ------------------------------------------------------------------ the sampling drawer
function openDrawer(open) {
  $("drawer").dataset.open = String(open);
  $("drawer").setAttribute("aria-hidden", String(!open));
  $("scrim").hidden = !open;
  if (open) { loadDrawer(); loadShared(); loadMcp(); }
}
function loadDrawer(s = settings) {
  for (const b of $("s-thinking").children) b.setAttribute("aria-checked", String(b.dataset.v === s.thinking));
  $("s-temp").value = s.temperature; $("s-topp").value = s.top_p; $("s-topk").value = s.top_k;
  $("s-max").value = s.max; $("s-seed").value = s.seed; $("s-ctx").value = s.ctx;
  $("s-show").setAttribute("aria-checked", String(!!s.show));
  $("s-esp").setAttribute("aria-checked", String(s.esp !== false));
  $("esp-row").hidden = !projectionLoaded();
  $("s-mcp").setAttribute("aria-checked", String(s.mcp !== false));
  $("s-share").setAttribute("aria-checked", String(sharedOn));
  outputs();
}
// "Use for other apps too": the server keeps these settings as every client's defaults (GET/POST /settings)
let sharedOn = false;
async function loadShared() {
  try {
    const r = await fetch("settings", {headers: headers()});
    if (r.ok) sharedOn = !!(await r.json()).shared;
  } catch (e) { /* an older server: the switch just stays off */ }
  $("s-share").setAttribute("aria-checked", String(sharedOn));
}
function sharedDefaults(s) {
  const d = {reasoning_effort: s.thinking, temperature: +s.temperature};
  if (+s.temperature > 0) Object.assign(d, {top_p: +s.top_p, top_k: +s.top_k});
  if (s.seed) d.seed = +s.seed;
  if (s.max) d.max_tokens = +s.max;
  if (projectionLoaded()) d.experimental_speed_projection = s.esp !== false;
  return d;
}
async function saveShared(on, s) {
  const r = await fetch("settings", {method: "POST", headers: headers(true),
                                      body: JSON.stringify({defaults: on ? sharedDefaults(s) : null})});
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error.message || msg; } catch (e) { /* not json */ }
    throw new Error(msg);
  }
  sharedOn = !!(await r.json()).shared;
}
// the engine was started with the experimental-speed-projection control vector (INFO cvec=...)
function projectionLoaded() {
  const c = lastMetrics && lastMetrics.engine ? lastMetrics.engine.cvec : 0;
  return !!c && c !== "0";
}
function outputs() {
  const t = +$("s-temp").value;
  $("o-temp").textContent = t === 0 ? "0 · greedy" : t.toFixed(2);
  $("o-topp").textContent = (+$("s-topp").value).toFixed(2);
  $("o-topk").textContent = $("s-topk").value;
  const cap = +$("s-ctx").value;
  $("o-ctx").textContent = cap > 0 ? `${ctxfmt(cap)} · earliest turns dropped past that` : "no cap"
  const sel = [...$("s-thinking").children].find((b) => b.getAttribute("aria-checked") === "true");
  $("o-thinking").textContent = sel ? {none: "answers right away", low: "short", medium: "medium", high: "thorough (default)"}[sel.dataset.v] : "";
  for (const id of ["s-topp", "s-topk"]) $(id).disabled = t === 0;
}
for (const b of $("s-thinking").children) b.onclick = () => { for (const x of $("s-thinking").children) x.setAttribute("aria-checked", String(x === b)); outputs(); };
for (const id of ["s-temp", "s-topp", "s-topk"]) $(id).oninput = outputs;
$("s-show").onclick = () => $("s-show").setAttribute("aria-checked", String($("s-show").getAttribute("aria-checked") !== "true"));
$("s-esp").onclick = () => $("s-esp").setAttribute("aria-checked", String($("s-esp").getAttribute("aria-checked") !== "true"));
$("s-mcp").onclick = () => $("s-mcp").setAttribute("aria-checked", String($("s-mcp").getAttribute("aria-checked") !== "true"));
$("s-share").onclick = () => $("s-share").setAttribute("aria-checked", String($("s-share").getAttribute("aria-checked") !== "true"));
$("s-reset").onclick = () => loadDrawer(DEFAULTS);
$("s-apply").onclick = async () => {
  const sel = [...$("s-thinking").children].find((b) => b.getAttribute("aria-checked") === "true");
  settings = {thinking: sel ? sel.dataset.v : "high", temperature: +$("s-temp").value, top_p: +$("s-topp").value,
              top_k: +$("s-topk").value, max: $("s-max").value.trim(), seed: $("s-seed").value.trim(),
              ctx: +$("s-ctx").value || 0,
              show: $("s-show").getAttribute("aria-checked") === "true",
              esp: $("s-esp").getAttribute("aria-checked") === "true",
              mcp: $("s-mcp").getAttribute("aria-checked") === "true"};
  store.set("sampling", settings);
  const share = $("s-share").getAttribute("aria-checked") === "true";
  openDrawer(false);
  if (share || sharedOn) {
    try {
      await saveShared(share, settings);
      toast("success", "Sampling saved", share ? "Other apps (omp, API clients) use these settings from their next request."
                                               : "Other apps use their own settings again.");
    } catch (e) {
      toast("error", "Saved here, but not for other apps", e.message, 6000);
    }
    return;
  }
  toast("success", "Sampling saved", settings.temperature === 0 ? "Greedy: the same question gives the same answer." : "");
};
$("sampling-btn").onclick = () => openDrawer(true);
$("drawer-close").onclick = () => openDrawer(false);
$("scrim").onclick = () => openDrawer(false);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("drawer").dataset.open === "true") openDrawer(false); });

// ---------------------------------------------------------------- Assist
let assistMessages = store.get("assist", []);
let assistModels = [];

// The dropdown is only the fast models. This panel is the rest of the table: what the last probe
// measured, what it left out, and why. Without it a missing model looks like a missing endpoint.
function renderAssistProbe(data) {
  const box = $("assist-probe-results"), body = $("assist-probe-body");
  if (!box || !body) return;
  const over = data.over_limit || [];
  if (!data.pinned) { box.hidden = true; return; }
  // #617: a model the endpoint answers 404 for is not on the endpoint at all. It is neither slow nor
  // throttled, and printing it next to the ones worth retrying reads as "try it again in a minute".
  // It is counted here and left out of the table: the count still adds up, no name is shown.
  const notServed = (m) => m.reason === "not available" || /404/.test(m.error || "");
  const listed = (m) => !notServed(m);
  // A model can be in the dropdown without having been measured: the endpoint throttled the probe.
  // Without that distinction a count changes and nothing else does, which is what looked like a bug.
  const held = (data.models || []).filter((m) => m.ok === false && listed(m));
  box.hidden = false;
  box.querySelector("summary").textContent =
    `Probe results: ${data.models.length} pinned (${held.length} kept without a fresh measurement), ` +
    `${over.length} left out (limit ${data.filter.max_lag_s ?? 1.0} s, ${data.filter.rounds ?? 2} rounds, ` +
    `updated ${timeStr(data.filter.updated)})`;
  const why = (m) => {
    if (m.reason === "not available") return `the endpoint does not serve it (${m.error || "404"})`;
    if (m.reason === "rate limited") return `throttled while probing (${m.error || "429"}) - the endpoint, not the model`;
    if (m.reason === "timed out") return "the probe timed out";
    if (m.ttft_s != null) return `${fmt(m.ttft_s, 2)} s to the first byte (worst ${fmt(m.worst_s, 2)} s)`;
    return m.error || "no response";
  };
  const slow = over.filter((m) => m.ttft_s != null && listed(m));
  const throttled = over.filter((m) => m.ttft_s == null && listed(m));
  const group = (title, list, note) => list.length ? `<div class="assist-probe__row"><strong>${esc(title)}</strong>` +
    `<span class="muted small">${esc(note)}</span></div>` + list.map((m) =>
      `<div class="assist-probe__row"><span class="st-badge st-badge--queued">not pinned</span>` +
      `<strong>${esc(m.id)}</strong><span class="muted small">${esc(why(m))}</span></div>`).join("") : "";
  // #612 follow-up: the count moved from 12 to 10 and nothing said which models left. They are the
  // ones that were pinned before and are not pinned now, each with the reason the probe got.
  const removed = ((assistProbe && assistProbe.removed) || []).filter(listed);
  // The 404s are counted once, by id, so the "left out" number above can be read against this line.
  const gone = new Set([...over, ...((assistProbe && assistProbe.removed) || [])].filter(notServed).map((m) => m.id));
  body.innerHTML = (removed.length ? `<div class="assist-probe__row"><strong>Removed from the list by this probe</strong>` +
      `<span class="muted small">the dropdown went from ${assistProbe.previous} to ${assistProbe.pinned}</span></div>` +
      removed.map((m) => `<div class="assist-probe__row"><span class="st-badge st-badge--error">dropped</span>` +
        `<strong>${esc(m.id)}</strong><span class="muted small">${esc(why(m))}</span></div>`).join("") : "") +
    (held.length ? `<div class="assist-probe__row"><strong>Still pinned, not measured</strong>` +
      `<span class="muted small">the endpoint refused the probe; they stay in the dropdown because they worked before</span></div>` +
      held.map((m) => `<div class="assist-probe__row"><span class="st-badge st-badge--queued">unverified</span>` +
        `<strong>${esc(m.id)}</strong><span class="muted small">${esc(why(m))}</span></div>`).join("") : "") +
    group("Slower than the limit", slow, "measured, above it") +
    group("Throttled or unreachable while probing", throttled, "no measurement, so no judgement - try again in a minute") +
    (gone.size ? `<div class="assist-probe__row"><span class="muted small">${gone.size} model(s) the endpoint does not serve are not listed: ` +
      `the chat call answers 404 for them, so there is nothing to measure.</span></div>` : "") ||
    `<p class="muted small">Every model the endpoint served answered under the limit. If the pinned file was ` +
      `written before this card existed, press "Check lag" to record the full table.</p>`;
}
let assistShowAll = false;
let assistLoading = false;
let assistProbe = null;   // the last "Check lag" run: how many were pinned before, and which left

// The model picker folds like the thought box: the summary is the button, the body is the select, the
// endpoint status and the buttons. Folding it must not hide which model is answering, so the summary
// carries the selection; whether the panel is open is the user's choice and is remembered here.
const assistTop = $("assist-top");

function paintAssistTop() {
  const pick = $("assist-top-pick");
  if (!pick) return;
  const model = $("assist-model") ? $("assist-model").value : "";
  pick.textContent = model
    ? `${model} · ${assistModels.length} model${assistModels.length === 1 ? "" : "s"} on the endpoint`
    : "no endpoint yet";
}

function foldAssistTop(open) {
  if (!assistTop) return;                      // an older page has no picker to fold
  assistTop._auto = true;                     // the listener below ignores our own open/close
  assistTop.open = open;
  store.set("assistTopOpen", open);
}

function wireAssistTop() {
  if (!assistTop) return;
  if (!store.get("assistTopOpen", true)) foldAssistTop(false);
  assistTop.addEventListener("toggle", () => {
    if (assistTop._auto) { assistTop._auto = false; return; }
    store.set("assistTopOpen", assistTop.open);
  });
  const select = $("assist-model");
  if (select) select.addEventListener("change", paintAssistTop);
}
wireAssistTop();

// #612: the messages live inside #assist-chat, which also holds the empty-state placeholder. Wiping
// the container took the placeholder with them, so the second call found $("assist-empty") null and
// sendAssist() threw before the request was ever made - the tab looked dead. Remove the messages only,
// exactly as renderChat() does, and never touch a node that is not there.
function renderAssist() {
  const chat = $("assist-chat");
  const empty = $("assist-empty");
  chat.querySelectorAll(".st-msg").forEach((el) => el.remove());
  if (empty) empty.hidden = assistMessages.length > 0;
  for (let i = 0; i < assistMessages.length; i++) {
    const m = assistMessages[i];
    const el = msgEl(m, i);
    if (m.role === "assistant") updateAssistant(el, m, false);
    chat.append(el);
  }
  const scroll = $("assist-scroll");
  scroll.scrollTop = scroll.scrollHeight;
}

async function loadAssistModels(force = false, showAll = assistShowAll) {
  const status = $("assist-status");
  const select = $("assist-model");
  if (assistModels.length && !force) return;
  status.textContent = "Loading cached NVIDIA endpoint…";
  try {
    const r = await fetch("assist/models" + (showAll ? "?all=1" : ""), { headers: headers() });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error?.message || `HTTP ${r.status}`);
    assistModels = data.models || [];
    renderAssistProbe(data);
    select.innerHTML = "";
    for (const m of assistModels) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.lag_s ? `${m.id} (${m.lag_s.toFixed(2)}s)`
                                : (m.error ? `${m.id} (not measured: ${m.error})` : m.id);
      select.append(opt);
    }
    const preferred = [
      "openai/gpt-oss-20b",
      "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
      "google/gemma-4-31b-it",
      "deepseek-ai/deepseek-v4.1-flash",
    ];
    if (!select.value) select.value = preferred.find((id) => assistModels.some((m) => m.id === id)) || assistModels[0]?.id || "openai/gpt-oss-20b";
    assistShowAll = showAll;
    $("assist-all").textContent = showAll ? "Show only the fast ones" : "Show every model";
    status.textContent = data.pinned
      ? `Pinned to ${assistModels.length} model(s) that answered under ${data.filter.max_lag_s}s (measured ${timeStr(data.filter.updated)}). ` +
        `Edit ${data.filter.file || "serve/assist_fast_models.json"} by hand to change the list.` +
        (data.filter.note ? ` Note: ${data.filter.note}` : "")
      : `Endpoint ready: ${assistModels.length} model(s).`;
    paintAssistTop();
  } catch (e) {
    status.textContent = `Endpoint unavailable: ${e.message}`;
    select.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "openai/gpt-oss-20b";
    opt.textContent = "openai/gpt-oss-20b (fallback)";
    select.append(opt);
    select.value = "openai/gpt-oss-20b";
    foldAssistTop(true);        // there is nothing to pick unless the panel is open
    paintAssistTop();
  }
}

async function sendAssist() {
  const input = $("assist-input");
  const send = $("assist-send");
  const text = input.value.trim();
  if (!text || assistLoading) return;

  const model = $("assist-model").value || "openai/gpt-oss-20b";
  const now = new Date().toISOString();
  const user = { role: "user", text: text, content: text, time: now };
  const assistant = { role: "assistant", model, text: "", reasoning: "", time: now, meta: `${model} · ${timeStr(now)}` };
  assistMessages.push(user, assistant);
  input.value = "";
  assistLoading = true;
  send.disabled = true;
  renderAssist();

  try {
    // The bridge's own defaults (serve/assist.py, ASSIST_DEFAULTS) - deliberately not the local model's
    // sampling settings. A hosted endpoint has different limits, and inheriting a local temperature or
    // context size would make a failure in one of them look like a failure in the other.
    const r = await fetch("assist/chat", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({
        model,
        messages: assistMessages.filter((m) => m.role === "user" || m.text),
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error?.message || `HTTP ${r.status}`);
    assistant.text = data.text || "The external assistant returned no text.";
    if (data.finish_reason === "length") {
      assistant.text += "\n\n(the answer was cut off at the token limit — raise it or ask for less)";
    }
    assistant.reasoning = data.reasoning || "";
    assistant.meta = `${data.model || model} · ${timeStr(new Date().toISOString())}`;
    store.set("assist", assistMessages);
    renderAssist();
  } catch (e) {
    assistant.text = `Could not reach the external assistant: ${e.message}`;
    renderAssist();
    foldAssistTop(true);        // the picker is where another model is chosen
    toast("error", "Assist request failed", e.message, 6000);
  } finally {
    assistLoading = false;
    send.disabled = false;
    input.focus();
  }
}

$("assist-composer").onsubmit = (e) => { e.preventDefault(); sendAssist(); };
$("assist-clear").onclick = () => {
  assistMessages = [];
  store.set("assist", []);
  renderAssist();
  toast("success", "Assist conversation cleared", "The endpoint and cached models are still available.");
};
$("assist-all").onclick = () => { assistShowAll = !assistShowAll; loadAssistModels(true, assistShowAll); };
$("assist-probe").onclick = async () => {   // time every model and re-pin the dropdown to the fast ones
  const status = $("assist-status");
  const button = $("assist-probe");
  button.disabled = true;
  status.textContent = "Timing every model on the endpoint…";
  // Timing the whole endpoint takes minutes. The POST that runs it is still open, so the page asks
  // the status route for a count while it waits; without that the button just looks broken.
  const ticker = setInterval(async () => {
    try {
      const s = await (await fetch("assist/probe/status", { headers: headers() })).json();
      if (s.running) status.textContent = `Timing every model… ${s.done} of ${s.total} probed (${fmt(s.elapsed_s, 0)}s so far)`;
    } catch (e) { /* an older server has no status route; the original message stays */ }
  }, 2000);
  try {
    const r = await fetch("assist/models/probe", { method: "POST", headers: headers(true),
      body: JSON.stringify({ max_lag: 1.0, rounds: 2 }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error?.message || `HTTP ${r.status}`);
    // The headline number is the size the dropdown ends up with, not the number that measured under
    // the limit. A pinned model whose probe was throttled stays in the list, so announcing "10" while
    // 12 are still in the dropdown looked like the count had changed for no reason.
    const pinned = data.pinned ?? data.kept ?? (data.fast ?? 0);
    assistProbe = data;   // the probe panel reads this to name the models that left the list
    const dropped = (data.removed || []).map((m) => `${m.id} (${m.reason || "probe failed"})`);
    toast("success", "Lag check finished", `${pinned} model(s) in the dropdown of ${data.probed} probed` +
      (data.previous != null ? ` (was ${data.previous})` : "") + `: ` +
      `${data.fast ?? 0} measured under ${data.max_lag_s}s` +
      (data.held ? `, ${data.held} kept from the previous list - their probe was throttled` : "") +
      (dropped.length ? `, ${dropped.length} dropped: ${dropped.slice(0, 6).join(", ")}` +
        (dropped.length > 6 ? ` and ${dropped.length - 6} more` : "") : "") +
      (data.over_limit?.length ? `, ${data.over_limit.length} left out` : ""));
    status.textContent = `Lag check finished: ${pinned} pinned` +
      (data.previous != null ? ` (was ${data.previous})` : "") +
      `, ${data.fast ?? 0} measured under ${data.max_lag_s}s. "Probe results" below lists what was left out and why.` +
      (data.note ? ` (${data.note})` : "");
    await loadAssistModels(true, false);
    foldAssistTop(true);        // the results are in the panel; folding them away hides the whole run
  } catch (e) {
    status.textContent = `Lag check failed: ${e.message}`;
    toast("error", "Lag check failed", e.message, 6000);
  } finally {
    clearInterval(ticker);
    button.disabled = false;
  }
};

// ------------------------------------------------------------------ start
setBusy(false);
renderChat();
const startQuestion = new URLSearchParams(location.search).get("q");   // /?q=... starts a chat (a shortcut)
if (startQuestion) history.replaceState(null, "", location.pathname + location.hash);
loadHealth().then(() => { loadMcp(); loadPrompts(); }).then(() => { if (startQuestion) { $("input").value = startQuestion; send(); } });
showTab(location.hash.slice(1) || "chat");
poll();

// ------------------------------------------------------------------ Tune tab
let tuneTimer = null;

function loadTune() {
  fetch("tune/params", { headers: headers() }).then(r => r.json()).then((params) => {
    if (params.models && params.models.length) $("tune-model").value = params.models[0];
    const pins = $("tune-pins");
    const opts = params.pin_options || {};
    // an empty box means "search this dimension"; a filled one pins it
    pins.innerHTML = (params.pinable || []).map((name) => {
      const o = opts[name];
      if (o && o.kind === "choice") {
        return `<label>${name}<select data-pin="${name}"><option value="">search</option>` +
          o.choices.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("") +
          `</select></label>`;
      }
      if (o) {
        return `<label>${name}<input type="number" data-pin="${name}" min="${o.low}" ` +
          `max="${o.high}" step="${o.step}" placeholder="search"></label>`;
      }
      return `<label>${name}<input type="text" data-pin="${name}" placeholder="search"></label>`;
    }).join("");
    renderPriors(params.priors || []);
    renderTuneStatus(tuneState || { state: "idle" });
  }).catch(() => { $("tune-meta").textContent = "the tuner is not available on this server"; });
  pollTune();
}

function renderPriors(priors) {
  const list = $("tune-priors");
  if (!list) return;
  if (!priors.length) {
    list.innerHTML = `<li class="muted">No run data yet: the first sweep on this machine starts cold.</li>`;
    return;
  }
  const here = priors.filter((p) => p.matches).length;
  const other = priors.length - here;
  list.innerHTML = priors.map((p) => `<li>${esc(p.model)} / ${esc(p.objective)} / ${p.context}` +
    ` - ${p.trials} measured configurations ` +
    (p.matches ? `<span class="ok">this machine</span>`
               : `<span class="muted">${esc(p.hardware || "another machine")}</span>`) +
    (p.seed ? ` <span class="muted">imported</span>` : "") + "</li>").join("");
  if (other && !$("tune-cross-hardware").checked) {
    list.innerHTML += `<li class="muted">${other} of them were measured on other hardware; ` +
      `tick "use priors from other hardware" to let the sweep use them.</li>`;
  }
}

function pollTune() {
  if (tuneTimer) clearInterval(tuneTimer);
  tuneTimer = setInterval(() => {
    fetch("tune/status", { headers: headers() }).then(r => r.json()).then(renderTuneStatus).catch(() => {});
  }, 2000);
}

let tuneState = null;

function renderTuneStatus(s) {
  tuneState = s;
  const running = s.state === "running";
  $("tune-stop").disabled = !running;
  $("tune-run").disabled = running;
  $("tune-note").hidden = !running;
  if (s.state === "idle") return;
  $("view-tune .tune-progress").hidden = false;
  const pct = s.trials_total ? Math.min(100, (s.trials_done / s.trials_total) * 100) : 0;
  $("tune-bar-fill").style.width = pct + "%";
  $("tune-meta").textContent = `${s.trials_done}/${s.trials_total || "?"} trials in ${s.elapsed_s}s`;
  const rows = (s.rows || []).map((r) => `<tr><td>${r.trial}</td><td>${esc(r.layer)}</td><td>${esc(r.vram)}</td>` +
    `<td>${esc(r.pcie)}</td><td>${esc(r.ple_io)}</td><td>${esc(r.kv)}</td><td>${esc(r.kvgrow)}</td>` +
    `<td>${esc(r.hostcore)}</td><td>${esc(r.affinity)}</td><td>${esc(r.worker)}</td><td>${esc(r.tail)}</td>` +
    `<td>${esc(r.draft_floor)}</td><td>${esc(r.status)}</td><td>${r.gen_tok_s}</td>` +
    `<td>${r.prefill_tok_s}</td><td>${r.latency_ms}</td></tr>`).join("");
  $("tune-rows").innerHTML = rows;
  $("view-tune .tune-results").hidden = !(s.rows && s.rows.length);
  if (s.best) {
    $("tune-best").innerHTML = `<strong>Best so far</strong> gen ${s.best.row.gen_tok_s} tok/s, ` +
      `prefill ${s.best.row.prefill_tok_s} tok/s, latency ${s.best.row.latency_ms} ms`;
  }
  if (s.state === "done" && s.files) {
    $("tune-export").innerHTML = Object.entries(s.files).map(([k, v]) =>
      `<a href="logs/${encodeURIComponent(v)}">${esc(k)}</a>`).join(" ");
  }
}

$("tune-run").onclick = async () => {
  const pin = {};
  document.querySelectorAll("[data-pin]").forEach((el) => { if (el.value.trim()) pin[el.dataset.pin] = el.value.trim(); });
  const body = {
    model: $("tune-model").value, objective: $("tune-objective").value,
    context: Number($("tune-context").value), trials: Number($("tune-trials").value),
    sampler: $("tune-sampler").value, mode: $("tune-mode").value,
    vram_min: Number($("tune-vram-min").value), vram_max: Number($("tune-vram-max").value),
    pcie: Number($("tune-pcie").value), pin, benchmark: $("tune-benchmark").checked,
    fresh: $("tune-fresh").checked, cross_hardware: $("tune-cross-hardware").checked,
    cross_objective: $("tune-cross-objective").checked,
  };
  const r = await fetch("tune/run", { method: "POST", headers: headers(true), body: JSON.stringify(body) });
  renderTuneStatus(await r.json());
};
$("tune-stop").onclick = () => fetch("tune/stop", { method: "POST", headers: headers(true), body: "{}" });

// ------------------------------------------------------------------ Build tab
function loadBuild() {
  fetch("build/status", { headers: headers() }).then(r => r.json()).then((s) => {
    const inst = s.installed || {};
    $("build-installed").innerHTML = `<dt>Engine</dt><dd>${esc(inst.version || "none installed")}</dd>` +
      `<dt>Source</dt><dd>${esc(inst.source || "-")}</dd>` +
      `<dt>Worker</dt><dd>${inst.worker ? "built" : "not built (engine runs in direct mode)"}</dd>`;
    const releases = (s.latest && s.latest.releases) || [];
    $("build-tag").innerHTML = releases.map((r) => `<option value="${esc(r.tag)}">${esc(r.tag)}</option>`).join("");
    $("build-update-note").textContent = s.update_available
      ? `Update available: ${inst.version} -> ${releases[0].version}`
      : "The engine is the latest release.";
    renderBuildOptions(s.options);
    renderBuildLog(s.job);
  }).catch(() => { $("build-update-note").textContent = "could not reach GitHub"; });
}

function renderBuildOptions(options) {
  if (!options) return;
  const visible = (options.cmake_options || []).filter((o) => o.visible);
  $("build-options-grid").innerHTML = visible.map((o) => {
    if (o.kind === "bool") {
      return `<label>${esc(o.name)}<select data-opt="${esc(o.name)}">
        <option value="ON"${o.default === "ON" ? " selected" : ""}>ON</option>
        <option value="OFF"${o.default === "OFF" ? " selected" : ""}>OFF</option></select>
        <span class="muted small">${esc(o.help)}</span></label>`;
    }
    return `<label>${esc(o.name)}<input type="text" data-opt="${esc(o.name)}" value="${esc(o.default)}">
      <span class="muted small">${esc(o.help)}</span></label>`;
  }).join("");
  const c = options.changes;
  if (c) {
    $("build-changes").hidden = false;
    $("build-changes").innerHTML = `<strong>Options changed between ${esc(c.from || "?")} and ${esc(c.to || "?")}</strong>` +
      `<ul>${(c.added_options || []).map((x) => `<li>added: ${esc(x)}</li>`).join("")}` +
      `${(c.removed_options || []).map((x) => `<li>removed: ${esc(x)}</li>`).join("")}` +
      `${(c.changed_defaults || []).map((x) => `<li>default changed: ${esc(x.name)} -> ${esc(x.default)}</li>`).join("")}</ul>`;
  }
}

function renderBuildLog(job) {
  if (!job || job.state === "idle") return;
  $("view-build .build-log").hidden = false;
  $("build-log").textContent = job.log.join("\n");
}

$("build-fetch").onclick = async () => {
  const r = await fetch("build/fetch-prebuilt", {
    method: "POST", headers: headers(true), body: JSON.stringify({ tag: $("build-tag").value })});
  const out = await r.json();
  $("build-update-note").textContent = out.ok ? `Installed ${out.version}. Restart to use it.` : out.error;
  loadBuild();
};
$("build-start").onclick = async () => {
  const options = {};
  document.querySelectorAll("[data-opt]").forEach((el) => { options[el.dataset.opt] = el.value; });
  const r = await fetch("build/start", {
    method: "POST", headers: headers(true), body: JSON.stringify({ tag: $("build-tag").value, options })});
  renderBuildLog(await r.json());
  const poll = setInterval(async () => {
    const s = await (await fetch("build/log", { headers: headers() })).json();
    renderBuildLog(s);
    if (s.state === "done" || s.state === "failed") { clearInterval(poll); loadBuild(); }
  }, 3000);
};


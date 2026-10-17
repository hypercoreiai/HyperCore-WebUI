// Regression harness for the thought panel in the web UI, run with:
//   node serve/web/test_assist_thought_panel.js
//
// There is no browser here, so the DOM is a stand-in that supports exactly what the handlers use:
// the message markup, querySelector/closest by class or attribute, and a toggle event. Nothing is
// copied from app.js - the functions are read out of it, so this test fails when app.js changes.
"use strict";
const fs = require("fs");

const SRC = fs.readFileSync(__dirname + "/app.js", "utf8");
const PAGE_SRC = fs.readFileSync(__dirname + "/index.html", "utf8");

function sourceOf(name) {
  const start = SRC.indexOf("function " + name + "(");
  if (start < 0) throw new Error(`function ${name} is not in app.js`);
  let depth = 0;
  for (let i = SRC.indexOf("{", start); i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") { if (--depth === 0) return SRC.slice(start, i + 1); }
  }
  throw new Error(`function ${name} is not balanced`);
}

// ---------------------------------------------------------------- minimal DOM
class El {
  constructor(tag, attrs = {}) {
    this.tag = tag; this.attrs = attrs; this.children = []; this.parent = null;
    this.dataset = {}; this.listeners = {}; this.textContent = "";
    this.open = false; this.hidden = false;
  }
  get tagName() { return this.tag.toUpperCase(); }
  get className() { return this.attrs.class || ""; }
  set className(v) { this.attrs.class = v; }
  get classList() {
    const self = this;
    const set = () => (self.attrs.class || "").split(/\s+/).filter(Boolean);
    return {
      add: (c) => { const s = set(); if (!s.includes(c)) s.push(c); self.attrs.class = s.join(" "); },
      remove: (c) => { self.attrs.class = set().filter((x) => x !== c).join(" "); },
      contains: (c) => set().includes(c),
    };
  }
  appendChild(el) { el.parent = this; this.children.push(el); return el; }
  append(el) { return this.appendChild(el); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, target) { for (const fn of this.listeners[type] || []) fn({ target, type }); }
  matches(sel) {                                   // "details.think", ".think", "#assist-chat", "[data-msg-copy]"
    const m = /^(\w*)((?:[.#][\w-]+)*)((?:\[[^\]]+\])*)$/.exec(sel);
    if (!m) return false;
    if (m[1] && this.tag !== m[1]) return false;
    for (const part of m[2].match(/[.#][\w-]+/g) || []) {
      if (part[0] === "." && !(this.attrs.class || "").split(/\s+/).includes(part.slice(1))) return false;
      if (part[0] === "#" && this.attrs.id !== part.slice(1)) return false;
    }
    for (const a of m[3].match(/\[[^\]]+\]/g) || []) if (!(a.slice(1, -1) in this.attrs)) return false;
    return true;
  }
  querySelector(sel) {
    let nodes = [this];
    for (const part of sel.split(/\s*>\s*/)) {
      const found = [];
      const walk = (n) => { for (const c of n.children) { if (c.matches(part)) found.push(c); walk(c); } };
      for (const n of nodes) walk(n);
      if (!found.length) return null;
      nodes = found;
    }
    return nodes[0];
  }
  closest(sel) { let n = this; while (n) { if (n.matches(sel)) return n; n = n.parent; } return null; }
  get innerHTML() { return this.raw || ""; }   // the assertions read the markup back as text
  set innerHTML(html) {
    this.raw = html;
    this.children = [];
    const stack = [this];
    for (const t of html.match(/<\/?(\w+)((?:[^>'"]|"[^"]*"|'[^']*')*\/?>)/g) || []) {
      if (t.startsWith("</")) { if (stack.length > 1) stack.pop(); continue; }
      const m = /^<(\w+)((?:[^>'"]|"[^"]*"|'[^']*')*)/.exec(t);
      const attrs = {};
      for (const a of m[2].match(/([\w-]+)=("[^"]*"|'[^']*')|[\w-]+/g) || []) {
        const eq = a.indexOf("=");
        if (eq < 0) attrs[a] = "";                 // a bare attribute, like data-msg-copy
        else attrs[a.slice(0, eq)] = a.slice(eq + 1).slice(1, -1);
      }
      const el = new El(m[1], attrs);
      stack[stack.length - 1].appendChild(el);
      if (!t.endsWith("/>")) stack.push(el);
    }
  }
  get firstChild() { return this.children[0] || null; }
}

const ids = {};
const $ = (id) => ids[id] || null;

// ---------------------------------------------------------------- the scope app.js gives these functions
const copied = [];
const SCOPE_BODY = [
  "let messages = [];",
  "let assistMessages = [];",
  sourceOf("answerHtml"),
  sourceOf("updateAssistant"),
  sourceOf("transcriptOf"),
  sourceOf("bindTranscript"),
  "return { updateAssistant, transcriptOf, bindTranscript,",
  "         setChat: (m) => { messages = m; }, setAssist: (m) => { assistMessages = m; } };",
].join("\n");
const scope = new Function("$", "esc", "icon", "fmt", "markdown", "toolHtml", "copyText", "settings", "busy", SCOPE_BODY)(
  $, (s) => String(s), () => "", (x, n) => Number(x).toFixed(n), (s) => String(s), () => "",
  (text) => copied.push(text), { show: false }, null);

// ---------------------------------------------------------------- the scenario
const MARKUP = `<details class="st-collapse think" hidden><summary><span class="think-title"></span></summary>` +
  `<div class="st-collapse__body thinking"></div></details><div class="st-bubble"></div>` +
  `<div class="st-msg__meta"><span class="meta-text"></span><button data-msg-copy></button></div>`;

function assistantEl(i) {
  const el = new El("div");
  el.className = "st-msg st-msg--assistant";
  el.dataset.i = i;
  el.innerHTML = MARKUP;
  return el;
}

function assert(cond, msg) { if (!cond) throw new Error(msg); }
function run(name, fn) {
  try { fn(); console.log(`ok    ${name}`); }
  catch (e) {
    console.log(`FAIL  ${name}: ${e.message}`);
    if (process.env.DEBUG) console.log(e.stack.split("\n").slice(1, 4).join("\n"));
    process.exitCode = 1;
  }
}

// app.js binds each transcript container at load; run those lines against the stand-in DOM so a
// container left unbound fails here exactly as it does in the browser.
const chat = new El("div", { id: "chat" });
const assist = new El("div", { id: "assist-chat" });
ids["chat"] = chat; ids["assist-chat"] = assist;
const WIRING = SRC.match(/^bindTranscript\(\$\("[^"]+"\)\);/gm) || [];
new Function("$", "bindTranscript", WIRING.join("\n"))($, scope.bindTranscript);

function thoughtPanelIsReachable(containerId, array, container) {
  scope.setChat([]); scope.setAssist([]);
  if (containerId === "chat") scope.setChat(array); else scope.setAssist(array);

  const el = assistantEl(0);
  container.appendChild(el);
  if (process.env.DEBUG) {
    const orig = el.querySelector;
    el.querySelector = (sel) => { const r = orig.call(el, sel); if (!r) console.log(`      NULL for ${sel}`); return r; };
  }
  scope.updateAssistant(el, { role: "assistant", text: "the answer", reasoning: "the thinking", meta: "m · 12:00" }, false);

  const det = el.querySelector(".think");
  assert(det && !det.hidden, "the thought panel stayed hidden although the model returned reasoning");
  assert(el.querySelector(".think-title").textContent === "Thoughts", "the panel title is wrong");

  const body = el.querySelector(".thinking");
  assert(body.dataset.pending === "1", "the body was not queued to be filled when the panel opens");
  assert(body.textContent === "", "the body was filled while the panel was closed");

  det.open = true;                                  // the user clicks the summary
  container.dispatch("toggle", det);
  assert(body.textContent === "the thinking", `opening the panel left it empty in #${containerId}`);
  assert(!body.dataset.pending, "the pending flag was still set after the panel filled");

  const copy = el.querySelector("[data-msg-copy]");
  copied.length = 0;
  container.dispatch("click", copy);
  assert(copied[0] === "the answer", `the copy button read the wrong transcript in #${containerId}`);
}

run("both transcripts are bound", () => {
  assert(WIRING.includes('bindTranscript($("chat"));'), "#chat is not bound");
  assert(WIRING.includes('bindTranscript($("assist-chat"));'), "#assist-chat is not bound");
});

run("chat tab: opening the thought panel shows the reasoning",
  () => thoughtPanelIsReachable("chat", [{ role: "assistant", text: "the answer", reasoning: "the thinking" }], chat));

run("assist tab: opening the thought panel shows the reasoning",
  () => thoughtPanelIsReachable("assist-chat", [{ role: "assistant", text: "the answer", reasoning: "the thinking" }], assist));

run("transcriptOf picks the array the element is in", () => {
  const msg = assistantEl(1); assist.appendChild(msg);
  assert(scope.transcriptOf(msg) && scope.transcriptOf(msg).length === 1, "an assist message resolved to the chat array");
  const inChat = assistantEl(2); chat.appendChild(inChat);
  assert(scope.transcriptOf(inChat) && scope.transcriptOf(inChat).length === 0, "a chat message resolved to the assist array");
});

run("a missing container does not throw", () => { scope.bindTranscript(null); });

// ---------------------------------------------------------------- the model picker fold
// The whole fold block is taken from app.js, including the `const assistTop = $("assist-top")` line,
// so a renamed id or a lost listener fails here rather than only in the browser.
const FOLD_SRC = SRC.slice(SRC.indexOf('const assistTop = $("assist-top");'),
                           SRC.indexOf("wireAssistTop();") + "wireAssistTop();".length);

const saved = {};
const storeStub = { get: (k, d) => (k in saved ? saved[k] : d), set: (k, v) => { saved[k] = v; } };
const models = [{ id: "openai/gpt-oss-20b" }, { id: "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning" }];
const buildFold = () => new Function("$", "store", "assistModels", FOLD_SRC + "\nreturn { paintAssistTop, foldAssistTop, wireAssistTop };")($, storeStub, models);

function pickerDom(open = true) {
  const top = new El("details", { id: "assist-top" }); top.open = open;
  const select = new El("select", { id: "assist-model" }); select.value = models[0].id;
  const pick = new El("span", { id: "assist-top-pick" });
  top.appendChild(pick); top.appendChild(select);
  ids["assist-top"] = top; ids["assist-model"] = select; ids["assist-top-pick"] = pick;
  return { top, pick };
}

run("the fold block is present in app.js and the picker is a details in the page", () => {
  assert(FOLD_SRC.includes("function paintAssistTop"), "paintAssistTop is missing");
  assert(FOLD_SRC.includes("function foldAssistTop"), "foldAssistTop is missing");
  assert(FOLD_SRC.includes("function wireAssistTop"), "wireAssistTop is missing");
  assert(PAGE_SRC.includes('<details class="st-collapse assist-top" id="assist-top"'), "the picker is not a details");
  assert(PAGE_SRC.includes('<span class="muted small assist-top__pick" id="assist-top-pick"'), "the summary has no selection line");
});

run("the summary names the selected model while the panel is closed", () => {
  const { pick } = pickerDom(false);
  const fold = buildFold();
  fold.paintAssistTop();
  assert(pick.textContent.includes(models[0].id), "the summary does not name the selected model");
  assert(pick.textContent.includes("2 models"), "the summary does not say how many are available");
});

run("closing the picker is remembered, and reopening restores it", () => {
  const { top } = pickerDom(true);
  buildFold();                     // nothing stored yet: the panel stays open
  assert(top.open, "a first visit closed a panel the user never touched");
  top.open = false;                // the user clicks the summary
  top.dispatch("toggle", top);
  assert(saved.assistTopOpen === false, "the closed state was not stored");

  const again = pickerDom(true);
  buildFold();                     // a new page load reads the stored state and folds it back
  assert(!again.top.open, "the remembered fold did not come back closed");
  again.top.dispatch("toggle", again.top);            // the event our own open()/close() produced
  assert(saved.assistTopOpen === false, "our own fold overwrote the stored state");
  again.top.open = true;
  again.top.dispatch("toggle", again.top);            // now the user's click
  assert(saved.assistTopOpen === true, "the user opening the panel was not remembered");
});

run("a programmatic open does not overwrite the user's choice on the way out", () => {
  const { top } = pickerDom(true);
  const fold = buildFold();
  fold.foldAssistTop(false);
  assert(!top.open && saved.assistTopOpen === false, "foldAssistTop did not fold and store");
  top.open = true;                   // a stray toggle event from our own open()
  top.dispatch("toggle", top);
  assert(saved.assistTopOpen === false, "our own open() overwrote the stored fold");
});

run("an older page without the picker does not throw", () => {
  ids["assist-top"] = null; ids["assist-model"] = null; ids["assist-top-pick"] = null;
  buildFold();
});

// ---------------------------------------------------------------- the probe results table
const probeScope = (assistProbe) => new Function("$", "esc", "fmt", "timeStr", "assistProbe",
  sourceOf("renderAssistProbe") + "\nreturn renderAssistProbe;")(
  $, (s) => String(s), (x, n) => Number(x).toFixed(n), () => "12:00", assistProbe);

function probePanel() {
  const box = new El("details", { id: "assist-probe-results" }); box.hidden = true;
  const summary = new El("summary"); box.appendChild(summary);
  const body = new El("div", { id: "assist-probe-body" }); box.appendChild(body);
  ids["assist-probe-results"] = box; ids["assist-probe-body"] = body;
  return { box, body };
}

run("a model the endpoint answers 404 for is not listed in the probe results", () => {
  const { box, body } = probePanel();
  probeScope(null)({
    pinned: 3,
    models: [{ id: "openai/gpt-oss-20b", ok: true, ttft_s: 0.4 }],
    over_limit: [
      { id: "nvidia/gone", ok: false, error: "HTTP 404", reason: "not available" },
      { id: "nvidia/slow", ok: true, ttft_s: 4.2, worst_s: 5.1, reason: "over the limit" },
    ],
    filter: { max_lag_s: 1.0, rounds: 2, updated: "2026-10-09T15:00:00Z" },
  });
  assert(!box.hidden, "the panel stayed hidden although models were left out");
  assert(!body.innerHTML.includes("nvidia/gone"), "the 404 model was named in the table");
  assert(body.innerHTML.includes("nvidia/slow"), "the slow model was dropped from the table");
  assert(body.innerHTML.includes("1 model(s) the endpoint does not serve"), "the count of 404s is not reported");
  assert(!body.innerHTML.includes("Not served by this endpoint"), "the 404 group is still printed");
});

run("a 404 removed by the probe is counted once, not listed", () => {
  const { body } = probePanel();
  const gone = { id: "nvidia/gone", ok: false, error: "HTTP 404", reason: "not available" };
  const removed = [gone, { id: "nvidia/slow", ok: true, ttft_s: 4.2, worst_s: 5.1, reason: "over the limit" }];
  const fn = probeScope({ previous: 5, pinned: 3, removed });
  fn({ pinned: 3, models: [{ id: "openai/gpt-oss-20b", ok: true, ttft_s: 0.4 }], over_limit: [gone],
      filter: { max_lag_s: 1.0, rounds: 2, updated: "2026-10-09T15:00:00Z" } });
  assert(!body.innerHTML.includes("nvidia/gone"), "the removed 404 was still named");
  assert(body.innerHTML.includes("1 model(s) the endpoint does not serve"), "the 404 count is missing");
  assert(body.innerHTML.includes("nvidia/slow"), "a genuinely dropped model is missing from the list");
});

run("nothing is hidden when there is nothing to hide", () => {
  const { box, body } = probePanel();
  probeScope(null)({ pinned: 0, models: [], over_limit: [], filter: { max_lag_s: 1.0, rounds: 2, updated: "x" } });
  assert(box.hidden, "a probe with no pinned models opened the panel");
});

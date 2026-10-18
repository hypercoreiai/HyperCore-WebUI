// Regression harness for the "the engine is not running" banner: the markup is in
// index.html, the CSS is in app.css, and app.js must show it from /health's engine
// state and hide it when an engine is back. Run with:
//   node tests/test_engine_offline_banner.js
//
// There is no browser: the banner code is lifted out of app.js and run against a stub
// DOM, so the test fails if the markup, the copy or the state machine changes.
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const WEB = path.join(ROOT, "serve", "web");
const APP = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const CSS = fs.readFileSync(path.join(WEB, "app.css"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");
const SPRITE = fs.readFileSync(path.join(WEB, "sprite.svg"), "utf8");

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

function slice(src, from, to, what) {
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`${what} is no longer in the file: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(to, i);
  if (j < 0) throw new Error(`${what} is not closed`);
  return src.slice(i, j + to.length);
}

// ---- 1. the banner exists, and is hidden by default --------------------------
check(HTML.includes('id="engine-offline"'), "index.html has no engine banner");
check(/id="engine-offline"[^>]*hidden/.test(HTML), "the engine banner is not hidden by default");
check(HTML.includes('id="engine-load-btn"'), "the banner has no Load button");
check(HTML.includes('id="engine-offline-why"'), "the banner has no place for the reason");
check(HTML.indexOf('id="engine-offline"') < HTML.indexOf('id="chat-scroll"'),
      "the banner must sit above the chat, not inside it");
const icon = (HTML.match(/id="engine-offline"[\s\S]{0,300}?<use href="web\/sprite\.svg#(i-[a-z0-9-]+)"/) || [])[1];
check(icon && SPRITE.includes(`id="${icon}"`), `the banner's icon ${icon || "(none)"} is not in sprite.svg`);
check(CSS.includes(".engine-offline"), "app.css has no style for the banner");

// ---- 2. the server tells the UI the engine state -----------------------------
check(SERVER.includes('"engine": engine_state'), "GET /health does not report the engine state");
check(SERVER.includes("EngineUnavailable"), "the server has no engine_unavailable error path");
check(SERVER.includes('"--ui-only"'), "the server has no --ui-only mode");
check(SERVER.includes("busy=card_is_busy"),
      "the retry thread is never told when a sweep or a build has the GPU");
check(SERVER.includes("a tune sweep is using the GPU"), "the pause reason does not name the sweep");
check(SERVER.includes("a build is using the GPU"), "the pause reason does not name a build");
check(SERVER.includes("HYPERCORE_HOLD_THE_CARD"),
      "a sweep run from the CLI cannot tell the retry to hold off");

// ---- 3. run the renderer against a stub DOM -----------------------------------
const renderFn = slice(APP, "function renderEngineState() {", "\n}", "renderEngineState");
// The banner renderer and the header button are one state machine: the banner paints the
// header's Stop/Start label too, so both are run together here.
const engineBlock = slice(APP, "const RESUME_PROMPT", "$(\"close-server-btn\")",
                          "the banner's engine block");
const makeEl = () => ({hidden: false, disabled: false, dataset: {}, title: "", textContent: "",
                       attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
                       getAttribute(k) { return this.attrs[k]; }});
const els = {
  "engine-offline": Object.assign(makeEl(), {hidden: true}),
  "engine-offline-title": makeEl(),
  "engine-offline-why": makeEl(),
  "engine-load-btn": Object.assign(makeEl(), {disabled: false}),
  "chat-empty-sub": makeEl(),
  "shutdown-btn": Object.assign(makeEl(), {disabled: false}),
  "shutdown-text": makeEl(),
};
const $ = (id) => els[id];
const run = new Function("$", "health", "setPill", "toast", "fetch", "headers", "document",
                         engineBlock + "\n" + renderFn + "\nreturn renderEngineState;");
const render = (health) => run($, health, () => {}, () => {}, async () => ({ok: true, json: async () => ({})}), () => ({}),
                               {querySelectorAll: () => []})();

render({model: "fake-model", engine: {state: "running"}});
check(els["engine-offline"].hidden === true, "the banner shows when an engine is running");

render({model: "fake-model", engine: {state: "offline", reason: "the GPU is held by a sweep",
                                     detail: "INFO engine 1.45\nERR the GPU is held by a sweep",
                                     retry: {state: "retrying", next_attempt_s: 12.4, attempts: 3}}});
check(els["engine-offline"].hidden === false, "the banner is hidden when the engine is offline");
check(els["engine-offline-why"].textContent.includes("sweep"), "the reason is not shown");
check(els["engine-offline-why"].textContent.includes("12"), "the next retry time is not shown");
check((els["engine-offline-why"].title || "").includes("INFO engine 1.45"),
      "the full engine error is not available to read");
check(els["engine-load-btn"].textContent === "Try now", "the button should say Try now while a retry is running");
check(els["chat-empty-sub"].textContent.includes("not loaded"), "the empty chat still promises a local model");

render({model: "fake-model", engine: {state: "offline", reason: "no engine was asked for"}});
check(els["engine-offline-why"].textContent.includes("no engine was asked for"),
      "a missing reason is dropped instead of shown");
check(els["engine-load-btn"].textContent === "Load it", "with no retry scheduled the button should say Load it");

render({model: "fake-model", engine: {state: "offline", reason: "the GPU is held by a sweep",
                                     retry: {state: "paused", paused: true,
                                           paused_reason: "a tune sweep is using the GPU; the engine is retried when it finishes",
                                           attempts: 0, next_attempt_s: 30}}});
check(els["engine-offline-why"].textContent.includes("tune sweep"),
      "a paused retry is not explained");
check(!els["engine-offline-why"].textContent.includes("Retrying in"),
      "a paused retry is still announced as retrying, which is a lie");
check(els["engine-load-btn"].textContent === "Try anyway",
      "the Load button is not honest while the retry is paused");

render({model: "fake-model"});
check(els["engine-offline"].hidden === true, "a health response with no engine state shows the banner");

// ---- 4. the header button follows the same state ----------------------------
render({model: "fake-model", engine: {state: "running"}});
check("the header offers Stop Engine while an engine is running",
      els["shutdown-text"].textContent === "Stop Engine",
      `got ${els["shutdown-text"].textContent}`);
check("the header says the app keeps serving",
      /This app keeps serving/.test(els["shutdown-btn"].title));
render({model: "fake-model", engine: {state: "offline", stopped: true,
                                      reason: "the engine was stopped from this app"}});
check("a stop from the app turns the header into Start Engine",
      els["shutdown-text"].textContent === "Start Engine",
      `got ${els["shutdown-text"].textContent}`);
check("the header calls it a stop, not a crash",
      /stopped from this app/.test(els["engine-offline-why"].textContent));
check("the banner's own button starts a stopped engine",
      els["engine-load-btn"].textContent === "Start the engine",
      `got ${els["engine-load-btn"].textContent}`);

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("ok: the engine banner is wired to /health");

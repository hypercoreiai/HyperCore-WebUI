// Regression harness for the two buttons that change their own name: the composer's
// Stop/Start pair and the banner's engine Stop/Start button. Run with:
//   node tests/test_stop_start_buttons.js
//
// There is no browser here. The blocks are lifted out of app.js and run as-is against a
// stub DOM, so the test fails when app.js changes a label, an icon, the prompt text or
// the state machine behind either button. It also checks index.html carries the ids the
// painting code writes to, that the banner button stops the engine and not the app, and
// that closing the whole server is a separate control on the Monitor tab.
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

function slice(src, from, to, what) {            // includes the closing marker
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`${what} is no longer in the file: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(to, i);
  if (j < 0) throw new Error(`${what} is not closed`);
  return src.slice(i, j + to.length);
}

function sliceBefore(src, from, marker, what) { // stops just before the next block
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`${what} is no longer in the file: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(marker, i);
  if (j < 0) throw new Error(`${what} does not end at ${JSON.stringify(marker)}`);
  return src.slice(i, j);
}

const engineBlock = sliceBefore(APP, "const RESUME_PROMPT", "$(\"close-server-btn\")",
                                "the banner's engine block");
const closeBlock = slice(APP, "$(\"close-server-btn\").onclick = async () => {", "};",
                         "the Monitor's Close this server handler");
const composerBlock = slice(APP, "let turnEnded = false;", "  setBusy(false);\n}",
                            "the composer's Stop/Start block");
const stopHandler = slice(APP, "$(\"stop-btn\").onclick = () => {", "};",
                          "the composer button's click handler");

// ---- stub DOM ---------------------------------------------------------------
const els = {};
function makeEl(id) {
  return {
    id, hidden: false, disabled: false, dataset: {}, title: "", textContent: "", attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
  };
}
for (const id of ["stop-btn", "stop-icon", "stop-text", "shutdown-btn", "shutdown-text",
                  "close-server-btn", "send-btn", "input", "composer-hint"]) els[id] = makeEl(id);

const $ = (id) => els[id];
let beat = null;
const beatText = () => "";
const toasts = [];
const toast = (kind, title, body) => toasts.push([kind, title, body]);
const setPill = () => {};
const headers = () => ({ "Content-Type": "application/json" });
const fakeDocument = { querySelectorAll: () => [] };
// The real page re-reads /health after a stop or a start; the stub keeps the state the
// test set up, so the label after a request is the state the handler produced.
const loadHealth = async () => {};

let sent = null;
const send = () => { sent = els["input"].value; };

let canned = [];                       // one canned response per fetch, in order
const fetchStub = async () => {
  const body = canned.length ? canned.shift() : { ok: true, json: {} };
  return { ok: body.ok, status: body.status || 200, json: async () => body.json };
};

// What /health says about the engine: the banner paints itself from this.
const health = {model: "test-model", engine: {state: "running"}};

// Run the blocks once, in the same scope app.js would. `busy` is the test's stand-in for
// the running turn: Stop must abort it, Start must not.
const api = new Function("$", "beat", "beatText", "setPill", "toast", "headers", "loadHealth",
                         "document", "fetch", "send", "health",
                         "let busy = null, aborted = false;\n" + engineBlock + "\n" + closeBlock + "\n" +
                         composerBlock + "\n" + stopHandler +
                         "\nreturn {setBusy, endTurn, disarmEngineButton, paintEngineButton," +
                         " closeServer: () => $('close-server-btn').onclick()," +
                         " clickStop: () => $('stop-btn').onclick()," +
                         " startTurn: () => { busy = {controller: {abort: () => { aborted = true;}}}; setBusy(true); }," +
                         " endTurn2: () => { busy = null; endTurn(); }, wasAborted: () => aborted};")
                        ($, beat, beatText, setPill, toast, headers, loadHealth,
                         fakeDocument, fetchStub, send, health);

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`  ok    ${name}`); return; }
  failures++;
  console.log(`  FAIL  ${name}${detail ? `: ${detail}` : ""}`);
}

async function main() {

// ---- what the page is built with ------------------------------------------
check("the resume prompt is 'Complete the open tasks'",
      /const RESUME_PROMPT = "Complete the open tasks";/.test(APP));
for (const id of ["stop-btn", "stop-icon", "stop-text", "shutdown-btn", "shutdown-text",
                  "close-server-btn"])
  check(`index.html has id="${id}"`, HTML.includes(`id="${id}"`));
check("the Start state has a CSS rule", /\.st-btn\[data-mode="start"\]/.test(
      fs.readFileSync(path.join(WEB, "components.css"), "utf8")));
check("the banner button's label is Stop Engine",
      /id="shutdown-text">Stop Engine<\/span>/.test(HTML), "the header still says Shut Down");
check("the banner button says the app keeps serving",
      /id="shutdown-btn"[\s\S]{0,200}This app keeps serving/.test(HTML),
      "its title does not say the app stays up");
check("the banner button is not labelled as closing the app",
      !/id="shutdown-btn"[\s\S]{0,200}close this server/i.test(HTML),
      "the header button still promises to close the server");
check("the sprite has the power icon the header uses", SPRITE.includes('id="i-power"'));
check("closing the whole server is a control on the Monitor tab",
      /id="close-server-btn"/.test(HTML) && /Close this server/.test(HTML));
check("the Monitor control is styled", CSS.includes(".state-card__controls"));
check("the server reports a stop from the app in /health",
      SERVER.includes('engine_state["stopped"] = True') &&
      SERVER.includes('getattr(svc, "engine_stopped", False)'));

// ---- the banner button talks to the engine, not to the server -------------
check("the banner button stops the engine", /fetch\("v1\/stop"/.test(engineBlock));
check("the banner button can start it again", /fetch\("v1\/start"/.test(engineBlock));
check("the banner button does not close the server", !/fetch\("v1\/shutdown"/.test(engineBlock),
      "the header button still calls /v1/shutdown");
check("the Monitor control closes the server", /fetch\("v1\/shutdown"/.test(closeBlock));
check("the offline banner starts a stopped engine rather than loading into nothing",
      APP.includes('(health.engine || {}).stopped ? "v1/start" : "v1/load"'));
check("polling stops only when the server is closed",
      APP.includes("if (!serverClosed) setTimeout(poll, 1000)"));

// ---- the composer's Stop button -------------------------------------------
api.setBusy(false);                    // app.js does this at startup
check("before the first turn the button is hidden", els["stop-btn"].hidden === true,
      `hidden=${els["stop-btn"].hidden}`);

api.startTurn();                     // a turn starts
check("during a turn the button says Stop", els["stop-text"].textContent === "Stop",
      `got ${JSON.stringify(els["stop-text"].textContent)}`);
check("during a turn the button is visible", els["stop-btn"].hidden === false);
check("during a turn the icon is i-stop",
      els["stop-icon"].getAttribute("href") === "web/sprite.svg#i-stop",
      `got ${els["stop-icon"].getAttribute("href")}`);
check("during a turn the send button is disabled", els["send-btn"].disabled === true);
check("during a turn the button is data-mode=stop", els["stop-btn"].dataset.mode === "stop",
      `got ${els["stop-btn"].dataset.mode}`);

sent = null;
api.clickStop();                     // Stop: abort the turn, inject nothing
check("Stop aborts the running turn", api.wasAborted() === true);
check("Stop does not inject a prompt", sent === null, `got ${JSON.stringify(sent)}`);

api.endTurn2();                      // the turn has ended
check("after the turn the button says Start", els["stop-text"].textContent === "Start",
      `got ${JSON.stringify(els["stop-text"].textContent)}`);
check("after the turn the icon is i-bolt",
      els["stop-icon"].getAttribute("href") === "web/sprite.svg#i-bolt",
      `got ${els["stop-icon"].getAttribute("href")}`);
check("after the turn the button is visible", els["stop-btn"].hidden === false);
check("after the turn the send button works again", els["send-btn"].disabled === false);
check("after the turn the button is data-mode=start", els["stop-btn"].dataset.mode === "start",
      `got ${els["stop-btn"].dataset.mode}`);

els["input"].value = "something the user was typing";
sent = null;
api.clickStop();                     // Start: inject the prompt and send it
check("Start injects the prompt into the composer", sent === "Complete the open tasks",
      `sent ${JSON.stringify(sent)}`);
check("Start replaces whatever was in the box", els["input"].value === "Complete the open tasks",
      `got ${JSON.stringify(els["input"].value)}`);

// ---- the banner's engine button -------------------------------------------
api.paintEngineButton();
check("with an engine running the button says Stop Engine",
      els["shutdown-text"].textContent === "Stop Engine",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("the label says the app keeps serving",
      /This app keeps serving/.test(els["shutdown-btn"].title),
      `got ${JSON.stringify(els["shutdown-btn"].title)}`);

els["shutdown-btn"].onclick();       // first click: arm
check("the first click arms it", els["shutdown-text"].textContent === "Confirm",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("arming makes no request", canned.length === 0, "the first click made a request");
check("arming does not disable the button", els["shutdown-btn"].disabled === false);
check("arming times out on its own",
      /setTimeout\(\(\) => \{ if \(!engineBusy\) disarmEngineButton\(\); \}, 8000\)/.test(APP));

api.disarmEngineButton();
check("disarming puts the label back", els["shutdown-text"].textContent === "Stop Engine",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);

els["shutdown-btn"].onclick();       // arm again
canned = [{ok: true, json: {status: "stopped", stopped: ["the model engine", "the MCP servers"]}}];
await els["shutdown-btn"].onclick(); // second click: stop the engine
check("the second click stops the engine", toasts.some((t) => t[1] === "Engine stopped"));
check("the toast names what was stopped", toasts.some((t) => t[2].includes("the model engine")));
check("the toast says the app is still serving",
      toasts.some((t) => t[1] === "Engine stopped" && t[2].includes("still serving")));
check("a stopped engine is not a closed app: the button is still usable",
      els["shutdown-btn"].disabled === false, `disabled=${els["shutdown-btn"].disabled}`);
check("after a stop the button says Start Engine",
      els["shutdown-text"].textContent === "Start Engine",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);

// A stop that fails must not leave the button in the stopped state: bring the engine back
// first, then try a stop that the server refuses.
canned = [{ok: true, json: {status: "running", model: "test-model"}}];
await els["shutdown-btn"].onclick();   // start it again
check("the start request reached the server", canned.length === 0);
check("a successful start is reported", toasts.some((t) => t[1] === "Engine starting"));
check("after a start the button says Stop Engine again",
      els["shutdown-text"].textContent === "Stop Engine",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);

health.engine = {state: "running"};
canned = [{ok: false, status: 500, json: {error: {message: "the engine would not stop"}}}];
api.paintEngineButton();
els["shutdown-btn"].onclick();       // arm
await els["shutdown-btn"].onclick(); // stop, and fail
check("a failed stop is reported",
      toasts.some((t) => t[0] === "error" && t[1] === "Could not stop the engine"));
check("a failed stop leaves the label at Stop Engine",
      els["shutdown-text"].textContent === "Stop Engine",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("a failed stop leaves the button enabled", els["shutdown-btn"].disabled === false);

// An engine that never came up: the button offers to start it.
health.engine = {state: "offline", reason: "a tune sweep has the GPU", stopped: false};
api.paintEngineButton();
check("an engine that is not running offers Start Engine",
      els["shutdown-text"].textContent === "Start Engine",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("the offer says the app kept serving",
      /kept serving while it was down/.test(els["shutdown-btn"].title));

// ---- the Monitor's Close this server --------------------------------------
canned = [{ok: true, json: {status: "shutting_down", closing: ["the model engine", "the HTTP server"]}}];
await api.closeServer();
check("closing the server is reported", toasts.some((t) => t[1] === "Shutting down"));
check("the close toast names what is being closed",
      toasts.some((t) => t[2].includes("the HTTP server")));
check("after a close the banner says Server closed",
      els["shutdown-text"].textContent === "Server closed",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("after a close the banner button is disabled", els["shutdown-btn"].disabled === true);

canned = [{ok: false, status: 500, json: {error: {message: "the server could not close"}}}];
els["close-server-btn"].disabled = false;
await api.closeServer();
check("a failed close is reported",
      toasts.some((t) => t[0] === "error" && t[1] === "Could not close the server"));
check("a failed close re-enables the control", els["close-server-btn"].disabled === false);

console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
process.exit(failures ? 1 : 0);
}

main();

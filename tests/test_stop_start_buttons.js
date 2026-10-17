// Regression harness for the two buttons that change their own name: the composer's
// Stop/Start pair and the banner's two-click Shut Down. Run with:
//   node serve/web/test_stop_start_buttons.js
//
// There is no browser here. The two blocks are lifted out of app.js and run as-is against
// a stub DOM, so the test fails when app.js changes a label, an icon, the prompt text or
// the state machine behind either button. It also checks index.html carries the ids the
// painting code writes to.
"use strict";

const fs = require("fs");
const path = require("path");

const APP = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

function slice(src, from, to, what) {
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`app.js no longer has ${what}: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(to, i);
  if (j < 0) throw new Error(`app.js no longer closes ${what}`);
  return src.slice(i, j + to.length);
}

const engineBlock = slice(APP, "const RESUME_PROMPT", "};", "the banner's Shut Down block");
const composerBlock = slice(APP, "let turnEnded = false;", "  setBusy(false);\n}",
                            "the composer's Stop/Start block");
const stopHandler = slice(APP, '$("stop-btn").onclick = () => {', "};",
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
for (const id of ["stop-btn", "stop-icon", "stop-text", "shutdown-btn",
                  "shutdown-text", "send-btn", "input", "composer-hint"]) els[id] = makeEl(id);

const $ = (id) => els[id];
let beat = null;
const beatText = () => "";
const toasts = [];
const toast = (kind, title, body) => toasts.push([kind, title, body]);
const setPill = () => {};
const headers = () => ({ "Content-Type": "application/json" });
const fakeDocument = { querySelectorAll: () => [] };

let sent = null;
const send = () => { sent = els["input"].value; };

let canned = [];                       // one canned response per fetch, in order
const fetchStub = async () => {
  const body = canned.length ? canned.shift() : { ok: true, json: {} };
  return { ok: body.ok, status: body.status || 200, json: async () => body.json };
};

// Run the blocks once, in the same scope app.js would. `busy` is the test's stand-in for
// the running turn: Stop must abort it, Start must not.
const api = new Function("$", "beat", "beatText", "setPill", "toast", "headers",
                         "document", "fetch", "send",
                         "let busy = null, aborted = false;\n" + engineBlock + "\n" + composerBlock + "\n" + stopHandler +
                         "\nreturn {setBusy, endTurn, disarmShutdown, clickStop: () => $('stop-btn').onclick(),"
                         + " startTurn: () => { busy = {controller: {abort: () => { aborted = true;}}}; setBusy(true); },"
                         + " endTurn2: () => { busy = null; endTurn(); }, wasAborted: () => aborted};")
                        ($, beat, beatText, setPill, toast, headers, fakeDocument, fetchStub, send);

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
for (const id of ["stop-btn", "stop-icon", "stop-text", "shutdown-btn", "shutdown-text"])
  check(`index.html has id="${id}"`, HTML.includes(`id="${id}"`));
check("the Start state has a CSS rule", /\.st-btn\[data-mode="start"\]/.test(
      fs.readFileSync(path.join(__dirname, "components.css"), "utf8")));

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

// ---- the banner's Shut Down button -----------------------------------------
api.disarmShutdown();
check("before a click the button says Shut Down", els["shutdown-text"].textContent === "Shut Down",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("the banner button still says it closes the server",
      /then close this server/.test(els["shutdown-btn"].title));
check("the banner does not call the engine Stop/Start pair",
      !/fetch\("v1\/(stop|start)"/.test(APP));

els["shutdown-btn"].onclick();        // first click: arm
check("the first click arms it", els["shutdown-text"].textContent === "Confirm",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("arming does not shut anything down", els["shutdown-btn"].disabled === false);
check("arming times out on its own",
      /setTimeout\(\(\) => \{ if \(!shuttingDown\) disarmShutdown\(\); \}, 8000\)/.test(APP));

canned = [{ ok: false, status: 500, json: { error: { message: "the server could not close" } } }];
await els["shutdown-btn"].onclick();  // second click: the request fails
check("a failed shutdown re-enables the button",
      els["shutdown-btn"].disabled === false,
      `disabled=${els["shutdown-btn"].disabled}`);
check("a failed shutdown puts the label back", els["shutdown-text"].textContent === "Shut Down",
      `got ${JSON.stringify(els["shutdown-text"].textContent)}`);
check("a failed shutdown is reported", toasts.some((t) => t[0] === "error" && t[1] === "Could not shut down"));

els["shutdown-btn"].onclick();        // arm again
canned = [{ ok: true, json: { status: "shutting_down", closing: ["the model engine", "the MCP servers"] } }];
await els["shutdown-btn"].onclick();  // second click: shut down
check("the second click asks for a shutdown", toasts.some((t) => t[1] === "Shutting down"));
check("the shutdown toast names what is being closed",
      toasts.some((t) => t[2].includes("the model engine")));
check("a shutdown disables the button", els["shutdown-btn"].disabled === true);

console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
process.exit(failures ? 1 : 0);
}

main();

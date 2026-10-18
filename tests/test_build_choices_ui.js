// Regression harness for the Build tab's "Setup choices" card: the model, quant,
// context, KV cache, image and experimental flags of a release must be listed
// before anything is built, and the command line under them must match the boxes.
// No browser: the rendering functions are lifted out of app.js and run against a
// stub DOM.
//
//   node tests/test_build_choices_ui.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const APP = fs.readFileSync(path.join(ROOT, "serve", "web", "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");
const API = fs.readFileSync(path.join(ROOT, "serve", "build_api.py"), "utf8");
const CSS = fs.readFileSync(path.join(ROOT, "serve", "web", "app.css"), "utf8");
const HOOK = fs.readFileSync(path.join(ROOT, "serve", "strata_hook.py"), "utf8");

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

function slice(src, from, to, what) {
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`app.js no longer has ${what}: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(to, i);
  if (j < 0) throw new Error(`app.js no longer closes ${what}`);
  return src.slice(i, j + to.length);
}

// ---- 1. the card and its parts are in the page ---------------------------------
check(HTML.includes('id="build-choices"'), "the Build tab has no setup-choices card");
check(HTML.includes('id="build-choices-groups"'), "the card has no group container");
check(HTML.includes('id="build-choices-command"'), "the card does not show the command the boxes mean");
check(HTML.includes('id="build-choices-rest"'), "the card does not say what it left out");
check(HTML.includes('id="build-choices-tag"'), "the card does not name the release it is describing");
check(HTML.includes("Run setup with these"), "the card has no way to apply the choices");
check(CSS.includes(".build-choices-group"), "the groups have no styling");
check(CSS.includes(".build-command"), "the command line has no styling");

// ---- 2. the server can answer for a release without cloning it -----------------
check(API.includes("def options_for(tag: str) -> dict[str, Any]:"), "serve/build_api.py has no options_for");
check(API.includes("raw.githubusercontent.com"), "the release files are not fetched from GitHub");
check(API.includes("def setup_choices(options: dict[str, Any]) -> dict[str, Any]:"), "no grouping helper");
check(SERVER.includes('path == "/build/options"'), "GET /build/options is not routed");
check(SERVER.includes("build_api.options_for(tag)"), "the route does not read the release's own files");
check(SERVER.includes("build_api.setup_choices(options)"), "the route does not return the groups");

// ---- 3. run the rendering against a stub DOM -----------------------------------
const renderSrc = (() => {
  const i = APP.indexOf("const CHOICE_ORDER =");
  const j = APP.indexOf('$("build-tag").addEventListener');
  if (i < 0 || j < 0) throw new Error("app.js no longer has the choice renderer");
  return APP.slice(i, j);
})();

function stubDom() {
  return {
    "build-choices": { hidden: true },
    "build-choices-tag": { textContent: "" },
    "build-choices-groups": { innerHTML: "" },
    "build-choices-rest": { textContent: "" },
    "build-choices-command": { textContent: "" },
    "build-choices-note": { textContent: "" },
  };
}

function parseControls(html) {
  // A stand-in for the browser: every control the card rendered, with the value a
  // user would have left in it (the preselected option, or the value attribute).
  const controls = [];
  for (const [, flag, body] of html.matchAll(/<select data-flag="([^"]+)">([\s\S]*?)<\/select>/g)) {
    const selected = body.match(/<option value="([^"]+)" selected>/);
    controls.push({ dataset: { flag }, value: selected ? selected[1] : "" });
  }
  for (const [, , flag, attrs] of html.matchAll(/<input([^>]*)data-flag="([^"]+)"([^>]*)>/g)) {
    const value = (attrs.match(/value="([^"]*)"/) || [null, ""])[1];
    controls.push({ dataset: { flag }, value });
  }
  return controls;
}

function run(setup, tag, error) {
  const dom = stubDom();
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  const doc = { querySelectorAll: () => parseControls(dom["build-choices-groups"].innerHTML) };
  const fn = new Function("$", "esc", "document", renderSrc + "\nreturn renderBuildChoices;");
  const render = fn((id) => dom[id], esc, doc);
  render(setup, tag, error);
  return dom;
}

const GROUPS = {
  model: [
    { flag: "--family", group: "model", choices: ["qwen", "swift"], default: null, help: "which model", type: null, visible: true },
    { flag: "--model", group: "model", choices: ["Q2_0", "IQ3_XXS"], default: null, help: "which quant", type: null, visible: true },
  ],
  context: [{ flag: "--context", group: "context", choices: null, default: 32768, help: "context length", type: "int", visible: true }],
  "kv-cache": [{ flag: "--kv", group: "kv-cache", choices: ["int8", "q4_0"], default: "int8", help: "KV cache precision", type: null, visible: true }],
  images: [
    { flag: "--vision", group: "images", choices: ["yes", "no", "none", "gpu", "cpu"], default: null, help: "let the model read images", type: null, visible: true },
    { flag: "--vision-tokens", group: "images", choices: null, default: null, help: "image token cap", type: "int", visible: true },
    { flag: "--images", group: "images", choices: ["qwen-image", "no"], default: null, help: "", type: null, visible: true },
    { flag: "--image-quant", group: "images", choices: ["Q4_K_M", "Q6_K", "Q8_0"], default: null, help: "", type: null, visible: true },
    { flag: "--image-backend", group: "images", choices: ["cuda", "hip", "vulkan", "cpu"], default: null, help: "", type: null, visible: true },
    { flag: "--image-native", group: "images", choices: ["on", "off"], default: null, help: "", type: null, visible: true },
    { flag: "--image-gpu-policy", group: "images", choices: ["auto", "keep", "shrink", "unload"], default: null, help: "", type: null, visible: true },
  ],
  experimental: [{ flag: "--experimental-speed-projection", group: "experimental", choices: ["on", "off", "GGUF"], default: null, help: "EXPERIMENTAL", type: null, visible: true }],
};
const SETUP = { groups: GROUPS, hidden: 2, total: 13 };

const dom = run(SETUP, "v0.1.45", "");
const html = dom["build-choices-groups"].innerHTML;
for (const title of ["Model and quant", "Context", "KV cache", "Images", "Experimental"]) {
  check(html.includes(title), `the ${title} group is not rendered`);
}
for (const flag of ["--family", "--model", "--context", "--kv", "--vision", "--vision-tokens",
                    "--images", "--image-quant", "--image-backend", "--image-native",
                    "--image-gpu-policy", "--experimental-speed-projection"]) {
  check(html.includes(`data-flag="${flag}"`), `${flag} has no control`);
}
check(html.includes('<option value="int8" selected>'), "the default choice is not preselected");
check(html.includes('<option value="no" selected>'), "the image default is not no");
check(dom["build-choices-rest"].textContent.includes("1 further flag"), "the card does not count what it left out");
check(dom["build-choices"].hidden === false, "the card stays hidden when there are choices to show");

// The command line reflects the boxes: an explicit default is sent, an unset or off
// choice is not. The default build must have no image arguments at all.
const command = dom["build-choices-command"].textContent;
check(command.startsWith("python setup.py"), "the command does not start with the setup script");
check(command.includes("--kv int8"), "the preselected default is not in the command");
check(!command.includes("--family"), "an unset box is in the command anyway");
check(command.includes("--context 32768"), "a numeric field with a value is not in the command");
check(!command.includes("--vision"), "the no-vision default is sent to setup.py");
check(!command.includes("--images"), "the no-image default is sent to setup.py");
check(!command.includes("--image-quant"), "an unset image argument is sent anyway");
check(!command.includes("--image-backend"), "an unset image backend is sent anyway");
check(!command.includes("--image-native"), "an unset image native flag is sent anyway");
check(!command.includes("--image-gpu-policy"), "an unset image GPU policy is sent anyway");
check(!command.includes("--experimental-speed-projection"), "an unset experimental flag is sent anyway");

// ---- 4. the command is the one that runs: a detached setup has no terminal ---------
check(APP.includes("const HOOK_FLAGS = [\"--yes\", \"--no-start\"]"), "app.js does not list the flags the hook adds");
check(command.includes("--yes") && command.includes("--no-start"), "the preview command omits the non-interactive flags");
check(command.trim().endsWith("--no-start"), "the hook flags are not appended after the choices");
check(HOOK.includes('NON_INTERACTIVE = ("--yes", "--no-start")'), "serve/strata_hook.py adds no non-interactive flags");
check(HOOK.includes("def build_command(setup: Path, args: list[str] | None) -> list[str]:"), "no command builder in the hook");
check(HOOK.includes("command = build_command(setup, args)"), "start() does not use the command builder");
check(HOOK.includes("stdin=subprocess.DEVNULL"), "the hook leaves stdin open, so a prompt hangs the build");
check(HOOK.includes("if flag not in given"), "a flag the caller sent would be added twice");
check(HTML.includes("runs without questions"), "the card does not say the setup answers its own questions");

// A release whose files could not be read says so instead of rendering an empty grid.
const offline = run({ groups: {}, hidden: 0, total: 0 }, "v9.9.9", "could not read Niko1221/Strata/v9.9.9: timed out");
check(offline["build-choices-groups"].innerHTML === "", "an unreadable release renders something anyway");
check(offline["build-choices-rest"].textContent.includes("unknown until"), "the unreadable case gives no next step");

// The Run setup button: it posts the boxes, says which Strata folder the hook runs
// setup.py in, and while a setup is running it is the button that stops it.
const runStart = APP.indexOf('$("setup-run").addEventListener');
if (runStart < 0) { console.error("FAIL: app.js has no setup-run handler"); failures += 1; }
const runBlock = APP.slice(runStart, APP.indexOf("function renderBuildOptions", runStart));
check(runBlock.includes('fetch("config/strata-hook", { method: "POST"'), "the button posts the setup hook");
check(runBlock.includes('if (setupState === "running")') &&
      runBlock.includes('fetch("config/strata-hook/stop", { method: "POST"'),
      "the button cannot stop a setup that is already running");
check(runBlock.includes("b.checkout"), "the note does not name the folder being set up");
check(runBlock.includes("catch (e)"), "a fetch that throws is left unhandled");
check(HTML.includes('id="setup-run"'), "the Build tab has the run button");
check(HTML.includes('id="strata-hook-stop"'), "the Stop button the Config card uses exists");

// ---- 4. the status card: the button flips, the stage line and the problems render ----
check(HTML.includes('id="build-status"'), "the Build tab has no status card for a build in progress");
check(HTML.includes('id="build-status-stage"'), "the status card has no stage line");
check(HTML.includes('id="build-status-problems"'), "the status card has no place for what a build prints");
check(CSS.includes(".build-status__problems"), "the status card's problem list has no styling");
check(HOOK.includes("STEP_RE"), "serve/strata_hook.py does not read setup.py's own step markers");
check(HOOK.includes('"stage": self.stage'), "the hook status reports no stage");

const statusSrc = (() => {
  const i = APP.indexOf("let setupOffset = 0, setupTimer = null;");
  const j = APP.indexOf('$("setup-run").addEventListener');
  if (i < 0 || j < 0) throw new Error("app.js no longer has the background-build status code");
  return APP.slice(i, j);
})();

function statusDom() {
  const button = (label) => ({
    textContent: label, dataset: {}, handlers: {},
    classList: { set: new Set(), toggle(c, on) { if (on) this.set.add(c); else this.set.delete(c); } },
    addEventListener(type, handler) { this.handlers[type] = handler; },
  });
  return {
    "build-status": { hidden: true, dataset: {} },
    "build-status-stage": { textContent: "" },
    "build-status-elapsed": { textContent: "" },
    "build-status-problems": { hidden: true, innerHTML: "" },
    "build-choices-note": { textContent: "" },
    "setup-run": button("Run setup with these"),
    "build-start": button("Clone and build from source"),
  };
}

const esc2 = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
const sdom = statusDom();
const renderStatus = new Function("$", "esc", statusSrc + "\nreturn renderBuildStatus;")((id) => sdom[id], esc2);

renderStatus({
  state: "running", elapsed_s: 42.3, exit_code: null,
  stage: "Step 5: downloading Qwen3.8-Flash-Next IQ2_XS", percent: 31,
  download: "Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00001-of-00002.gguf",
  problems: [{ kind: "warning", text: "the SSD is slow" },
             { kind: "error", text: "OSError: the model folder is read-only" }],
}, "setup");
check(sdom["setup-run"].textContent === "Kill Build", "the run button does not become Kill Build while a setup runs");
check(sdom["build-status"].hidden === false, "the status card stays hidden while a build is running");
check(sdom["build-status-stage"].textContent.includes("Step 5: downloading"), "the stage line does not show the step the setup is on");
check(sdom["build-status-stage"].textContent.includes("31% of Qwen3.8"), "the stage line ignores the download percent");
check(sdom["build-status-elapsed"].textContent === "running for 42.3s", "the status card does not say how long it has been going");
check(sdom["build-status-problems"].hidden === false, "the problems list is hidden when there are problems to show");
check(sdom["build-status-problems"].innerHTML.includes('class="warning"'), "a warning is not marked as one");
check(sdom["build-status-problems"].innerHTML.includes("OSError:"), "an error line is not shown");
check(sdom["build-status-problems"].innerHTML.includes("the SSD is slow"), "the problems list does not show the text");

// The same card for a source build: the stage is the build job's own state.
renderStatus({ state: "building", elapsed_s: 200 }, "source");
check(sdom["build-status-stage"].textContent === "compiling", "the source build stage is not named");
check(sdom["build-start"].textContent === "Kill Build", "the build-from-source button does not become Kill Build");
renderStatus({ state: "done", elapsed_s: 1200, exit_code: 0 }, "source");
check(sdom["build-start"].textContent === "Clone and build from source", "the build button does not go back when the build ends");
check(sdom["build-status-stage"].textContent === "finished in 1200s", "a finished source build is not reported as finished");

// Finished, stopped, or never started: the button goes back and the card says so.
renderStatus({ state: "done", elapsed_s: 900, exit_code: 0 }, "setup");
check(sdom["setup-run"].textContent === "Run setup with these", "the button stays Kill Build after the setup ended");
check(sdom["build-status-stage"].textContent === "finished in 900s", "a finished build is not reported as finished");
check(sdom["build-status-elapsed"].textContent === "", "the elapsed line still shows after the run ended");
renderStatus({ state: "failed", elapsed_s: 12, exit_code: 3, problems: [{ kind: "error", text: "no network" }] }, "setup");
check(sdom["build-status-stage"].textContent.includes("exit 3"), "a failed run does not report its exit code");
renderStatus({ state: "idle" }, "setup");
check(sdom["build-status"].hidden === true, "the status card is shown when nothing is running");
check(sdom["build-start"].textContent === "Clone and build from source", "the source button lost its label");

// ---- 5. the handler itself: start, flip to Kill Build, click again to stop it ----
const handlerSrc = APP.slice(APP.indexOf("let setupOffset = 0, setupTimer = null;"),
                            APP.indexOf("function renderBuildOptions"));
if (handlerSrc.length < 400) throw new Error("app.js no longer has the background-build handler");

const HOOK_RUNNING = {
  state: "running", elapsed_s: 61.5, exit_code: null, checkout: "/home/ai-user0/Strata",
  log: "logs/strata-hook.log", stage: "Step 3: Python packages", percent: null, download: "",
  problems: [{ kind: "warning", text: "pip is slower on this machine" }], lines: [], offset: 120,
};

function buildHandler() {
  const dom = statusDom();
  const posts = [];
  const fakeFetch = (url, opts = {}) => {
    posts.push(`${opts.method || "GET"} ${url}`);
    if (url === "config/strata-hook" && opts.method === "POST") {
      return Promise.resolve({ ok: true, json: () => ({ state: "running", checkout: "/home/ai-user0/Strata", log: "logs/strata-hook.log" }) });
    }
    if (url === "config/strata-hook/stop") {
      return Promise.resolve({ ok: true, json: () => ({ cancelled: true }) });
    }
    return Promise.resolve({ ok: true, json: () => HOOK_RUNNING });
  };
  const fn = new Function("$", "esc", "headers", "fetch", "toast", "activeBuildArgs", "loadBuild",
    "setInterval", "clearInterval", handlerSrc + "\nreturn { state: () => setupState };");
  return {
    dom, posts,
    api: fn((id) => dom[id], esc2, () => ({}), fakeFetch, () => {}, () => ["--model", "IQ2_XS"],
            () => {}, () => 1, () => {}),
  };
}

(async () => {
  const h = buildHandler();
  await h.dom["setup-run"].handlers.click();
  await new Promise((r) => setTimeout(r, 50));   // the un-awaited first poll must have landed
  check(h.posts[0] === "POST config/strata-hook", "the run button does not post the setup hook");
  check(h.dom["setup-run"].textContent === "Kill Build", "the button still reads Run setup with these while the setup runs");
  check(h.dom["build-status"].hidden === false, "the status card stayed hidden during a running build");
  check(h.dom["build-status-stage"].textContent === "Step 3: Python packages", "the status card does not show the step");
  check(h.dom["build-status-problems"].hidden === false, "the warning the setup printed is not shown");
  check(h.api.state() === "running", "the handler does not remember that a setup is running");
  await h.dom["setup-run"].handlers.click();
  check(h.posts.some((p) => p === "POST config/strata-hook/stop"), "clicking Kill Build does not stop the setup");
  check(h.posts.filter((p) => p === "POST config/strata-hook").length === 1,
        "clicking Kill Build started a second setup");
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("all setup-choice checks passed");
})();

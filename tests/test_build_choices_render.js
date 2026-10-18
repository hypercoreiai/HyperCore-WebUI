// End-to-end render test for the Build tab's "Setup choices" card.
//
// The card is hidden in serve/web/index.html and is opened by renderBuildChoices in
// serve/web/app.js. A throw anywhere inside loadBuild's promise chain lands in its
// catch, and that catch empties #build-choices-groups: the tab then shows the card
// title with no boxes at all. This test runs the real app.js against a stub DOM
// built from the real index.html, with the real build/status payload shape, and
// checks the boxes survive.
//
//   node tests/test_build_choices_render.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");
const APP = fs.readFileSync(path.join(ROOT, "serve", "web", "app.js"), "utf8");

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

// ---------------------------------------------------------------- the page and the script
// $ is document.getElementById. Passing it a CSS selector returns null, and the
// dereference throws inside the promise chain that renders the Build tab.
const selectorCalls = APP.match(/\$\("[^"]*[. ][^"]*"\)/g) || [];
check(selectorCalls.length === 0, "$ is called with a CSS selector: " + selectorCalls.join(", "));
for (const id of ["build-log-card", "tune-progress-card", "tune-results-card"]) {
  check(HTML.includes(`id="${id}"`), `the page has no ${id}`);
}
for (const id of ["build-choices", "build-choices-groups", "build-choices-command", "build-choices-rest", "build-choices-tag"]) {
  check(HTML.includes(`id="${id}"`), `the Build tab has no ${id}`);
}

// ---------------------------------------------------------------- stub DOM
const handlers = [];
function makeEl(id) {
  const el = {
    id, hidden: false, textContent: "", innerHTML: "", value: "", disabled: false,
    classList: { toggle: () => {}, add: () => {}, remove: () => {}, contains: () => false },
    addEventListener: (k, fn) => handlers.push([id, k, fn]),
    appendChild: () => {}, removeChild: () => {}, querySelector: () => null,
    querySelectorAll: () => [], setAttribute: () => {}, getAttribute: () => null,
    removeAttribute: () => {}, closest: () => null, focus: () => {},
    style: { setProperty: () => {}, removeProperty: () => {} },
    children: [], insertAdjacentHTML: () => {}, dataset: {},
  };
  Object.defineProperty(el, "onclick", { set: (fn) => handlers.push([id, "click", fn]), get: () => null });
  return el;
}
const els = new Map();
for (const m of HTML.matchAll(/id="([A-Za-z0-9_-]+)"/g)) els.set(m[1], makeEl(m[1]));

// A stand-in for the browser: the controls inside a rendered container, with the
// value a user would have left in each one.
function controlsIn(containerId) {
  const src = (els.get(containerId) || {}).innerHTML || "";
  const out = [];
  for (const m of src.matchAll(/<select data-flag="([^"]+)">([\s\S]*?)<\/select>/g)) {
    const opt = m[2].match(/<option value="([^"]*)" selected>/);
    out.push({ dataset: { flag: m[1] }, value: opt ? opt[1] : "" });
  }
  for (const m of src.matchAll(/<input([^>]*)data-flag="([^"]+)"([^>]*)>/g)) {
    out.push({ dataset: { flag: m[2] }, value: (m[3].match(/value="([^"]*)"/) || [null, ""])[1] });
  }
  return out;
}

const document = {
  documentElement: makeEl("html"), body: makeEl("body"), title: "",
  getElementById: (id) => els.get(id) || null,
  querySelector: () => null,
  querySelectorAll: (sel) => sel === "#build-choices-groups [data-flag]" ? controlsIn("build-choices-groups") : [],
  createElement: (t) => makeEl("anon-" + t), addEventListener: () => {},
  activeElement: null, readyState: "complete",
};

// ---------------------------------------------------------------- the payload
// The shape serve/build_api.py:build_status() returns. Kept in the test so it runs
// with no network: the groups are what setup_choices() produces from a release's
// own setup.py (config/build_options.json is the same shape).
const SETUP = {
  groups: {
    model: [
      { flag: "--family", group: "model", visible: true, choices: ["qwen", "swift"], default: null, help: "which model", type: null },
      { flag: "--model", group: "model", visible: true, choices: ["Q2_0", "IQ3_XXS"], default: null, help: "which quant", type: null },
    ],
    context: [{ flag: "--context", group: "context", visible: true, choices: null, default: 32768, help: "context length", type: "int" }],
    "kv-cache": [{ flag: "--kv", group: "kv-cache", visible: true, choices: ["int8", "q4_0"], default: "int8", help: "KV cache precision", type: null }],
    images: [
      { flag: "--images", group: "images", visible: true, choices: ["no", "yes"], default: "no", help: "pictures", type: null },
      { flag: "--image-quant", group: "images", visible: true, choices: ["q8_0", "q4_0"], default: null, help: "vision quant", type: null },
    ],
    experimental: [{ flag: "--experimental-speed-projection", group: "experimental", visible: true, choices: ["no", "yes"], default: "no", help: "speed projection", type: null }],
  },
  hidden: 32,
  total: 47,
};

// Optional second argument: a captured build/status payload, so the same checks can
// be run against what a real server actually returns:
//   node tests/test_build_choices_render.js /tmp/build_status.json
const FIXTURE = process.argv[2];
const STATUS = FIXTURE ? JSON.parse(fs.readFileSync(FIXTURE, "utf8")) : {
  installed: { version: "0.1.45", source: "local", worker: false },
  latest: { repo: "strata-net/strata", releases: [{ tag: "v0.1.45", version: "0.1.45" }], checked_at: "2026-10-18T00:00:00Z" },
  newest: { tag: "v0.1.45", version: "0.1.45" },
  update_available: false,
  options: { source: "github", version: "0.1.45", cmake_options: [{ name: "STRATA_CUDA", help: "CUDA support" }], setup_choices: [] },
  setup: SETUP,
  job: { state: "done", exit_code: 0, elapsed_s: 12, log: ["cmake -B build", "build finished"], version: "0.1.45" },
};

const shim = {
  document,
  window: { document, addEventListener: () => {}, alert: () => {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  fetch: async (url) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes("build/status") ? STATUS : {}),
    text: async () => "",
  }),
  WebSocket: function () { return { send: () => {}, close: () => {} }; },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  navigator: { userAgent: "node" },
  history: { pushState: () => {}, replaceState: () => {} },
  location: { href: "http://localhost/", search: "", hash: "#build", pathname: "/" },
  URL: { createObjectURL: () => "" },
  requestAnimationFrame: () => 0,
  performance: { now: () => 0 },
};

// Instrument loadBuild so a throw in any step is named rather than swallowed by its
// own catch. The catch is what turns a broken step into an empty card.
const instrument = (src) =>
  src
    .replace("renderBuildOptions(s.options);",
             "try { renderBuildOptions(s.options); } catch (e) { steps.push(['renderBuildOptions', e.message]); }")
    .replace("renderBuildChoices(s.setup, inst.version || \"main\", (s.options || {}).error);",
             "try { renderBuildChoices(s.setup, inst.version || \"main\", (s.options || {}).error); } catch (e) { steps.push(['renderBuildChoices', e.message]); }")
    .replace("renderBuildLog(s.job);",
             "try { renderBuildLog(s.job); } catch (e) { steps.push(['renderBuildLog', e.message]); }")
    .replace("renderBuildStatus(s.job || { state: \"idle\" }, \"source\");",
             "try { renderBuildStatus(s.job || { state: \"idle\" }, \"source\"); } catch (e) { steps.push(['renderBuildStatus', e.message]); }")
    .replace("refreshBuildStatus();",
             "try { refreshBuildStatus(); } catch (e) { steps.push(['refreshBuildStatus', e.message]); }");

// globalThis so the instrumented app.js, which runs in global scope, can see it
const steps = [];
globalThis.steps = steps;
const keys = Object.keys(shim);
let loadBuild = null;
try {
  const fn = new Function(...keys, instrument(APP) + "\nglobalThis.__loadBuild = loadBuild;");
  fn(...keys.map((k) => shim[k]));
  loadBuild = globalThis.__loadBuild;
} catch (e) {
  console.error("FAIL: app.js cannot be run in the stub DOM: " + e.message);
  process.exit(1);
}

if (typeof loadBuild !== "function") {
  console.error("FAIL: serve/web/app.js has no loadBuild");
  process.exit(1);
}

loadBuild();

setTimeout(() => {
  const setup = FIXTURE ? (STATUS.setup || {}) : SETUP;
  check(steps.length === 0, "a step inside loadBuild threw: " + JSON.stringify(steps));
  check(els.get("build-choices").hidden === false, "the setup choices card stayed hidden");
  const flags = controlsIn("build-choices-groups").map((c) => c.dataset.flag);
  check(els.get("build-choices-tag").textContent === ((STATUS.installed || {}).version || "main"),
        "the card does not name the release it is describing");
  for (const group of Object.keys(setup.groups || {})) {
    const wanted = (setup.groups[group] || []).map((c) => c.flag);
    check(wanted.every((f) => flags.includes(f)), `the card does not list the ${group} group (${wanted.join(", ")})`);
  }
  const unshown = (setup.total || 0) - flags.length;
  check(els.get("build-choices-rest").textContent.includes(`${unshown} further`),
        "the card does not say what it left out (got: " + els.get("build-choices-rest").textContent + ")");
  check(els.get("build-choices-command").textContent.startsWith("python setup.py"), "the command line under the boxes is missing");
  // The build log card is opened by renderBuildLog. It used to be reached through a
  // CSS selector passed to $, which is getElementById: the null dereference threw and
  // loadBuild's catch emptied the choices card, which is how the boxes disappeared.
  check(els.get("build-log-card").hidden === false, "a finished build job does not open its log card");
  check(els.get("build-log").textContent.includes("cmake"), "the build log card shows no log");
  if (steps.length) console.error("steps that threw: " + JSON.stringify(steps));
  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("the setup choices render from a build/status payload: " + flags.length + " controls");
  process.exit(0);
}, 500);

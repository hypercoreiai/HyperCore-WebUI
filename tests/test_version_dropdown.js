// Regression harness for the header's Model version dropdown.
//
// The dropdown is fed by GET /versions (serve/quant_store.py) and a choice is sent
// to POST /versions/select. This runs renderVersions against a stub DOM and checks
// the wiring in index.html, app.js and serve/server.py, so a change to the payload
// shape empties the box in the test rather than in the browser.
//
//   node tests/test_version_dropdown.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const APP = fs.readFileSync(path.join(ROOT, "serve", "web", "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");
const STORE = fs.readFileSync(path.join(ROOT, "serve", "quant_store.py"), "utf8");

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

// ---- 1. the wiring -------------------------------------------------------------
check(HTML.includes('id="version-select"'), "the header has no Model version select");
check(HTML.includes('id="build-quant"'), "the Build tab has no quant field");
check(APP.includes("async function loadVersions()"), "app.js has no loadVersions");
check(APP.includes("function renderVersions(active)"), "app.js has no renderVersions");
check(APP.includes("loadVersions();"), "loadVersions is never called at boot");
check(APP.includes('fetch("versions/select"'), "the dropdown never posts a choice");
check(APP.includes("quant: ($(\"build-quant\")"), "build/start does not send the quant the build is for");
check(SERVER.includes('path == "/versions"'), "GET /versions is not routed");
check(SERVER.includes("quant_store.view()"), "GET /versions does not call the store");
check(SERVER.includes('path == "/versions/select"'), "POST /versions/select is not routed");
check(SERVER.includes("quant_store.select(wanted)"), "the select route does not call quant_store.select");
check(SERVER.includes("def apply_model_version(svc: Service, entry: dict[str, Any]) -> tuple[bool, str]:"),
      "serve/server.py has no apply_model_version");
check(STORE.includes("def materialise(key: str) -> tuple[bool, str]:"),
      "serve/quant_store.py cannot place a build at engine/strata");

// ---- 2. renderVersions against a stub DOM ------------------------------------
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.value = ""; this.disabled = false;
    this.textContent = ""; this._html = "";
  }
  set innerHTML(v) { this._html = v; this.children = []; }
  get innerHTML() {
    if (this._html) return this._html;
    return this.children.map((c) => `<option value="${c.value}">${c.textContent}</option>`).join("");
  }
  appendChild(child) { this.children.push(child); }
}

const renderSrc = slice(APP, "function renderVersions(active) {", "\n}", "renderVersions");
const labelSrc = slice(APP, "function versionLabel(v) {", "\n}", "versionLabel");

// renderVersions reads the module-scope `versions`, so run it with that name bound.
function renderWith(versions, active) {
  const dom = { "version-select": new El("select"), "build-quant": new El("input") };
  const stubDoc = { createElement: (tag) => new El(tag) };
  const fn = new Function("$", "document", "versions", labelSrc + "\n" + renderSrc + "\nreturn renderVersions;");
  fn((id) => dom[id], stubDoc, versions)(active);
  return dom;
}

function optionValues(html) {
  return (html.match(/<option value="([^"]*)"/g) || []).map((m) => m.match(/value="([^"]*)"/)[1]);
}

const V = [
  { key: "q2_0", label: "qwen3.8-flash-next-q2_0", quant: "q2_0", version: "0.1.45",
    ready: true, active: true },
  { key: "iq2_s", label: "qwen3.8-flash-next-iq2_s", quant: "iq2_s", version: "0.1.45",
    ready: true, active: false },
  { key: "q4_k_m", label: "qwen3.8-flash-next-q4_k_m", quant: "q4_k_m", version: null,
    ready: false, active: false },
];

let dom = renderWith(V, V[0]);
const opts = optionValues(dom["version-select"].innerHTML);
check(opts.length === 3, `the dropdown has ${opts.length} options, expected 3`);
check(opts[0] === "q2_0" && opts[1] === "iq2_s" && opts[2] === "q4_k_m",
      "the options are not the stored versions");
check(dom["version-select"].value === "q2_0", "the active version is not selected");
check(dom["version-select"].innerHTML.includes("- running"), "the running version is not marked");
check(dom["version-select"].innerHTML.includes("q4_k_m (no engine binary)"),
      "a version with no engine binary is not flagged");
check(dom["build-quant"].value === "q2_0", "the Build tab quant field is not prefilled");

dom = renderWith(V, null);
check(dom["version-select"].value === "q2_0", "with nothing active the first version is not chosen");

dom = renderWith([], null);
check(dom["version-select"].innerHTML.includes("no model versions built"),
      "an empty store is not reported as empty");
check(dom["version-select"].disabled === true, "an empty dropdown is left enabled");

// ---- 3. the choice handler sends the right thing -----------------------------
const handlerSrc = slice(APP, '$("version-select").onchange = async () => {', "\n};", "the version onchange");
check(handlerSrc.includes('method: "POST"'), "the change handler does not POST");
check(handlerSrc.includes("JSON.stringify({ version: key })"), "the change handler sends no version key");
check(handlerSrc.includes('setPill("queued"'), "the header pill is not moved while a switch runs");
check(handlerSrc.includes("sel.disabled = false;"), "the dropdown is left disabled after a failed switch");

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("all model-version checks passed");

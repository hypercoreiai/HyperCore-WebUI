// Does "Clone and build from source" carry the Setup choices with it?
//
// The run config a build writes is a copy of the nearest run config on the box - the
// shipped template when there is none - so on a box whose Setup card said
// --context 262144 --kv q4_0 the engine still started on the template's 131072 and
// int8. The card's boxes are sent with the build request now, and serve/build_api.py
// writes them into the config the build just wrote.
//
// Two things are checked: the wiring (the page sends the boxes, the server forwards
// them, the build applies them), and the behaviour - the card's own code, run against
// a stub DOM, says which quant a build is for and which settings it holds.
//
//   node tests/test_build_source_choices.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const APP = fs.readFileSync(path.join(ROOT, "serve", "web", "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");
const API = fs.readFileSync(path.join(ROOT, "serve", "build_api.py"), "utf8");
const STORE = fs.readFileSync(path.join(ROOT, "serve", "quant_store.py"), "utf8");
const OPTIONS = JSON.parse(fs.readFileSync(path.join(ROOT, "config", "build_options.json"), "utf8"));

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

function between(src, from, to, what) {
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`app.js no longer has ${what}: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(to, i);
  if (j < 0) throw new Error(`app.js no longer closes ${what}`);
  return src.slice(i, j);
}

// ---- 1. the page sends the boxes with the build -------------------------------
const startHandler = between(APP, '$("build-start").onclick = async () => {', "const poll = setInterval");
check(startHandler.includes("choices: activeBuildArgs()"),
      "the build request sends no Setup choices");
check(startHandler.includes("quant: buildTargetQuant()"),
      "the build request still reads the quant from the box alone");
check(!startHandler.includes('$("build-quant").value.trim()'),
      "the build-start handler still takes the quant straight from the box");

const fetchHandler = between(APP, '$("build-fetch").onclick = async () => {', "loadBuild();");
check(fetchHandler.includes("choices: activeBuildArgs()"),
      "installing a ready-made engine ignores the Setup choices");
check(fetchHandler.includes("quant: buildTargetQuant()"),
      "a ready-made install does not take the quant from the model box");

check(HTML.includes("the model named in the Setup choices"),
      "the quant box does not say where an empty value comes from");

// ---- 2. the server forwards them ---------------------------------------------
const startRoute = SERVER.slice(SERVER.indexOf('if path == "/build/start":'),
                                SERVER.indexOf('if path == "/versions/select":'));
check(startRoute.includes('req.get("choices")'), "POST /build/start drops the Setup choices");
check(startRoute.includes('req.get("quant")'), "POST /build/start no longer takes a quant");
const fetchRoute = SERVER.slice(SERVER.indexOf('if path == "/build/fetch-prebuilt":'),
                                SERVER.indexOf('if path == "/build/start":'));
check(fetchRoute.includes('req.get("choices")'), "POST /build/fetch-prebuilt drops the Setup choices");

// ---- 3. the build applies them -----------------------------------------------
check(API.includes("def start(self, tag: str, options: dict[str, Any], quant: str | None = None,\n             choices: Any = None)"),
      "BuildJob.start takes no choices");
check(API.includes("quant_store.apply_build_choices(key, self.choices)"),
      "a build never writes the Setup choices into its run config");
check(API.includes("quant_from_choices"),
      "the build cannot name itself for the model the card names");
check(API.includes('"choices": self.choices'),
      "the build records nothing about the settings it was asked for");
check(STORE.includes("RUN_CHOICE_FIELDS"), "quant_store has no table of settings the card can write");
check(STORE.includes("def apply_build_choices("), "quant_store has no apply_build_choices");

// ---- 4. the settings the table writes are flags the release accepts ----------
const declared = new Set((OPTIONS.setup_choices || []).map((c) => c.flag));
for (const flag of ["--context", "--kv", "--rope-scaling", "--rope-scale", "--vram-reserve-mib",
                    "--parallel", "--port", "--host", "--api-key", "--gpu"]) {
  check(declared.has(flag), `the run-config table writes ${flag}, which this release's setup.py does not have`);
}

// ---- 5. the card's own code, against a stub DOM -----------------------------
const flagBlock = between(APP, "const CHOICE_ORDER =", "function refreshImageControls");
const quantFn = between(APP, "function buildTargetQuant()", "function renderBuildChoices");
if (flagBlock.length < 400 || quantFn.length < 100) throw new Error("the card's flag block or buildTargetQuant is gone");

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

function parseControls(html) {
  // A stand-in for the browser: every control the card rendered, with the value a
  // user would have left in it (the preselected option, or the value attribute).
  const controls = [];
  // choiceField puts data-longest and the --choice-chars sizing style on the select,
  // so the attribute run between data-flag and the closing > must be allowed.
  for (const [, flag, body] of html.matchAll(/<select[^>]*data-flag="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    const selected = body.match(/<option value="([^"]+)" selected>/);
    controls.push({ dataset: { flag }, value: selected ? selected[1] : "" });
  }
  for (const [, , flag, attrs] of html.matchAll(/<input([^>]*)data-flag="([^"]+)"([^>]*)>/g)) {
    controls.push({ dataset: { flag }, value: (attrs.match(/value="([^\"]*)"/) || [null, ""])[1] });
  }
  return controls;
}

function cardFor(setupChoices, quantBoxValue) {
  const controls = [];
  const render = new Function("document",
    "const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/\\\"/g, '&quot;');\n"
    + flagBlock + "\nreturn { choiceField };")({ querySelectorAll: () => controls });
  controls.push(...parseControls(setupChoices.map((c) => render.choiceField(c)).join("")));
  const doc = {
    querySelectorAll: (sel) => (sel === "#build-choices-groups [data-flag]" ? controls : []),
    querySelector: (sel) => {
      const flag = (sel.match(/\[data-flag="([^"]+)"\]/) || [null, null])[1];
      return controls.find((c) => c.dataset.flag === flag) || null;
    },
  };
  const els = { "build-quant": { value: quantBoxValue } };
  const api = new Function("document", "$",
    flagBlock + "\n" + quantFn + "\nreturn { buildTargetQuant, activeBuildArgs };")(doc, (id) => els[id] || null);
  return { quant: api.buildTargetQuant(), args: api.activeBuildArgs() };
}

const choices = OPTIONS.setup_choices || [];
const pair = (args, flag) => args[args.indexOf(flag) + 1];

// Nothing chosen: the build is the default engine, exactly as before.
const nothing = cardFor(choices, "");
check(nothing.quant === "", "a build with no quant named is named for a model anyway");

// The model box answers for the quant when the quant box is empty.
const named = choices.map((c) => (c.flag === "--model" ? { ...c, default: "Q2_0" } : c));
check(cardFor(named, "").quant === "Q2_0",
      "the model named in the card is not the quant the build is stored under");

// The explicit box wins over the card.
check(cardFor(named, "IQ3_XXS").quant === "IQ3_XXS",
      "the quant box is overridden by the model box");

// The settings the card holds reach the request.
const settings = choices.map((c) => {
  if (c.flag === "--context") return { ...c, default: 262144 };
  if (c.flag === "--kv") return { ...c, default: "q4_0" };
  return c;
});
const args = cardFor(settings, "").args;
check(pair(args, "--context") === "262144", "the context box is not in the request");
check(pair(args, "--kv") === "q4_0", "the KV cache box is not in the request");

// A quant the card offers is a quant the store can key by.
for (const c of choices.filter((c) => c.flag === "--model")) {
  for (const value of c.choices || []) {
    const key = String(value).toLowerCase().replace(/[-/]/g, "_").replace(/\s+/g, "_");
    check(/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/.test(key),
          `the model box offers ${value}, which is not a quant name the store can use`);
  }
}

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("all source-build choice checks passed");

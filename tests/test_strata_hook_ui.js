// Regression harness for the Config view's "Run Strata setup" hook: the button is
// in the model settings card, app.js posts /config/strata-hook and reads the log
// from a byte offset, and serve/server.py answers the three routes. No browser: the
// wiring is checked against the source, the way the other UI checks are.
//
//   node tests/test_strata_hook_ui.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const APP = fs.readFileSync(path.join(ROOT, "serve", "web", "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");
const HOOK = fs.readFileSync(path.join(ROOT, "serve", "strata_hook.py"), "utf8");

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

// The controls are in the model settings card, not somewhere the page never renders.
const card = HTML.slice(HTML.indexOf('id="cfg-card"'), HTML.indexOf("</section>", HTML.indexOf('id="cfg-card"')));
check(card.includes('id="strata-hook"'), "the model settings card has the setup hook button");
check(card.includes('id="strata-hook-stop"'), "the card has a Stop button for a download in progress");
check(card.includes('id="strata-hook-msg"'), "the card has a status line");
check(card.includes('id="strata-hook-log"'), "the card has a log panel");
check(card.includes('class="build-log"'), "the hook log reuses the styled build-log block");

// app.js wires every control it renders, and none of the wiring is dead.
check(APP.includes('$("strata-hook").addEventListener'), "app.js wires the Run Strata setup button");
check(APP.includes('$("strata-hook-stop").addEventListener'), "app.js wires the Stop button");
check(APP.includes('fetch("config/strata-hook", {method: "POST"'), "the button POSTs /config/strata-hook");
check(APP.includes('fetch("config/strata-hook/stop", {method: "POST"'), "the stop button POSTs /config/strata-hook/stop");
check(APP.includes("config/strata-hook?offset="), "the log is polled from a byte offset, not re-sent whole");
check(APP.includes("hookOffset = b.offset"), "the offset advances with what was read");
check(APP.includes("if (b.state !== \"running\")") && APP.includes("clearInterval(hookTimer)"),
      "the poll stops when the hook finishes");
check(APP.includes("stop.hidden = b.state !== \"running\""), "Stop only appears while a download is running");

// The server answers all three routes, and the hook module keeps its contract.
check(SERVER.includes('path == "/config/strata-hook"'), "GET/POST /config/strata-hook is routed");
check(SERVER.includes('path == "/config/strata-hook/stop"'), "POST /config/strata-hook/stop is routed");
check(HOOK.includes("subprocess.Popen(command, cwd=str(setup.parent), stdout=log, stderr=log)"),
      "the setup script is detached with its output to a file, not a pipe");
check(HOOK.includes("_under_scratch"), "tmp/ is refused as a checkout: it is scratch");
check(HOOK.includes("threading.Thread(target=self._finish, args=(proc, log), daemon=True)"),
      "the exit code is collected off the request thread");

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("all hook checks passed");

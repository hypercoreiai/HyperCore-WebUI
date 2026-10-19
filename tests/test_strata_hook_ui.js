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
check(HOOK.includes("subprocess.Popen(command, cwd=str(setup.parent), stdin=subprocess.DEVNULL,") &&
      HOOK.includes("stdout=log, stderr=log)"),
      "the setup script is detached with its output to a file, not a pipe");
check(HOOK.includes("_under_scratch"), "the hook knows which folder is the clone a build keeps");
check(HOOK.includes("_scratch_checkout"), "the clone in tmp/ is a checkout when there is nothing else on the box");
check(HOOK.includes("def durable_data_dir()"), "a setup run from that clone pins its data folder outside tmp/");
check(HOOK.includes("_build_busy"), "a setup is refused while the Build tab is compiling the same clone");
check(APP.includes("model files go to ${b.data_dir}"), "the hook cards say where the packs of a clone setup go");
check(HOOK.includes("_run_config_cwd"),
      "the checkout search reads the run config's cwd, the Strata folder the engine runs from");
check(HOOK.includes("SIBLING_DIRS"), "a Strata folder next to this project is searched");
check(SERVER.includes("run_config=svc.config_path"),
      "the setup route tells the hook which run config is in use");
check(HOOK.includes("\"checkout\": self.checkout"), "the hook reports the folder it ran setup.py in");
check(HOOK.includes("threading.Thread(target=self._finish, args=(proc, log), daemon=True)"),
      "the exit code is collected off the request thread");

// The Build tab reports the same job while it runs: its run button becomes the stop
// button, and the stage the setup is on comes from setup.py's own step markers.
check(APP.includes('"Kill Build"'), "the Build tab's run button does not become a stop button while a setup runs");
check(APP.includes('fetch("config/strata-hook/stop"'), "the Build tab cannot stop a setup it started");
check(HOOK.includes("STEP_RE") && HOOK.includes('"stage": self.stage'),
      "the hook does not report the step setup.py has reached");
check(HOOK.includes("def _note(self, lines: list[str]) -> None:"), "the hook collects no problems from the log");
check(HOOK.includes("def _last_lines(self, count: int) -> list[str]:"),
      "a run that fails without an [X] line has nothing to show");

// The boxes are read from the release in the dropdown; the hook runs the checkout on the
// box, which may be an older release. A flag that script never declared is dropped by
// the hook and named on the page instead of ending the run in an argparse error.
check(HOOK.includes("FLAG_RE") && HOOK.includes("def accepted_flags(setup: Path)"),
      "the hook reads the setup.py it is about to run for its own flags");
check(HOOK.includes("def strip_unaccepted("),
      "unaccepted flags are removed before the command is built");
check(HOOK.includes("\"dropped\": list(self.dropped)"),
      "the status reports the flags it dropped");
check(APP.includes("dropped: this setup.py does not accept them"),
      "the page names the flags the checkout does not accept");
check(APP.includes("b.command.slice(1).join(\" \")"),
      "the command line under the boxes is replaced by the one that actually runs");

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("all hook checks passed");

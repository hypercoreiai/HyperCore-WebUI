// Regression harness for the Build tab's "Available releases" box. The dropdown was
// empty because GET /build/status put the newest single release under `latest` while
// the page read `latest.releases`. This runs loadBuild against a stub DOM and a stub
// fetch, so a change to the payload shape or to the fallback fails the test instead of
// silently emptying the box in the browser.
//
//   node tests/test_build_tab_releases.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const APP = fs.readFileSync(path.join(ROOT, "serve", "web", "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");
const API = fs.readFileSync(path.join(ROOT, "serve", "build_api.py"), "utf8");

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

// ---- 1. the box exists, and the route answers from one function ---------------
check(HTML.includes('id="build-tag"'), "the Build tab has no releases select");
check(HTML.includes("Build from"), "the Build tab has no build-source card");
check(SERVER.includes('path == "/build/status"'), "GET /build/status is not routed");
check(SERVER.includes("build_api.build_status()"), "the route does not call build_status");
check(API.includes("def build_status() -> dict[str, Any]:"), "serve/build_api.py has no build_status");
check(API.includes('"latest": listing'), "build_status returns the listing, not one release");
check(API.includes('"newest": newest'), "build_status does not name the newest release");
// The scan of tmp/ is what makes the box a choice of build sources rather than a list of
// GitHub's tags: a machine that has built two releases has two trees on disk.
check(API.includes("def local_releases("), "serve/build_api.py has no scan of the checkouts in tmp/");
check(API.includes('"local": local_releases()'), "build_status does not carry the scan");
check(APP.includes("renderBuildChoices(s.setup"), "loadBuild does not render the setup choices of the installed release");
check(APP.includes("renderBuildStatus(s.job"), "loadBuild does not report the state of a build in progress");
check(APP.includes("refreshBuildStatus()"), "loadBuild never asks the hook for the setup it may already be running");

// ---- 2. run loadBuild against a stub DOM ---------------------------------------
const loadBuildSrc = slice(APP, "function loadBuild() {", "\n}", "loadBuild");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function runLoadBuild(payload, fetchImpl) {
  const dom = {
    "build-installed": { innerHTML: "" },
    "build-tag": { innerHTML: "" },
    "build-update-note": { innerHTML: "", textContent: "" },
    // The setup-choices card is a separate card; loadBuild must not trip on it.
    "build-choices": { hidden: true },
    "build-choices-tag": { textContent: "" },
    "build-choices-groups": { innerHTML: "" },
    "build-choices-rest": { textContent: "" },
    "build-choices-command": { textContent: "" },
    "build-choices-note": { textContent: "" },
  };
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  const calls = { choices: null, options: null, log: null, status: null };
  const fn = new Function("$", "esc", "headers", "renderBuildOptions", "renderBuildChoices",
    "renderBuildLog", "renderBuildStatus", "refreshBuildStatus", "fetch",
    loadBuildSrc + "\nreturn loadBuild;");
  fn((id) => dom[id], esc, () => ({}),
     (options) => { calls.options = options; },
     (setup, tag, error) => { calls.choices = { setup, tag, error }; },
     (job) => { calls.log = job; },
     (job, kind) => { calls.status = { job, kind }; },
     () => {},
     fetchImpl)();
  lastChoices = calls;
  return dom;
}

let lastChoices = null;
const respond = (payload) => () => Promise.resolve({ json: () => payload });

function status(installed, listing, newest, update, setup, job = { state: "idle" }, local = []) {
  return { installed, latest: listing, newest, update_available: update,
           options: null, setup: setup || { groups: {}, hidden: 0, total: 0 },
           local, job };
}

const RELEASES = [
  { tag: "v0.1.45", version: "0.1.45", published: "2026-10-01T00:00:00Z", assets: [] },
  { tag: "v0.1.44.1", version: "0.1.44.1", published: "2026-09-11T00:00:00Z", assets: [] },
  { tag: "v0.1.44", version: "0.1.44", published: "2026-09-01T00:00:00Z", assets: [] },
];

function optionTags(html) {
  return (html.match(/<option value="([^"]+)"/g) || []).map((m) => m.match(/value="([^"]+)"/)[1]);
}

async function main() {
  // a) GitHub answered and a newer release exists
  let dom = runLoadBuild(
    status({ version: "0.1.44", source: "local", worker: true },
           { repo: "Niko1221/Strata", releases: RELEASES, checked_at: "2026-10-18T00:00:00Z" },
           RELEASES[0], true),
    respond(status({ version: "0.1.44", source: "local", worker: true },
                   { repo: "Niko1221/Strata", releases: RELEASES }, RELEASES[0], true)));
  await wait(50);
  const tags = optionTags(dom["build-tag"].innerHTML);
  check(tags.length === RELEASES.length + 1,
        `the dropdown has ${tags.length} options, expected ${RELEASES.length + 1}`);
  check(tags[0] === "v0.1.45", "the newest release is not the first option");
  check(tags[tags.length - 1] === "main", "the dropdown has no main fallback");
  check(dom["build-update-note"].textContent.includes("Update available: 0.1.44 -> 0.1.45"),
        "the update note does not name the newer release");
  check(dom["build-installed"].innerHTML.includes("Worker</dt><dd>built"),
        "the installed card does not report a built worker");
  check(lastChoices && lastChoices.choices && lastChoices.choices.tag === "0.1.44",
        "the setup-choices card is not filled for the installed release");

  // b) the installed engine is already the newest
  dom = runLoadBuild(
    status({ version: "0.1.45", source: "built-from-source", worker: false },
           { repo: "x", releases: RELEASES }, RELEASES[0], false),
    respond(status({ version: "0.1.45", source: "built-from-source", worker: false },
                   { repo: "x", releases: RELEASES }, RELEASES[0], false)));
  await wait(50);
  check(dom["build-update-note"].textContent.includes("latest release (0.1.45)"),
        "the note does not say the engine is current");
  check(dom["build-installed"].innerHTML.includes("not built (engine runs in direct mode)"),
        "the installed card does not say the worker is missing");

  // c) GitHub unreachable: the box still has a target and says why
  dom = runLoadBuild(
    status({ version: "0.1.44" }, { error: "timed out", releases: [] }, null, false),
    respond(status({ version: "0.1.44" }, { error: "timed out", releases: [] }, null, false)));
  await wait(50);
  check(dom["build-tag"].innerHTML.includes('value="main"'), "with no releases the dropdown is empty");
  check(dom["build-update-note"].textContent.includes("no releases"), "the offline case gives no reason");

  // d) the endpoint itself cannot be reached
  dom = runLoadBuild(null, () => Promise.reject(new Error("connection refused")));
  await wait(50);
  check(dom["build-tag"].innerHTML.includes('value="main"'), "a failed request leaves the dropdown empty");
  check(dom["build-update-note"].textContent.includes("could not read the build status"),
        "a failed request is not reported");
  // The setup-choices card is hidden until renderBuildChoices opens it. A failed status
  // call must not leave it hidden: the tab would look like it has no build controls at all.
  check(dom["build-choices"].hidden === false, "a failed status call leaves the setup-choices card hidden");
  check(dom["build-choices-rest"].textContent.includes("could not be read"),
        "the empty setup-choices card gives no reason for being empty");

  // e) a build is already running: loadBuild feeds its state to the status card
  const running = status({ version: "0.1.44", source: "local", worker: true },
                         { repo: "x", releases: RELEASES }, RELEASES[0], false,
                         { state: "idle" }, { state: "building", elapsed_s: 240, version: "0.1.44" });
  runLoadBuild(running, respond(running));
  await wait(50);
  check(lastChoices && lastChoices.status && lastChoices.status.job.state === "building",
        "a build in progress is not reported to the status card");
  check(lastChoices.status.kind === "source", "the status card is not told which job it is showing");

  // f) the checkouts this machine already has. A folder's name is the tag it was cloned at
  //    and its CMakeLists.txt may declare a different version, so both are shown and the
  //    value sent is the tag the scan reported. A folder that is not a checkout is
  //    filtered. A tree outside tmp/ is a checkout the hook would run setup.py from, so it
  //    is offered too and shows its folder, because its name is not a tag.
  const LOCAL = [
    { tag: "v0.2.0-beta", version: "0.1.45.3", folder: "tmp/strata-v0.2.0-beta", source: "tmp",
      has_setup: true, has_cmake: true, modified: "2026-10-18T00:00:00Z" },
    { tag: "v0.1.45", version: "0.1.45", folder: "tmp/strata-v0.1.45", source: "tmp",
      has_setup: true, has_cmake: true, modified: "2026-10-01T00:00:00Z" },
    { tag: "Strata", version: "0.1.45.4", folder: "~/Strata", source: "checkout",
      has_setup: true, has_cmake: true, modified: "2026-10-19T00:00:00Z" },
    { tag: "strata-data", version: null, folder: "tmp/strata-data", source: "tmp",
      has_setup: false, has_cmake: false, modified: "2026-10-18T00:00:00Z" },
  ];
  const p = status({ version: "0.1.45", source: "local", worker: false },
                   { repo: "x", releases: [] }, null, false, null, { state: "idle" }, LOCAL);
  dom = runLoadBuild(p, respond(p));
  await wait(50);
  const localTags = optionTags(dom["build-tag"].innerHTML);
  check(localTags.length === 4,
        `the box has ${localTags.length} options, expected 4: the scan's three checkouts plus main (${localTags.join(", ")})`);
  check(localTags[0] === "v0.2.0-beta" && localTags[1] === "v0.1.45",
        `the checkouts are not first or not newest first: ${localTags.join(", ")}`);
  check(localTags[2] === "Strata",
        `a checkout outside tmp/ is not offered as a build source: ${localTags.join(", ")}`);
  check(dom["build-tag"].innerHTML.includes("~/Strata"),
        "a checkout outside tmp/ shows no folder, so the box cannot be told from a clone");
  check(localTags[3] === "main", "the box has no main fallback when GitHub gives no releases");
  check(dom["build-tag"].innerHTML.includes("0.1.45.3"),
        "the option does not show the version the checkout declares, only the tag its folder is named for");
  check(dom["build-tag"].innerHTML.includes("checkouts on this machine"),
        "the box does not label the local checkouts as local checkouts");
  check(dom["build-update-note"].textContent.includes("GitHub gave no releases"),
        "the note does not say why there are no published releases");
  check(dom["build-update-note"].textContent.includes("3 checkout"),
        "the note does not count the checkouts that can be built from: " + dom["build-update-note"].textContent);

  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("all release-box checks passed");
}

main();

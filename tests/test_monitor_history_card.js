// Regression harness for the Monitor tab's "Chat history" card: the card must be
// in the Monitor view, app.js must fill it from GET /history/status, the rebuild
// button must POST /history/distil, and the server must answer both routes.
// Run with:
//   node tests/test_monitor_history_card.js
//
// There is no browser here: the card's functions are lifted out of app.js and run
// against a stub DOM and a stub fetch, so the test fails if an endpoint, a fact
// label, or the running-state guard changes.
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const WEB = path.join(ROOT, "serve", "web");
const APP = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");
const ARCHIVE = fs.readFileSync(path.join(ROOT, "serve", "chat_archive.py"), "utf8");

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

// ---- 1. the card is in the Monitor view, with a rebuild button --------------
const monitorAt = HTML.indexOf('id="view-monitor"');
check(monitorAt >= 0, "index.html has no Monitor view");
const cardAt = HTML.indexOf('id="history-card"');
check(cardAt >= 0, "index.html has no history card");
check(cardAt > monitorAt, "the history card is not inside the Monitor view");
check(/class="st-card history-card" id="history-card" hidden/.test(HTML),
      "the history card is not hidden until the data arrives");
check(HTML.includes('id="history-rebuild"'), "the card has no Rebuild now button");
check(HTML.includes("data/history/_old_chats"), "the card does not say where the archive lives");

// ---- 2. app.js loads the card on the Monitor tab and refreshes it -----------
const loader = slice(APP, "async function loadHistory() {", "\n}", "loadHistory");
const renderer = slice(APP, "function renderHistory() {", "\n}", "renderHistory");
const rebuild = slice(APP, '$("history-rebuild").onclick = async () => {', "};", "the rebuild handler");

check(loader.includes('"history/status"'), "loadHistory does not read history/status");
check(loader.includes("headers()"), "loadHistory sends no headers, so it would 401");
check(loader.includes("renderHistory()"), "loadHistory never renders the card");
check(APP.includes("if (tab === \"monitor\") { loadMcp(); loadAssistMonitor(); loadHistory(); }"),
      "the Monitor tab does not load the history card");
check(APP.includes("loadMcp(); loadAssistMonitor(); loadHistory(); }       // server states change rarely"),
      "the poll loop does not refresh the history card");

const LABELS = ["Transcripts here", "Chat summaries", "Topic files", "Notes", "Last archived", "Last rebuild"];
for (const label of LABELS) check(renderer.includes(`"${label}"`), `renderHistory no longer shows "${label}"`);
check(renderer.includes("d.running ? \"rebuilding\""), "the card does not say the rebuild is running");
check(renderer.includes("\"history-rebuild\").disabled = !!d.running"),
      "the rebuild button is not disabled while a rebuild is running");
check(rebuild.includes('"/history/distil"') && rebuild.includes("method: \"POST\""),
      "the rebuild button does not POST /history/distil");
check(rebuild.includes("btn.textContent = \"Rebuild now\""), "the rebuild button never returns to its label");

// ---- 3. the server answers both routes ---------------------------------------
check(/if path == "\/history\/status":/.test(SERVER), "GET /history/status is not routed");
check(SERVER.includes("chat_archive.history_status()"), "GET /history/status does not call history_status");
check(/if path == "\/history\/distil":/.test(SERVER), "/history/distil is not routed");
check(SERVER.includes("chat_archive.run_builder()"), "POST /history/distil does not run the builder");

// ---- 4. history_status counts documents, not every file --------------------
check(ARCHIVE.includes("def count_markdown(directory: Path) -> int:"),
      "history_status no longer counts only .md files");
check(/"transcripts": count_markdown\(OLD_CHATS_DIR\)/.test(ARCHIVE),
      "the transcript count is not taken from the Markdown files");

// ---- 5. run the renderer against a stub DOM ----------------------------------
const dom = {};
const $ = (id) => (dom[id] ||= { textContent: "", innerHTML: "", hidden: true, disabled: false });

let historyState = {
  old_chats: "/data/history/_old_chats",
  transcripts: 208, summaries: 208, topics: 14, notes: 8,
  newest: "2026-10-17-23-12-real.md",
  distilled: { built: "2026-10-17T23:00:00Z", transcripts: 208, topics: 14 },
  distil: { auto: true, running: true, last: { started: "2026-10-17T23:12:00Z", seconds: 17.4, ok: true, output: "208 transcripts" } },
};

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "-" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const renderHistory = eval("(" + renderer + ")");
renderHistory();

check(dom["history-card"].hidden === false, "renderHistory left the card hidden");
check(dom["history-sum"].textContent === "rebuilding", "the summary does not show the running state");
check(dom["history-rebuild"].disabled === true, "the rebuild button is enabled while a rebuild runs");
check(dom["history-facts"].innerHTML.includes("Transcripts here"), "no fact rows were written");
check(dom["history-facts"].innerHTML.includes("208"), "the transcript count is missing from the rows");
check(dom["history-facts"].innerHTML.includes("17.4 s"), "the last rebuild time is not formatted");

const idle = {
  transcripts: 3, summaries: 0, topics: 0, notes: 0, newest: null, distilled: null,
  distil: { auto: true, running: false, last: null },
};
historyState = idle;
renderHistory();
check(dom["history-sum"].textContent === "0 topics", "an idle card does not report the topic count");
check(dom["history-rebuild"].disabled === false, "the rebuild button stays disabled with nothing running");
check(dom["history-facts"].innerHTML.includes("none yet"), "an empty archive is not reported as none yet");
check(dom["history-facts"].innerHTML.includes("none this session"), "a rebuild that never ran is not reported");

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("ok: the Monitor history card is wired to /history/status and /history/distil");

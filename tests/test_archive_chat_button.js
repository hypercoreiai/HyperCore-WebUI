// Regression harness for the composer's "Save to history" button: it must sit
// next to the download button in index.html, and its handler in app.js must post
// the same Markdown the download button saves. Run with:
//   node tests/test_archive_chat_button.js
//
// There is no browser here: the two blocks are lifted out of app.js and run
// against a stub DOM and a stub fetch, so the test fails if the button, its
// title, the endpoint, or the payload changes.
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const WEB = path.join(ROOT, "serve", "web");
const APP = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const HTML = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const SPRITE = fs.readFileSync(path.join(WEB, "sprite.svg"), "utf8");
const SERVER = fs.readFileSync(path.join(ROOT, "serve", "server.py"), "utf8");

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

// ---- 1. the button is in the composer bar, beside the download button --------
const exportAt = HTML.indexOf('id="export-btn"');
const archiveAt = HTML.indexOf('id="archive-btn"');
check(exportAt >= 0, "index.html has no export button");
check(archiveAt >= 0, "index.html has no archive button");
check(archiveAt > exportAt, "the archive button must come after the download button");
const between = HTML.slice(exportAt, archiveAt);
check(between.includes("</button>"), "the archive button is not a separate button");
check(between.length < 400, "the archive button is not next to the download button");
check(HTML.includes('title="Save this chat to data/history/_old_chats"'),
      "the archive button's title does not say where the chat goes");
check(/class="st-btn st-btn--icon" id="archive-btn"/.test(HTML), "the archive button is not styled like its neighbours");

const icon = (HTML.match(/id="archive-btn"[\s\S]{0,300}?<use href="web\/sprite\.svg#(i-[a-z0-9-]+)"/) || [])[1];
check(icon && SPRITE.includes(`id="${icon}"`), `the archive button's icon ${icon || "(none)"} is not in sprite.svg`);

// ---- 2. app.js: the handler posts the transcript to /history/archive --------
const markdownFn = slice(APP, "function chatMarkdown() {", "\n}", "chatMarkdown");
const titleFn = slice(APP, "function chatTitle() {", "\n}", "chatTitle");
const handler = slice(APP, '$("archive-btn").onclick = async () => {', "};", "the archive handler");

check(markdownFn.includes("m.role === \"user\""), "chatMarkdown no longer writes the transcript");
check(handler.includes('"/history/archive"'), "the archive handler no longer posts to /history/archive");
check(handler.includes("headers: headers(true)"), "the archive request does not send the app's headers");
check(handler.includes("method: \"POST\""), "the archive handler is not a POST");
check(handler.includes("btn.disabled = true") && handler.includes("btn.disabled = false"),
      "the archive button is not re-enabled after the request");

check(SERVER.includes('path == "/history/archive"'), "serve/server.py has no /history/archive route");
check(SERVER.includes("chat_archive.archive_chat"), "the route does not call serve/chat_archive.py");

// ---- 3. the request carries the key the page was served with ----------------
const headersFn = slice(APP, "function headers(json = false) {", "\n}", "headers");
check(headersFn.includes("window.HYPERCORE_API_KEY"),
      "headers() no longer falls back to the key the page carries");
const runHeaders = new Function("store", "window", headersFn + "\nreturn headers;");
const realHeaders = runHeaders(
  {get: (k, d) => d},                       // nothing stored: private mode, cleared profile
  {HYPERCORE_API_KEY: "0000"});             // the key run.sh started the server with
check(realHeaders(true).Authorization === "Bearer 0000",
      "a browser with nothing stored sends no key, so the archive call 401s");
const typedHeaders = runHeaders({get: (k, d) => (k === "apikey" ? "typed-key" : d)}, {});
check(typedHeaders(true).Authorization === "Bearer typed-key", "a key typed in Settings must win");
const noKeyHeaders = runHeaders({get: (k, d) => d}, {});
check(!noKeyHeaders(true).Authorization, "a server with no key should not get an Authorization header");
check(noKeyHeaders(true)["Content-Type"] === "application/json", "the JSON content type is missing");

// ---- 4. run the handler against a stub DOM -----------------------------------
// new Function() runs in the global scope, so the transcript and the health line
// the handler reads are passed in as parameters and mutated in place.
const els = { "archive-btn": { disabled: false }, "export-btn": {} };
const $ = (id) => els[id];
const toasts = [];
const toast = (kind, title, text) => toasts.push([kind, title, text]);
const headers = (json = false) => (json ? { "Content-Type": "application/json" } : {});
const health = { model: "test-model" };
const chat = [];
let posts = [];
const canned = [];
const fetchStub = async (url, opts) => {
  posts.push({ url, opts, disabledAtCall: els["archive-btn"].disabled });
  const body = canned.shift() || { ok: true, json: { path: "/tmp/history/_old_chats/2026-01-01-00-00-chat.md" } };
  if (body.throw) throw new Error(body.throw);
  return { ok: body.ok, status: body.status || 200, json: async () => body.json };
};

const run = new Function("$", "toast", "fetch", "headers", "messages", "health",
                         markdownFn + "\n" + titleFn + "\n" + handler + "\n");
run($, toast, fetchStub, headers, chat, health);
const click = els["archive-btn"].onclick;

(async () => {
  // nothing to save: no request at all
  chat.length = 0;
  await click();
  check(posts.length === 0, "an empty chat still posted to the server");
  check(toasts.length === 1 && toasts[0][0] === "info", "an empty chat is not reported");

  // a real chat, with a tool call
  chat.push(
    { role: "user", text: "Archive this please\nsecond line" },
    { role: "assistant", text: "done", reasoning: "thinking hard",
      tools: [{ name: "docs__edit_file", server: "docs", ok: true, result: "2 edits", arguments: { path: "a.py" } }] },
  );
  canned.push({ ok: true, json: { path: "/tmp/history/_old_chats/2026-01-01-00-00-archive-this-please.md",
                                 name: "x.md", distil: { queued: true, reason: "rebuilding" } } });
  await click();
  check(posts.length === 1, "the chat was not posted");
  const post = posts[0];
  check(post.url === "/history/archive", "the archive request went to " + post.url);
  check(post.opts.headers["Content-Type"] === "application/json", "the archive request is not JSON");
  check(post.disabledAtCall === true, "the button stayed clickable while the request was in flight");
  check(els["archive-btn"].disabled === false, "the button is left disabled after the request");
  const sent = JSON.parse(post.opts.body);
  check(sent.chat.includes("## You\n\nArchive this please"), "the transcript is missing the user turn");
  check(sent.chat.includes("## test-model"), "the transcript is missing the assistant turn");
  check(sent.chat.includes("<summary>Tool docs / docs__edit_file</summary>"), "the transcript is missing the tool call");
  check(sent.title === "Archive this please", "the title is not the first line of the first user turn");
  check(toasts[toasts.length - 1][0] === "success", "a successful archive is not reported as one");
  check(toasts[toasts.length - 1][2].includes("topic files are being rebuilt"),
        "the toast does not say the distillation was queued");

  // the rebuild is already running: the toast says so instead of implying it started
  posts.length = 0;
  canned.push({ ok: true, json: { path: "/tmp/history/x.md", name: "x.md",
                                 distil: { queued: false, reason: "a rebuild is already running" } } });
  await click();
  check(toasts[toasts.length - 1][2].includes("already running"), "the queued rebuild is not reported");

  // the server refuses
  posts.length = 0;
  canned.push({ ok: false, status: 400, json: { error: { message: "chat: there is nothing to save yet" } } });
  await click();
  check(toasts[toasts.length - 1][0] === "error", "a 400 from the server is not reported as an error");
  check(toasts[toasts.length - 1][2].includes("nothing to save"), "the server's message is dropped");
  check(els["archive-btn"].disabled === false, "the button is left disabled after a failed request");

  // the network is down
  posts.length = 0;
  canned.push({ throw: "connection reset" });
  await click();
  check(toasts[toasts.length - 1][0] === "error", "a failed request is not reported");
  check(els["archive-btn"].disabled === false, "the button is left disabled after a network failure");

  // no key in the browser: the server's 401 must say where to put the key
  posts.length = 0;
  canned.push({ ok: false, status: 401, json: { error: { type: "authentication_error", message: "missing or wrong API key" } } });
  await click();
  check(toasts[toasts.length - 1][0] === "error", "a 401 is not reported as an error");
  check(/API key/i.test(toasts[toasts.length - 1][2]), "the 401 does not tell the user about the API key");
  check(/Settings/i.test(toasts[toasts.length - 1][2]), "the 401 does not say where to add the key");

  // the process behind the page predates the route: offer the file instead
  posts.length = 0;
  let fallbackRan = false;
  const toastWithAction = (kind, title, text, ms, action) => toasts.push([kind, title, text, ms, action]);
  const saveAsMarkdown = () => { fallbackRan = true; };
  // re-run the handler with a toast that can carry an action; the assignment inside
  // the handler puts the new onclick on the stub element, so `click` is still valid
  // for the plain case and this one for the fallback.
  const retry = new Function("$", "toast", "fetch", "headers", "messages", "health", "saveChatMarkdown",
                            markdownFn + "\n" + titleFn + "\n" + handler + "\n");
  retry($, toastWithAction, fetchStub, headers, chat, health, saveAsMarkdown);
  canned.push({ ok: false, status: 404, json: { error: { message: "not found" } } });
  await els["archive-btn"].onclick();
  const last = toasts[toasts.length - 1];
  check(last[0] === "warn", "a missing route is not reported as a warning");
  check(last[4] && last[4].label === "Save as Markdown", "the 404 offers no way to save the chat");
  last[4].run();
  check(fallbackRan, "the fallback action does not save the chat as Markdown");
  check(els["archive-btn"].disabled === false, "the button is left disabled after a 404");

  if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
  console.log("ok: the archive button is wired to /history/archive");
})();

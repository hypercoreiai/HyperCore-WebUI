// Regression harness for attaching a large document in the chat. Run with:
//   node serve/web/test_large_document.js
//
// The page bounds its own payload: whole turns are dropped, and a turn still bigger than the
// window is head/tail-clipped. The bug this guards against is the clip promising more room than
// the window has - a fixed allowance computed from the template prefix alone ignores the profile
// and every other turn - so the page sent a payload over the cap, the engine answered 400
// ("requests are never truncated"), and the chat was left with an empty bubble. These checks
// lift the real functions out of app.js and assert the guarantee: what is sent is never over the
// cap, and when the tool block is what makes it impossible the turn is sent without the tools.
"use strict";

const fs = require("fs");
const WEB = __dirname + "/../serve/web";
const APP = fs.readFileSync(WEB + "/app.js", "utf8");

function slice(src, from, to, what) {
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`app.js no longer has ${what}: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(to, i);
  if (j < 0) throw new Error(`app.js no longer closes ${what}`);
  return src.slice(i, j);
}

// The whole estimate: constants, templatePrefix, payloadChars, estimateTokens, headTail,
// clipToFit, pruneToContext, buildRequest.
const block = slice(APP, "const CHARS_PER_TOKEN", "function apiMessages", "the context-window block");

function makeWindow(settings, mcpInfo) {
  const run = new Function("settings", "mcpInfo", block +
    "\nreturn {estimateTokens, pruneToContext, buildRequest, templatePrefix, headTail, overflowRatio, serverCanFit," +
    " anchorFromCount, nearCap, payloadChars, getAnchor: () => anchor," +
    " setPrefix: (p) => { prefix = p; }, setAnchor: (a) => { anchor = a; }};");
  return run(settings, mcpInfo);
}

let failures = 0;
function check(name, want, got) {
  if (want === got) { console.log(`ok ${name}`); return; }
  failures += 1;
  console.log(`FAIL ${name}: wanted ${want}, got ${got}`);
}

// A document of the shape the user attached: a chat transcript with details blocks, tables, code
// fences and inline backticks. Half a megabyte of it.
function transcript() {
  let doc = "";
  for (let i = 0; i < 400; i++) {
    doc += "<details><summary>Thinking</summary>\n" + "lorem ipsum dolor sit amet ".repeat(40) + "\n</details>\n\n";
    doc += "| a | b | c |\n|---|---|---|\n";
    for (let r = 0; r < 10; r++) doc += `| ${i} | value ${r} | ${(r / 7).toFixed(3)} |\n`;
    doc += "\n```python\n" + "print('x')\n".repeat(20) + "```\n\n";
  }
  return doc;
}
const DOC = transcript();
check("the fixture is a large document", true, DOC.length > 500000);

// fileBlock, as the page builds it, so the fixture is the payload a real attachment produces.
const fileBlock = new Function(slice(APP, "function fileBlock(f) {", "function userText", "fileBlock") + "\nreturn fileBlock;")();

const PROFILE = "You are HyperCore. " + "Answer the user carefully. ".repeat(400);   // a real profile is a few KB
const PREFIX = {tokens: 23496, base: 244, tools: 41};   // what /mcp reports for the 7 servers in mcp.yaml

function payload() {
  return [{role: "system", content: PROFILE},
          {role: "user", content: fileBlock({name: "strata-chat.md", text: DOC})}];
}

// 1. Every window a real engine has: the payload the page would send fits it, and the tools are
//    dropped rather than the request refused when the tool block is what does not fit.
for (const cap of [132000, 32768, 16384, 8192, 4096]) {
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const req = buildRequestOf(w, cap);
  const sent = w.estimateTokens(req.messages, req.offerTools);
  check(`a 520 KB document fits a ${cap} token window`, true, sent <= cap);
  check(`  the page says it fits (cap ${cap})`, true, req.fits);
  check(`  the document is not sent whole (cap ${cap})`, true,
        req.messages.some((m) => m.content.includes("omitted")));
  check(`  the profile turn is untouched (cap ${cap})`, true, req.messages[0].content === PROFILE);
  check(`  the beginning of the document is kept (cap ${cap})`, true,
        req.messages[1].content.startsWith("File: strata-chat.md"));
  check(`  the end of the document is kept (cap ${cap})`, true, req.messages[1].content.endsWith("````"));
  // A window smaller than the tool block cannot carry the tools; it must still carry the chat.
  const withTools = w.pruneToContext(payload(), cap, true);
  check(`tools are offered exactly when that payload fits (cap ${cap})`, withTools.fits, req.offerTools);
  check(`the tools are never offered for a payload over the cap (cap ${cap})`, true,
        !req.offerTools || sent <= cap);
}
function buildRequestOf(w, cap) { return w.buildRequest(payload(), cap); }

// 2. Idempotent: pruning a payload that already fits changes nothing, so the engine's prefix
//    cache keeps hitting and the conversation is not re-prefilled every turn.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const first = w.pruneToContext(payload(), 132000, true);
  const again = w.pruneToContext(first.messages, 132000, true);
  check("a payload that fits is not reordered", 0, again.dropped);
  check("a payload that fits is not clipped again", 0, again.clipped);
  check("pruning twice is the same as pruning once",
        JSON.stringify(first.messages), JSON.stringify(again.messages));
}

// 3. Several large turns at once: the total, not the largest, is what has to fit.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const many = [{role: "system", content: PROFILE},
                {role: "user", content: "a".repeat(60000)},
                {role: "assistant", content: "b".repeat(60000)},
                {role: "user", content: "c".repeat(60000)}];
  const r = w.pruneToContext(many, 32768, true);
  check("several large turns together still fit", true, w.estimateTokens(r.messages, true) <= 32768);
  check("the oldest turn is dropped first", true, !r.messages.some((m) => m.content.startsWith("aaaa")));
  check("the newest turn is still there", true, r.messages.some((m) => m.content.startsWith("ccc")));
  check("the dropped turns leave a summary", true, r.summary.includes("compacted by HyperCore"));
}

// 4. A window smaller than the tool block: the tools are the part that cannot fit, so the page
//    asks for them only when the turn can carry them.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const req = w.buildRequest([{role: "user", content: "hello"}], 20000);
  check("the tool block alone overflows a 20000 token window", false,
        w.pruneToContext([{role: "user", content: "hello"}], 20000, true).fits);
  check("the same turn is sent without the tools", true, req.fits && !req.offerTools);
  check("without the tools only the template's own block is counted", 244, w.templatePrefix(false));
  check("with the tools the whole block is counted", 23496, w.templatePrefix(true));
  check("the setting still decides when nothing is passed", 23496, w.templatePrefix());
  const off = makeWindow({mcp: false}, {tools: 41});
  off.setPrefix(PREFIX);
  check("MCP switched off offers no tools", false, off.buildRequest([{role: "user", content: "hi"}], 20000).offerTools);
}

// 5. The anchor path: once the server has reported a real prompt size the estimate is anchored on
//    it, and the guarantee still holds.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  w.setAnchor({tokens: 1200, chars: 4000});
  const r = w.pruneToContext([{role: "user", content: "z".repeat(200000)}], 8192, false);
  check("an anchored estimate still bounds the payload", true, w.estimateTokens(r.messages, false) <= 8192);
}

// 6. Nothing left to cut: the clip stops instead of looping, and says the window is full.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const r = w.pruneToContext([{role: "system", content: "p".repeat(100000)},
                             {role: "user", content: "q".repeat(100000)}], 1000, true);
  check("a profile bigger than the window is reported, not silently sent", false, r.fits);
  check("the user turn was emptied rather than left oversized", 0, r.messages[1].content.length);
  check("headTail with no room returns nothing", "", w.headTail("anything at all", 0));
  check("headTail below 80 keeps a plain prefix", "anything a".slice(0, 9), w.headTail("anything at all", 9));
  check("headTail leaves a short text alone", "short", w.headTail("short", 80));
}

// 7. The old shape of the bug: the allowance was the room the prefix left, with a 2000 character
//    floor, and nothing re-checked it. A 40000 character turn against a 10000 token cap with a
//    9100 token prefix left the payload over the cap.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix({tokens: 9100, base: 244, tools: 41});
  const msg = (n) => ({role: "user", content: "a".repeat(n)});
  const huge = w.pruneToContext([msg(40000)], 10000, true);
  check("the oversized turn is cut", true, huge.messages[0].content.length < 40000);
  check("the cut is marked as a cut", true, huge.messages[0].content.includes("omitted"));
  check("what is sent fits the window", true, w.estimateTokens(huge.messages, true) <= 10000);
  check("the clipping is reported", true, huge.clipped > 0);
  const two = w.pruneToContext([msg(6000), msg(6000)], 10000, true);
  check("two turns over the cap are folded", 1, two.dropped);
  check("and the result still fits", true, w.estimateTokens(two.messages, true) <= 10000);
}

// 8. The engine's own measurement. The page counts characters; the engine counts tokens. When a
//    document is denser than prose the estimate is wrong, the request is refused with 400, and the
//    400 states the size it measured. The page turns that into a ratio, re-cuts, and tries once.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const msg = "prompt (355050 tokens) leaves no room to answer in the context (132000); requests are never truncated";
  const p = payload();
  const measured = w.overflowRatio(msg, p);
  check("the 400 is read as a measurement", true, measured && measured.tokens === 355050 && measured.ctx === 132000);
  check("the ratio is the payload the page actually sent", true, measured && measured.chars === w2chars(p));
  check("a message that is not an overflow is not read", null, w.overflowRatio("HTTP 500", p));
  check("a payload too small to measure is not read", null, w.overflowRatio(msg, [{role: "user", content: "hi"}]));
  // With the real ratio in the anchor, the re-cut payload fits the window the engine reported.
  const ratio = Math.min(1, Math.max(0.2, (measured.tokens - w.templatePrefix(true)) / measured.chars));
  w.setAnchor({tokens: measured.tokens, chars: measured.chars, ratio});
  check("the ratio is the engine's count over the characters actually sent", true,
        Math.abs(ratio - (355050 - 23496) / measured.chars) < 1e-9);
  // 4002 characters: the 4000 of the turn plus the "[]" an empty tool_calls list serialises to.
  check("the anchored estimate is the arithmetic of that ratio",
        measured.tokens + Math.round((4002 - measured.chars) * ratio),
        w.estimateTokens([{role: "user", content: "a".repeat(4000)}], true));
  const again = w.buildRequest(p, measured.ctx);
  check("the re-cut turn fits the real window", true, again.fits);
  check("  and is smaller than the payload that was refused", true,
        w.estimateTokens(again.messages, again.offerTools) < measured.tokens);
}
function w2chars(p) { return p.reduce((n, m) => n + String(m.content || "").length + JSON.stringify(m.tool_calls || []).length, 0); }

// 9. The fence. A document with a hundred thousand inline code spans used to crash the page:
//    the longest run of backticks was computed by spreading every match into Math.max, which
//    exhausts the stack. The attachment then never reached the chat at all.
{
  let dense = "";
  for (let i = 0; i < 300000; i++) dense += "`` x\n";
  check("the attachment limit is 2 MB, not 512 KB", true, APP.includes("const MAX_TEXT_FILE = 2 * 1024 * 1024"));
  let threw = null, block = "";
  try { block = fileBlock({name: "log.txt", text: dense}); } catch (e) { threw = e.message; }
  check("a document with 300,000 backtick runs does not blow the stack", null, threw);
  check("the fence is longer than any run in the file", true, block.includes("```") && !block.includes("````"));
  check("the file's own text is inside it", true, block.includes("`` x\n"));
  // and the whole thing still goes through the trimmer
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const req = w.buildRequest([{role: "system", content: PROFILE}, {role: "user", content: block}], 16384);
  check("a 1.5 MB attachment is trimmed to the window", true,
        req.fits && w.estimateTokens(req.messages, req.offerTools) <= 16384);
}

// 10. A server that trims for us. GET /mcp reports what this server does with an over-cap prompt;
//     the page should not refuse a turn the server would fit.
{
  const over = {tokens: 190000, fits: false};
  check("a plain engine is not asked to trim", false, w0serverCanFit(over, null));
  check("trim_context off is still a refusal", false, w0serverCanFit(over, {max_context: 262144, trim_context: false}));
  check("a server that trims can take it", true, w0serverCanFit(over, {max_context: 262144, trim_context: true}));
  check("  but not past the engine's own window", false, w0serverCanFit({tokens: 300000}, {max_context: 262144, trim_context: true}));
  check("  and not when the engine reports no context", false, w0serverCanFit(over, {max_context: 0, trim_context: true}));
}
function w0serverCanFit(kept, ctx) {
  const w = makeWindow({}, null);
  return w.serverCanFit(kept, ctx);
}

// 11. The first request of a chat has no anchor, so its size is the character guess. The engine can
//     count a prompt exactly without running the model; the anchor built from that count must
//     reproduce it, carry the real ratio, and still allow the tools-off fallback.
{
  const w = makeWindow({mcp: true}, {tools: 41});
  w.setPrefix(PREFIX);
  const p = payload();
  const chars = w.payloadChars(p);
  check("a turn near the cap is worth measuring", true, w.nearCap(p, 132000, true));
  check("a turn nowhere near it is not", false, w.nearCap([{role: "user", content: "hello there, how are you today"}], 132000, true));
  check("no cap means nothing to measure", false, w.nearCap(p, 0, true));
  check("the unanchored estimate is the character guess", Math.round(chars / 4) + PREFIX.tokens,
        w.estimateTokens(p, true));

  // The count endpoint renders the prompt without the tool block, so the tool definitions are added
  // from the prefix /mcp measured with the real tokenizer.
  const counted = 190000;   // a dense document: the engine counts more tokens than chars/4
  check("a real count is accepted", true, w.anchorFromCount(counted, p, true));
  const total = counted + (PREFIX.tokens - PREFIX.base);
  check("the anchored estimate is the count plus the tool block", total, w.estimateTokens(p, true));
  check("  and the same payload without tools is the count alone", counted, w.estimateTokens(p, false));
  const a = w.getAnchor();
  check("the ratio is the measured content over the characters sent", true,
        Math.abs(a.ratio - (total - PREFIX.tokens) / chars) < 1e-9);
  check("the ratio is the real one, not the prose constant", true, a.ratio > 1 / 4);
  check("a zero count is rejected", false, w.anchorFromCount(0, p, true));
  check("a tiny payload is not worth anchoring on", false, w.anchorFromCount(9, [{role: "user", content: "hi"}], true));

  // Measured on this engine: the character guess is ~18% low, so the unanchored first request
  // claims less than the engine will count. With the anchor it is exact.
  const real = 213831;                       // prompt_tokens the engine counted for this payload
  w.anchorFromCount(real - (PREFIX.tokens - PREFIX.base), p, true);
  check("the first request is no longer underestimated", real, w.estimateTokens(p, true));
  check("  and it is bounded by the window after pruning",
        true, w.pruneToContext(p, 132000, true).tokens <= 132000);
}

console.log(failures ? `${failures} checks failed` : "all checks passed");
process.exit(failures ? 1 : 0);

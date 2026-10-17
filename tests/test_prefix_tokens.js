// Regression harness for the context-window estimate in app.js. Run with:
//   node serve/web/test_prefix_tokens.js
//
// The page cannot count the tool block the chat template writes around the messages, so it
// takes the number from GET /mcp. This lifts the whole block out of app.js and runs it as-is
// against a stub server response: it fails if the page stops reading the reported prefix, if
// it applies the tool block to a chat that is not offering tools, or if the fallback is used
// when a real number is available.
"use strict";

const fs = require("fs");
const APP = fs.readFileSync(__dirname + "/app.js", "utf8");

function slice(src, from, to, what) {
  const i = src.indexOf(from);
  if (i < 0) throw new Error(`app.js no longer has ${what}: looked for ${JSON.stringify(from)}`);
  const j = src.indexOf(to, i);
  if (j < 0) throw new Error(`app.js no longer closes ${what}`);
  return src.slice(i, j);   // up to but not including the next definition
}

// The whole estimate: constants, templatePrefix, payloadChars, estimateTokens, pruneToContext.
const block = slice(APP, "const CHARS_PER_TOKEN", "function apiMessages", "the context-window block");

function makeWindow(settings, mcpInfo) {
  const run = new Function("settings", "mcpInfo", block +
    "\nreturn {estimateTokens, pruneToContext, templatePrefix," +
    " setPrefix: (p) => { prefix = p; }, setAnchor: (a) => { anchor = a; }};");
  return run(settings, mcpInfo);
}

let failures = 0;
function check(name, want, got) {
  if (want === got) { console.log(`ok ${name}`); return; }
  failures += 1;
  console.log(`FAIL ${name}: wanted ${want}, got ${got}`);
}

const CHARS = 4;
// A message's cost is its text plus the "[]" its empty tool_calls list serialises to.
const msg = (n) => ({role: "user", content: "a".repeat(n)});
const cost = (n) => Math.round((n + 2) / CHARS);

// 1. A server that reports nothing: the fallback, and nothing else.
const w1 = makeWindow({mcp: true}, {tools: 41});
check("fallback prefix is used when /mcp says nothing", 6200, w1.templatePrefix());
check("estimate with no payload", 6200, w1.estimateTokens([]));
check("estimate counts the payload too", 6200 + cost(1000), w1.estimateTokens([msg(1000)]));

// 2. The number the server reports wins, and it moves with the tool surface.
const w2 = makeWindow({mcp: true}, {tools: 41});
w2.setPrefix({tokens: 9100, base: 244, tools: 41});
check("reported prefix is used", 9100, w2.templatePrefix());
check("estimate follows it", 9100 + cost(1000), w2.estimateTokens([msg(1000)]));

// 3. A chat that is not offering tools pays only the template's own block.
const noServers = makeWindow({mcp: true}, {tools: 0});
noServers.setPrefix({tokens: 9100, base: 244, tools: 0});
check("no servers: base only", 244, noServers.templatePrefix());
const off = makeWindow({mcp: false}, {tools: 41});
off.setPrefix({tokens: 9100, base: 244, tools: 41});
check("MCP switched off: base only", 244, off.templatePrefix());

// 4. Once the anchor exists the prefix is not added a second time.
const w4 = makeWindow({mcp: true}, {tools: 41});
w4.setPrefix({tokens: 9100, base: 244, tools: 41});
w4.setAnchor({tokens: 9500, chars: 4000});
check("anchored estimate ignores the prefix", 9500 + Math.round((cost(8000) * CHARS - 4000) / CHARS),
      w4.estimateTokens([msg(8000)]));

// 5. Pruning uses the reported number: a window that the fallback would have kept fits now.
const cap = 10000;
const payload = [msg(9000)];
const kept = w2.pruneToContext(payload, cap);
check("a 9100-token prefix leaves room for the turn", 0, kept.dropped);
const tight = w2.pruneToContext([msg(6000), msg(6000)], cap);
check("two turns over the cap are folded", 1, tight.dropped);

// 6. A turn bigger than the window is clipped until the estimate fits, using the reported prefix.
const huge = w2.pruneToContext([msg(40000)], cap);
check("the oversized turn is cut", true, huge.messages[0].content.length < 40000);
check("the cut is marked as a cut", true, huge.messages[0].content.includes("omitted"));
check("what is sent fits the window", true, w2.estimateTokens(huge.messages) <= cap);

if (APP.includes("PREFIX_TOKENS")) {
  failures += 1;
  console.log("FAIL app.js still has a hardcoded PREFIX_TOKENS constant");
} else {
  console.log("ok the estimate has no hardcoded prefix");
}

console.log(failures ? `${failures} checks failed` : "all checks passed");
process.exit(failures ? 1 : 0);

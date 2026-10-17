// Regression harness for the tool-limit line the chat shows when a turn is cut short:
// the stream handler must record which cap was hit, and the meta line must name that cap
// and how many calls ran. Run with:
//   node tests/test_tool_round_meta.js
//
// No browser here: the two blocks are lifted out of app.js and run against stubs, so the
// test fails if the wording or the fields change without the test knowing.
const fs = require("fs");
const APP = fs.readFileSync("serve/web/app.js", "utf8");

const slice = (text, start, end, what) => {
  const a = text.indexOf(start);
  if (a < 0) { console.error(`could not find ${what} in app.js`); process.exit(1); }
  const b = text.indexOf(end, a);
  if (b < 0) { console.error(`could not find the end of ${what}`); process.exit(1); }
  return text.slice(a, b + end.length);
};

const handler = slice(APP, 'if (x.event === "limit") {', "\n  }", "the limit event handler");
const meta = slice(APP, "if (m.limit) {", "\n  }", "the meta line");

let failures = 0;
const check = (cond, msg) => { if (!cond) { failures++; console.error("FAIL: " + msg); } };

check(handler.includes("max_tool_calls"), "the limit handler does not read the call cap");
check(handler.includes("m.limitCallsRun"), "the limit handler does not record how many calls ran");
check(meta.includes("max_tool_calls"), "the meta line never mentions the call cap");
check(meta.includes("max_rounds"), "the meta line never mentions the round cap");

// ---- run both blocks against stubs -------------------------------------------
const fmt = (n) => String(n);
const run = new Function("x", "m", "beat", "paintBeat", "fmt",
  handler + "\nreturn m;\n");

const cases = [
  {
    name: "the call cap was hit",
    x: {event: "limit", max_rounds: 500, max_tool_calls: 500, rounds: 2, calls: 500},
    want: "500 tool call limit (mcp.max_tool_calls)",
  },
  {
    name: "the round cap was hit",
    x: {event: "limit", max_rounds: 8, max_tool_calls: 0, rounds: 8, calls: 12},
    want: "8 tool round limit (mcp.max_rounds)",
  },
];

for (const c of cases) {
  const m = {};
  run(c.x, m, null, () => {});
  check(m.limit === c.x.max_rounds, `${c.name}: the round cap was not stored`);
  check(m.limitCallsRun === c.x.calls, `${c.name}: the call count was not stored`);
  const out = new Function("m", "fmt", meta + "\nreturn m.meta;")(m, fmt);
  check(out.includes(c.want), `${c.name}: the line says "${out}" instead of naming ${c.want}`);
  check(!out.includes("undefined"), `${c.name}: the line contains undefined: ${out}`);
}

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("ok: the tool limit line names the cap that was actually hit");

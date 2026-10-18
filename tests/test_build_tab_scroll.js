// Regression harness for the Build tab's page height. body is overflow:hidden, so a tab
// that is taller than the viewport is clipped unless the tab itself scrolls: the Build tab
// had no scroll rule and the cards below the fold (setup choices, build options, build
// log) could not be reached. This asserts the rules that give the tab its scroll, and
// that they are still in the order that wins.
//
//   node tests/test_build_tab_scroll.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const CSS = fs.readFileSync(path.join(ROOT, "serve", "web", "app.css"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

// A rule only applies if it targets the view class the page actually uses.
const buildSection = /<section class="view view--build" id="view-build"/;
check(buildSection.test(HTML), "the Build tab section does not carry view--build");

const scrollRule = /\.view--[a-z-]*(?:,\s*\.view--[a-z-]*)*\s*\{[^}]*overflow-y:\s*auto/;
const scrollSelectors = [...CSS.matchAll(/([.][a-z-]+(?:,\s*[.][a-z-]+)*)\s*\{[^}]*overflow-y:\s*auto/g)]
  .map((m) => m[1]);
const covered = scrollSelectors.join(",").includes(".view--build");
check(covered, "no rule gives the Build tab overflow-y: auto, so it is clipped");
check(scrollSelectors.join(",").includes(".view--tune"), "the Tune tab is clipped the same way");
check(scrollRule.test(CSS), "no tab has a scroll rule at all");

// The scroll only works because the ancestors give it a bounded height.
check(/body\s*\{[^}]*overflow:\s*hidden/.test(CSS), "body no longer bounds the page height");
check(/\.view\s*\{[^}]*min-height:\s*0/.test(CSS), ".view no longer has min-height:0, so a scrollable child cannot shrink");

// The cards are a column, not a wall of text edge to edge.
check(/\.view--(?:tune|build)\s*\{[^}]*gap:\s*var\(--st-s-5\)/.test(CSS), "the Build tab cards have no spacing");
check(/\.view--build > \.st-card[^}]*max-width:\s*1240px/.test(CSS), "the Build tab cards run edge to edge");

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("ok: the Build and Tune tabs scroll, and their cards stay in one column");

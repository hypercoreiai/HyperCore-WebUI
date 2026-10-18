// The card order of the Build tab. A build starts from what the release's setup.py
// accepts, so the setup-choices card is the first card on the page; the Engine card is
// the result of a build, so it sits at the bottom, after the build log. The middle cards
// keep the order they had.
//
// Only the cards that are direct children of the section are read: the Build status block
// inside the setup-choices card is nested and must not be counted as a top-level card.
//
//   node tests/test_build_tab_order.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "serve", "web", "index.html"), "utf8");

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

const section = /<section class="view view--build"[^>]*>([\s\S]*?)\n  <\/section>/.exec(HTML);
check(section, "there is no Build tab section to read the order from");
if (failures) process.exit(1);
const body = section[1];

/** Every st-card that is a direct child of the section, in document order. */
function topCards(text) {
  const opens = [];
  let depth = 0;
  for (const m of text.matchAll(/<(\/?)div\b[^>]*>/g)) {
    if (m[1] === "/") { depth -= 1; continue; }
    if (depth === 0 && /class="[^"]*\bst-card\b/.test(m[0])) opens.push({ tag: m[0], at: m.index });
    depth += 1;
  }
  return opens.map((card, i) => {
    const end = i + 1 < opens.length ? opens[i + 1].at : text.length;
    const block = text.slice(card.at, end);
    const classes = ((/class="([^"]*)"/.exec(card.tag) || [])[1] || "").split(/\s+/);
    const title = ((/class="card-title">\s*([^<]*)/.exec(block) || [])[1] || "").trim();
    return { classes, title, block };
  });
}

const cards = topCards(body);
check(cards.length === 5, `the Build tab has ${cards.length} cards, not the 5 it is meant to order`);

const order = cards.map((c) => c.classes.filter((c2) => c2.startsWith("build-")).join("."));
const want = ["build-choices", "build-releases", "build-options", "build-log", "build-status"];
check(order[0] === want[0], `the first card is "${cards[0] && cards[0].title}", the setup choices card must be at the top`);
check(order[order.length - 1] === want[4], `the last card is "${cards[cards.length - 1] && cards[cards.length - 1].title}", the Engine card must be at the bottom`);
check(order.slice(1, 4).join(",") === want.slice(1, 4).join(","), `the cards between them are in the order: ${order.join(", ")}`);

// The Engine card is the one that reports what is installed, not any card with that class.
const engine = cards[cards.length - 1] || { block: "" };
check(/id="build-installed"/.test(engine.block), "the last card is not the one that names the installed engine");
check(cards[0] && /id="build-choices-groups"/.test(cards[0].block), "the first card is not the one that holds the setup choices");

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("ok: the Build tab reads setup choices, releases, options, log, engine");

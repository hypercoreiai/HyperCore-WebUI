// Does the Build tab's command line say "no" when the box says "no"?
//
// DEFAULT_OFF_FLAGS used to leave --vision, --images and --image-native out of the
// command whenever the box held an off value. That was harmless while the release
// defaulted those to off. In strata-v0.2.0-beta --vision is "on by default where the
// model can", so an unspoken off choice was silently overridden by the release.
//
// No browser: the flag block is lifted out of app.js and run against a stub DOM.
//
//   node tests/test_build_off_flags.js
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const APP = fs.readFileSync(path.join(ROOT, "serve", "web", "app.js"), "utf8");
const OPTIONS = JSON.parse(fs.readFileSync(path.join(ROOT, "config", "build_options.json"), "utf8"));

let failures = 0;
function check(cond, what) {
  if (!cond) { failures += 1; console.error("FAIL: " + what); }
}

// ---- the release itself: what does setup.py default? --------------------------
const choices = OPTIONS.setup_choices || [];
const byFlag = Object.fromEntries(choices.map((c) => [c.flag, c]));
const vision = byFlag["--vision"];
check(vision, "config/build_options.json has no --vision choice");
check(vision.default === null, "--vision should have no fixed default: the release decides");
check(/on by default/i.test(vision.help || ""),
      "the --vision help no longer says the release turns it on by default");

// ---- run the flag block against a stub DOM ------------------------------------
const src = APP.slice(APP.indexOf("const CHOICE_ORDER ="), APP.indexOf("function refreshImageControls"));
if (src.length < 400) throw new Error("app.js no longer has the setup-choice flag block");

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

function parseControls(html) {
  // A stand-in for the browser: every control the card rendered, with the value a
  // user would have left in it (the preselected option, or the value attribute).
  const controls = [];
  // choiceField puts data-longest and the --choice-chars sizing style on the select,
  // so the attribute run between data-flag and the closing > must be allowed.
  for (const [, flag, body] of html.matchAll(/<select[^>]*data-flag="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    const selected = body.match(/<option value="([^"]+)" selected>/);
    controls.push({ dataset: { flag }, value: selected ? selected[1] : "" });
  }
  for (const [, , flag, attrs] of html.matchAll(/<input([^>]*)data-flag="([^"]+)"([^>]*)>/g)) {
    controls.push({ dataset: { flag }, value: (attrs.match(/value="([^"]*)"/) || [null, ""])[1] });
  }
  return controls;
}

function argsFor(setupChoices) {
  // choiceField calls the page's esc(); the block on its own has no scope, so the
  // helper is supplied with it.
  const body = "const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/\"/g, '&quot;');\n"
    + src + "\nreturn { choiceField, activeBuildArgs, DEFAULT_OFF_FLAGS };";
  const controls = [];
  const api = new Function("document", body)({ querySelectorAll: () => controls });
  controls.push(...parseControls(setupChoices.map((c) => api.choiceField(c)).join("")));
  return { args: api.activeBuildArgs(), flags: api.DEFAULT_OFF_FLAGS };
}

// 1. everything left at the preselection. A flag the release decides per machine
//    (--vision is "on by default where the model can") has no declared default, so the
//    card leaves it unset and setup.py decides for this box. Preselecting the off value
//    would make every build text-only on a machine that never touched the card.
const { args, flags } = argsFor(choices);
check(flags.has("--experimental-speed-projection"),
      "the experimental projection is no longer treated as a redundant off");
check(!flags.has("--vision"), "--vision is still in DEFAULT_OFF_FLAGS, so an off box is ignored");
check(!flags.has("--images"), "--images is still in DEFAULT_OFF_FLAGS, so an off box is ignored");
check(!flags.has("--image-native"), "--image-native is still in DEFAULT_OFF_FLAGS");

const pair = (list, flag) => {
  const i = list.indexOf(flag);
  return i < 0 ? null : list[i + 1];
};
check(!args.includes("--vision"),
      "the card preselects an off value for --vision, so a build that never touched the " +
      "card gets text-only while the release turns vision on where the model can");
check(!args.includes("--images"),
      "the card preselects an off value for --images, so a build that never touched the " +
      "card gets no image reader while the release turns it on");

// 2. an off value picked by hand is still said out loud, and the dependents of an off
//    parent still stay out
const byHand = choices.map((c) => ({ ...c, default: "no" }));
const args1 = argsFor(byHand).args;
check(args1.includes("--vision") && isOff(pair(args1, "--vision")),
      "the vision box says no but --vision is not on the command line");
check(args1.includes("--images") && isOff(pair(args1, "--images")),
      "the images box says no but --images is not on the command line");
check(!args1.includes("--vision-tokens"), "--vision-tokens is sent while vision is off");
check(!args1.includes("--image-backend"), "--image-backend is sent while images are off");
check(!args1.includes("--image-native"), "--image-native is sent while images are off");
check(!args1.includes("--experimental-speed-projection"),
      "the experimental projection is sent even though the release is off by default");

// 2. images on, native off, experimental off: the off values that are not the
//    release default are said out loud, the one that is stays out
const on = choices.map((c) => {
  if (c.flag === "--vision") return { ...c, default: "yes" };
  if (c.flag === "--images") return { ...c, default: "qwen-image" };
  if (c.flag === "--image-native") return { ...c, default: "off" };
  if (c.flag === "--image-backend") return { ...c, default: "cpu" };
  if (c.flag === "--experimental-speed-projection") return { ...c, default: "off" };
  return c;
});
const args2 = argsFor(on).args;
check(args2.includes("--vision") && pair2(args2, "--vision") === "yes", "vision on is not sent");
check(args2.includes("--images") && pair2(args2, "--images") === "qwen-image", "images on is not sent");
check(args2.includes("--image-native") && pair2(args2, "--image-native") === "off",
      "an explicit image-native off is dropped, so the release's own default wins");
// a box the release does declare a default for is still preselected to it
check(pair(args, "--low-ram") === "auto" && pair(args, "--kv-streaming") === "auto",
      "a box the release declares a default for is no longer preselected to it");
check(!args2.includes("--experimental-speed-projection"),
      "the experimental projection is sent even though the release is off by default");

// 3. an empty box is still no box at all
const blank = choices.map((c) => ({ ...c, default: "" }));
const args3 = argsFor(blank).args;
check(!args3.includes("--vision"), "an unset --vision is sent anyway");
check(!args3.includes("--images"), "an unset --images is sent anyway");

function isOff(v) { return ["no", "none", "off"].includes(String(v || "").toLowerCase()); }
function pair2(args, flag) { return args[args.indexOf(flag) + 1]; }

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("all off-flag checks passed");

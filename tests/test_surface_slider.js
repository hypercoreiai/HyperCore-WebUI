// Regression harness for the Surface slider in the Sampling drawer, run with:
//   node serve/web/test_surface_slider.js
//
// There is no browser here. The palette is read straight out of app.js and run as-is, so this
// test fails when the ramp in app.js changes; the markup and the CSS are checked as text.
"use strict";
const fs = require("fs");

const APP = fs.readFileSync(__dirname + "/app.js", "utf8");
const PAGE = fs.readFileSync(__dirname + "/index.html", "utf8");
const CSS = fs.readFileSync(__dirname + "/app.css", "utf8");
const TOKENS = fs.readFileSync(__dirname + "/tokens.css", "utf8");

function surfaceModule() {
  const start = APP.indexOf("const METAL_STOPS");
  const end = APP.indexOf("function applySurface");
  if (start < 0 || end < 0) throw new Error("the surface palette is not in app.js");
  return new Function(APP.slice(start, end) + "\nreturn {metalColor, metalPalette, METAL_STOPS};")();
}

const { metalColor, metalPalette } = surfaceModule();

// WCAG relative luminance and contrast, so "legible" is a number and not an opinion.
const srgb = (v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
const relLum = (hexStr) => {
  const w = [0.2126, 0.7152, 0.0722];
  return [1, 3, 5].reduce((acc, i, k) => acc + w[k] * srgb(parseInt(hexStr.slice(i, i + 2), 16)), 0);
};
const contrast = (a, b) => {
  const l1 = relLum(a), l2 = relLum(b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
};

let failures = 0;
function check(name, cond, detail = "") {
  if (cond) { console.log(`ok   ${name}`); return; }
  failures++;
  console.log(`FAIL ${name}${detail ? "  " + detail : ""}`);
}

// ---------------------------------------------------------------- the ramp
check("the range starts at pure white", metalPalette(0).bg === "#ffffff", metalPalette(0).bg);
check("the range ends at matte onyx", metalPalette(100).bg === "#14171a", metalPalette(100).bg);
check("the middle of the range is brushed silver", metalPalette(48).bg === "#b6bcc2", metalPalette(48).bg);

let monotone = true, prev = null;
for (let p = 0; p <= 100; p++) {
  const l = relLum(metalPalette(p).bg);
  if (prev !== null && l > prev + 1e-9) { monotone = false; break; }
  prev = l;
}
check("the surface darkens monotonically from left to right", monotone);

let maxStep = 0;
for (let p = 0; p < 100; p++) {
  const a = metalColor(p / 100), b = metalColor((p + 1) / 100);
  maxStep = Math.max(maxStep, Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]));
}
check("one slider step moves the colour by at most 12 in sRGB (no banding)", maxStep <= 12, `max step ${maxStep}`);

// ---------------------------------------------------------------- legibility at every position
let worstInk = Infinity, worstMuted = Infinity;
for (let p = 0; p <= 100; p++) {
  const q = metalPalette(p);
  worstInk = Math.min(worstInk, contrast(q.ink, q.surface));
  worstMuted = Math.min(worstMuted, contrast(q.inkMuted, q.surface));
}
check("ink on panels stays at 4.5:1 or better across the whole range", worstInk >= 4.5, `worst ${worstInk.toFixed(2)}`);
check("muted text stays at 3:1 or better across the whole range", worstMuted >= 3, `worst ${worstMuted.toFixed(2)}`);

// ---------------------------------------------------------------- the sheen
check("the specular highlight peaks in the metallic middle", metalPalette(50).sheen === 1);
check("the specular highlight is gone at both flat ends", metalPalette(0).sheen === 0 && metalPalette(100).sheen === 0);
check("the sheen falls off symmetrically", Math.abs(metalPalette(30).sheen - metalPalette(70).sheen) < 1e-9);

// ---------------------------------------------------------------- bad input
check("a negative position clamps to white", metalPalette(-20).bg === "#ffffff");
check("a position past the end clamps to onyx", metalPalette(240).bg === "#14171a");
check("a non-number falls back to the default", metalPalette("abc").bg === "#ffffff");
check("a position is always inside 0..100", metalPalette(240).pos === 100 && metalPalette(-20).pos === 0);

// ---------------------------------------------------------------- the wiring
check("the drawer has the slider",
  PAGE.includes('<input class="st-slider st-slider--metal" type="range" id="s-surface" min="0" max="100" step="1"'));
check("the slider has a label and a live output",
  PAGE.includes('for="s-surface"') && PAGE.includes('id="o-surface"'));
check("app.js wires the slider and stores the choice",
  APP.includes('$("s-surface").oninput = (e) => setSurface(e.target.value);') && APP.includes('store.set("surface", p.pos)'));
check("the saved choice is restored on load",
  APP.includes('const savedSurface = store.get("surface", 0);') && APP.includes('applySurface(savedSurface);'));
check("the background is painted from the sampled colour with the sheen layers",
  CSS.includes('body[data-surface="metal"] {') && CSS.includes("--st-metal-spec") && CSS.includes("--st-metal-streak"));
check("the slider's track is the metallic range it controls",
  CSS.includes(".st-slider--metal {") && CSS.includes("--st-metal-chrome") && CSS.includes("--st-metal-onyx"));
check("the anchors are in the token file",
  TOKENS.includes("--st-metal-white:#ffffff;") && TOKENS.includes("--st-metal-onyx:#14171a;"));
check("the surface is a browser setting, never sent to the server",
  !APP.slice(APP.indexOf("function sharedDefaults"), APP.indexOf("async function saveShared")).includes("surface"));

console.log(failures ? `\n${failures} check(s) failed` : "\nall surface checks passed");
process.exit(failures ? 1 : 0);

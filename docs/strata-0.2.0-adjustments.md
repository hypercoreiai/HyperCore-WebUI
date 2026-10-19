# Strata 0.2.0-beta vs 0.1.45 — what HyperCore-WebUI needs to adjust

Source of truth: `~/test_webUI/HyperCore-WebUI/tmp/strata-v0.2.0-beta` (git HEAD `3d1fb48`,
"Release notes 0.2.0") and `~/test_webUI/HyperCore-WebUI/tmp/strata-v0.1.45` (HEAD `477ac13`).

## Version facts

| | 0.1.45 tree | 0.2.0-beta tree |
|---|---|---|
| `project(strata VERSION ...)` | `0.1.45` | `0.1.45.3` |
| engine package name | `strata-<os>-x64[-variant].zip` | `strata-engine-<ver>-<os>-<arch>-<backend>-<toolchain>.tar.gz` |
| CMake `option(...)` count | 16 | 17 |
| `FATAL_ERROR` guards in CMakeLists | 11 | 11 (identical) |
| engine CLI flags in `src/program/generate.cpp` | 178 | 178 (identical set; 11 changed lines) |
| side libs built | `libstrata_kq.so` | `libstrata_kq.so` |
| `hypercore-interface/` (injected `engine-src/`) | — | byte-identical to 0.1.45's |

The engine ABI is **0.1.45.3**; "0.2.0" is the app/terminal release line.

## What changed in the build surface

1. **New CMake option** `STRATA_MMQ_TQ_OVERLAY` (default ON): Gyro trellis MMQ prompt kernels
   overlaid onto an upstream llama.cpp's `ggml-cuda` headers, written at configure time by the
   two new files `cmake/strata_ggml_tq_overlay.cmake` and `cmake/ggml_tq_overlay_test.cmake`.
   Only useful with `STRATA_USE_LLAMA_CPP=ON`. `extract_build_options.py` reports it as
   `visible: False`, so the tab will not surface it until the manifest is regenerated.
2. **New internal STRATA_* tokens** (11 added, none removed): `STRATA_MMQ_TQ`,
   `STRATA_MMQ_TQ_OVERLAY`, `STRATA_TQ_OVERLAY_OK`, `STRATA_GDN_CHUNK`, `STRATA_KQ_SPLIT`,
   `STRATA_GRW_V`, `STRATA_GU_SWIGLU`, `STRATA_NO_SUB16_GU`, `STRATA_PF_WMMA`,
   `STRATA_EXPERT_V2K`, `STRATA_FORCE_AVX2`.
3. **setup.py choices**: `--family` gains `gyro`; `--model` gains `Gyro-S`, `Gyro-M`,
   `NVFP4-QAD`; `--vision` help text now says it is on by default (CPU reader under 24 GB
   VRAM, GPU reader from 24 GB).
4. **12 new test targets**: `batch_telemetry_test`, `fusions_parity`, `gdn_chunk_bench`,
   `gdn_chunk_parity`, `hip_prefill_gr_write_bench`, `native_hadamard_test`, `pf_wmma_parity`,
   `pin_budget_test`, `pool_plan_test`, `prefill_plan_test`, `qsa_prompt_attn_sm70_bench`,
   `tq_cpu_parity`.
5. New upstream trees with no HyperCore impact: `apps/` (desktop + terminal), `packaging/`,
   `metal/` and `sycl/` kernel tweaks, docs/README rewrites.

## Adjustments to make in ~/HyperCore-WebUI/

**A. `serve/build_api.py:526 asset_for()` — the one real breakage. [implemented]**
It looks for `strata-windows-x64.zip` / `strata-linux-x64.zip` and falls back to any `.zip`
whose name contains the OS. 0.2.0 publishes `strata-engine-…tar.gz` engine bundles plus
per-platform installers (`Strata-0.2.0-beta-linux-nvidia-cuda13.AppImage`,
`…-windows-cuda12.exe`, `…-update.exe`). As written the ready-made path finds no asset and
silently falls back to "build from source". Fix: accept `.zip` and `.tar.gz`, prefer names
starting `strata-engine-`, select the backend/toolchain that matches `device_defaults()`
(cuda13 vs cuda12 vs hip), and exclude `-update` / installer assets.

**B. `config/build_options.json` was stale. [implemented]** It said `"version": "0.1.45"` and
`"source": ".../tmp/strata-v0.1.45"`. The tab re-reads the manifest from the tag it is asked
to build (`options_for(tag)`), so it self-heals on the next build, but an offline box keeps
using the stale manifest. Regenerate:
`python3 tools/extract_build_options.py tmp/strata-v0.2.0-beta --out config/build_options.json`
after cloning it into the main repo's `tmp/`.

**C. `serve/build_api.py` `update_available` — string compare. [implemented]** It built `"update_available": bool(newest and have and newest.get("version") != have)` — a string compare. `0.1.45` vs
`0.1.45.3` are correctly reported as different, but an installed `0.1.45.3` against a
`0.1.45` tag would report an update that is not one. Compare as tuples:
`tuple(int(p) for p in v.split("."))`. The version regex at `:317` already accepts four-part
versions — no change needed there.

**D. `serve/web/app.js DEFAULT_OFF_FLAGS` — the explicit off was being swallowed. [implemented]**
The card still preselects `no` for `--vision` and `--images`, which is the behaviour this
project wants on a box whose GPU may not carry a vision build. The bug was in how that
choice reached the command line: `DEFAULT_OFF_FLAGS` dropped any flag whose box held an off
value, on the assumption that off is what setup.py does anyway. In 0.1.45 that held. In
0.2.0 `--vision` is "on by default where the model can", so the dropped `no` meant the
release turned vision on behind the box that said `no`. `DEFAULT_OFF_FLAGS` now holds only
`--experimental-speed-projection`, whose help still says "off by default"; `--vision`,
`--images` and `--image-native` are sent with their off value. `IMAGE_DEPENDENT_FLAGS` and
`refreshImageControls()` are unchanged - the dependent controls still grey out when their
parent is off, and an unset box is still not sent. `tests/test_build_off_flags.js` checks
both halves: the off values reach the command, and the redundant one does not.

**E. No change needed.** `tools/extract_build_options.py` is version-agnostic (the new option
shows up automatically); `BACKEND_REQUIREMENTS` in `serve/build_api.py:193` is still accurate
because the guard set is unchanged; `SIDE_LIB` still matches the only side lib;
`normalise_quant()` already maps `Gyro-S` → `gyro_s` and `NVFP4-QAD` → `nvfp4_qad`;
`engine-src/` injects unchanged into the 0.2.0 tree.

**F. Cosmetic.** `serve/build_api.py:193` and `serve/quant_store.py:439` mention "the v0.1.45
tree" in comments; reword to "the clone being built".

## To build against the new release

```
cd ~/HyperCore-WebUI
./tools/build_engine.sh v0.2.0-beta --quant q2_0
```

Baseline test run before this analysis: `pytest tests/test_extract_build_options.py
tests/test_build_api.py tests/test_build_history.py tests/test_quant_store.py -q` → 160 passed.

## What was implemented

| File | Change |
|---|---|
| `serve/build_api.py` | `is_engine_archive()`, `backend_of()`, `cuda_major()` and a re-written `asset_for(release)` that ranks a release's own asset list by OS, backend and CUDA major version, refuses app installers/updaters and GPU bundles on a GPU-less box; `unpack_archive()` reads `zip` and `tar.gz` and refuses a member that would land outside the destination; `version_tuple()` / `is_newer()` replace the string compare in `update_available` |
| `tools/extract_build_options.py` | the `--diff` result is computed before `--out` writes, so the manifest on disk carries its change list |
| `config/build_options.json` | regenerated from `tmp/strata-v0.2.0-beta`: version `0.1.45.3`, 23 CMake options, 47 setup choices, 192 targets, `changes: {from: 0.1.45, to: 0.1.45.3, added_options: [STRATA_MMQ_TQ_OVERLAY]}` |
| `tests/test_build_api.py` | `AssetTests` (toolchain pick, HIP pick, installer/updater refusal, CPU box refuses a GPU bundle, 0.1.45 names still understood, OS not covered, tar.gz install end-to-end, zip-slip refused) and `VersionTests` (numeric compare, no update when the engine is at or above the release) |
| `tests/test_extract_build_options.py` | `ReleaseManifestTests` against the clone in `tmp/`: version, the new option, the gyro/NVFP4-QAD choices, the new targets, the shipped manifest matching the clone, and the change list being written to the file |

`pytest tests/test_build_api.py tests/test_extract_build_options.py -q` → **97 passed**. The 13 failures in `test_version_routes.py`, `test_chat_archive.py`, `test_strata_hook.py` and `test_turn_tail_measure.py` are pre-existing and environmental (they need a running server and an API token); the same 13 fail in the untouched copy.

`node tests/test_build_off_flags.js` → all off-flag checks passed. `node tests/test_build_choices_ui.js` → all setup-choice checks passed; its command-line assertions were moved from "the default build has no image arguments" to "the default build says `--vision no --images no`".

## The Setup choices reached only `Run setup with these`

`Clone and build from source` sent `{tag, options, quant}`: the cmake switches from the
Build options card and a quant name. Nothing from the Setup choices card went with it.

The run config a build writes is a copy of the nearest run config on the box, and when there
is none it is `config/run-example.json`. So on this machine a build started with
`--context 262144 --kv q4_0` in the boxes above the button wrote a config whose args said
`--max-context 131072 --kv int8` - the template's numbers. The model box was ignored too:
with nothing in the quant box the engine went to `engine/` as the default engine rather than
`engine/engine_<quant>/`.

What is now sent and applied:

| Step | Behaviour |
|---|---|
| `serve/web/app.js` | both buttons send `choices: activeBuildArgs()`; `buildTargetQuant()` reads the quant box, and falls back to the model the card names |
| `serve/server.py` | `/build/start` and `/build/fetch-prebuilt` forward `choices` |
| `serve/build_api.py` | `BuildJob.start(..., choices=...)` keeps them on the job, records them in `BUILD.json` and the status, and names the quant the card implies |
| `serve/quant_store.py` | `RUN_CHOICE_FIELDS` maps the flags a run config carries; `apply_build_choices()` writes them into the config the build just wrote and returns the changes |
| the build log | says what was written (`setup choices written to config/run-q2_0.json: --max-context 262144, --kv q4_0`) and what a run config cannot carry (`--vision yes, --image-quant Q8_0`) |

Flags outside the table are left to `setup.py` on purpose: `--vision`/`--images`/`--image-quant`
choose which encoder to build and which mmproj to download, `--data-dir`/`--gguf-dir` say where
files go. A run config written by a build has no part in those, and writing them would put
settings in the file that nothing reads.

`node tests/test_build_source_choices.js` checks the wiring (page → server → build) and runs
the card's own code against a stub DOM to confirm which quant a build is named for and which
settings it holds. `RunChoiceTests` in `tests/test_build_api.py` runs the transfer half of a
build and reads back the config it wrote.

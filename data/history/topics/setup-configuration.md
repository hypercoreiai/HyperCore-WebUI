# Setup, run configs and defaults

Accumulated from 57 transcripts, 163 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**Entry point.** `./setup.sh` (Linux) or `START-HERE.bat` (Windows) is the only thing a user
needs to run. It finds or creates `.venv`, installs the packages, gets or builds the engine,
writes `hypercore-<model>.json` and `run-<model>.sh`/`.bat`, and starts the server. A second
run does not reinstall; it just starts.

**Defaults written by setup (since 2026-10-11).** Every run config setup writes now carries:

- `"host": "0.0.0.0"` — the server answers to the other devices on the network
- `"api_key": "0000"` — a placeholder key, so a default install is not wide open
- `"turn_tail_check": true` — the end-of-turn completion check is on

`run-<model>.sh`/`.bat` passes `--turn-tail-check` accordingly. `--host`, `--api-key` and
`--no-turn-tail-check` override them; a model whose config already sets one keeps it. The
defaults are filled in only where the key is absent (`apply_server_defaults` in `setup.py`),
and a config written by an older setup gets them on its next start.

**Run configs are the source of truth for a model.** `hypercore-<model>.json` holds the engine
path, the engine args, the tokenizer, the log path, the port, the GPU selection, and the
server-side settings (`api_key`, `host`, `cors_origins`, `open_browser`, `reasoning_effort`,
`turn_tail_check`, `sampling.*`). Keys setup does not write are carried over when setup runs
again (`carry_over`), and the previous file is kept as `.bak`.

**Two config paths are checked before anything else:** `--config` on the command line, then
`hypercore-<model>.json` beside the server. `find_prompt_file` follows the same rule for the
agent profiles.


## What is true now

how the thing works, as the chats established it


**./setup.sh** · 6 chats
- `./setup.sh` also runs a calibration (`tools/calibrate.py`) that measures the PC and writes `/home/ai-user0/.config/hypercore/settings.json`.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>
- Both, but the default is generic. `./setup.sh` / `START-HERE.bat` downloads a prebuilt engine and only compiles when that engine cannot run on the machine.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>
- `./setup.sh` (which execs `setup.py`) now writes those three defaults into every run config and run script it generates.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>
- One caveat: `hypercore-q2_0.json` already existed, so I set its three keys by hand rather than re-running setup; a fresh `./setup.sh` produces the same.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-07-09.md</sub>
- `tools/conversation_restore_speed.py` So `./setup.sh` hit `ImportError` inside `calibrate_config()`, which is wrapped in `except Exception` → `[!] the tuning did not finish ...
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>
- `build-cuda/hypercore` is built but not copied to `engine/hypercore` — `engine/` still holds the stale Oct 6 `strata` binaries, so `./setup.sh` from this tree would still fetch a ready-made engine.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**setup.py** · 5 chats
- It is copied there by `setup.py`, in one of two ways. The destination is always `engine_dir()`:
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>
- `mypy --ignore-missing-imports` on `hardware.py history.py bayesian_tune.py search_space.py engine_runner.py run_config.py` → no errors in those files (the 81 errors reported are all in `setup.py`, pre-existing).
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>
- The rename is consistent now: `setup.py` `EXE = "hypercore"`, CMake `add_executable(hypercore …)`, `serve/server.py --engine hypercore` all agree.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>
- `setup.py` no longer trusts "is llama.cpp here" — it checks the six files the two builds actually read before either build runs, and re-unpacks the pinned archive when one is missing: If the archive itself is short of a file, setup names the file and keeps the zip, instead of dying three folders deep in CMake.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>
- On a user's PC, `STRATA_` may already be set by `setup.py`.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**ni_hypercore-q2_0.json** · 4 chats
- A reply that produced zero tokens keeps `finish == "length"`, so the #530 hint at 2787 fires and blames `reasoning_budget_tokens` (24756 in `ni_hypercore-q2_0.json`) — even though nothing was generated.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>
- Ran the live A/B against the loaded model on `:8081` (engine 0.1.40, same defaults as `ni_hypercore-q2_0.json`), sending the identical message five times: Two things confirmed: 1.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- The `[hypercore] the reply reached max tokens while still thinking` line is just the advisory at `serve/server.py:2668`, and `ni_hypercore-q2_0.json` has no `reasoning_budget_tokens` key (checked with `jq`), so nothing caps the thinking by default.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>
- Config filenames — rename `ni_strata-q2_0.json` → `ni_hypercore-q2_0.json` (which also means renaming the profile the live server PID 1552343 is reading), or leave the filenames and fix the run scripts to point at what exists?
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**--turn-tail-check** · 3 chats
- Opt-in via `--turn-tail-check` or `"turn_tail_check": true`; the question text is overridable with `--turn-tail-prompt` / `turn_tail_prompt`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>
- One caveat: the turn-tail check is off by default — the running server was started without `--turn-tail-check`, so it won't fire until you add the flag or set `"turn_tail_check": true` in the profile.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>
- Turn-tail check (`--turn-tail-check`, or `"turn_tail_check": true` in the model's config — on by default for anything `setup` writes): when a turn would end, the server appends one short question asking the model to check its answer against the request.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md · also in hypercore-chat-2026-10-11-08-01.md</sub>

**setup only ever** · 3 chats
- setup.sh: "HyperCore for Linux: the first run installs everything and starts the model; later runs just start it." ✓ Good.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>
- `python3 setup.py --help` now runs, and Step 7's `import calibrate` resolves.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>
- Setup only ever writes `strata-<model>.json` (`setup.py:4920`), so re-running setup won't touch what's live.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**--build** · 2 chats
- Route 2 - compiled on this machine (`--build`, or when no ready-made engine fits) `build_engine()` (`setup.py:2872-2931`) builds into the source tree and then copies: Before copying it checks `binary_engine_version(bdir / EXE)` against `source_version()` and refuses to install a stale binary (`:2915-2916`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>
- The per-machine path (`--build`, or when the fit test fails) - `setup.py:2900-2913`: and it writes a `BUILD.json` with `"source": "local"` and that arch list, so a later run knows the engine is for this PC.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**--serve** · 2 chats
- Nothing else is passed — no extra env vars, and `--serve` is stripped from the deliverable run config (`clean_args`) so the production launcher doesn't carry it.
  <sub>2026-10-15 · strata-chat-2026-10-15-09-53.md</sub>
- Engine commits in between: `ni_hypercore-q2_0.json` vs `max_run_q2_0.json` differ in 12 flags: Also note the tune's metric is not chat throughput: `gen_tok_s = 1000 × 292 / decode_ms` on a bare `--serve` engine with a warm profile-filled cache (`optuna_tune/engine_runner.py:264`).
  <sub>2026-10-14 · strata-chat-2026-10-14-17-09.md</sub>

**./ni_run-q2_0.sh** · 2 chats
- The restart budget (`max_restarts=3`) exists but is unused on this path, which is why your Ctrl+C and manual `./ni_run-q2_0.sh` were needed.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>
- The residual tokens are almost all bare `strata` — `tools/rename_hypercore.sh` only matched `strata-`, `strata_`, `STRATA_`, `strata::`, and `#include "strata/`, so bare words were missed: Hard functional mismatches (these are what break `./setup.sh` / `./ni_run-q2_0.sh`):
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**.env** · 2 chats
- "FREE Frontier Model" — the Assist tab talks to `integrate.api.nvidia.com` with an `API_KEY` from `.env`; the repo does carry an archived catalogue of 38 NIM free-endpoint models (`serve/data/nvidia_free_models/`).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>
- The key is not in the repo. `.env` (124 bytes) contains no `dtn_` string; `.github/workflows/boundary.yml:135` reads `secrets.DAYTONA_API_KEY`, which lives in GitHub's secret store, not in the project tree.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md · also in 2026-10-10-hypercore-chat-2026-10-10-11-52.md</sub>

**.gitignore** · 2 chats
- One stray file worth noting: `.run-q2_0.sh.swp` (a vim swap file) is untracked in the root — `git clean -ndx` or a `.swp` line in `.gitignore` would handle it.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>
- I also copied `/home/ai-user0/HyperCore-WebUI/.env` → `/home/ai-user0/Strata/.env` (the Assist tab needs `ENDPOINT`/`API_KEY`) and added `/.env` to Strata's `.gitignore`, which did not have it.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md · also in strata-chat-2026-10-09-09-50.md</sub>

**/home/ai-user0/strata-data** · 2 chats
- If finish: do I also rename `setup.py`'s engine target, config filenames, and env vars — and should the data folder `/home/ai-user0/Strata-data` become `HyperCore-data`?
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>
- `settings_path()` at `setup.py:2936`: Holds `data_dir` (here `/home/ai-user0/Strata-data`), `installs`, and the per-GPU calibration table — keyed as `GPU|RAM|CPU|model|context|mode`, e.g.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**api_key** · 2 chats
- `setup.py:4920` writes it: On this PC: Keys: `exe`, `args` (the engine flags), `cwd`, `tokenizer`, `model_name`, `log`, `lib_dirs`, `port`, and optionally `gpu`, `gpus_asked`, `layer_split`, `host`, `api_key`, `vision`, `draft_vocab`, `parallel`, `env`, `backend`.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>
- There are three configs for the same model (`strata-`, `ni_`, `yi_`) with different ports (8080 vs 8081) and different `api_key`/`host`/`vision` settings.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**args** · 2 chats
- Found it — it's not the engine, it's a malformed `args` array. Your config had this at the end of `args`: `turn_tail_check` is a server config key, not an engine flag — it was appended bare into the engine's argv.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- The server reads the config at start, so the tab's own note says it plainly: "Saved to the run config; used from the next start of the model." Some keys have guards — `lazy_load` is refused if the config has a `vision` block — and `vram_reserve_mib` is written into the `args` list as `--vram-reserve-mib <n>`, not as a top-level key.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**engine/.previous** · 2 chats
- That is why `engine/.previous` exists on a normal install: it is the rollback target for `setup.py --rollback-engine`.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>
- So `engine/hypercore` is the extracted binary from the release zip, with the previous generation kept in `engine/.previous` for `setup.py --rollback-engine` (`:2495`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>

**line saving runs** · 2 chats
- One correction to the line reference I gave: `engine_dir()` is at `setup.py:884-886`, not 885-887.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>
- [14] the one-line fix: saving the API key now re-runs `loadMcp()` and `loadPrompts(true)`, so a fresh browser stops reporting "no tools".
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-18-57.md</sub>

**ni_run-q2_0.sh** · 2 chats
- Two things to separate: `ni_run-q2_0.sh` itself carries no tag. It just execs the server: The `[strata]` prefix on every output line is printed by the server code it launches — 139 literal occurrences across six modules.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>
- The running server is not the one `ni_run-q2_0.sh` starts. It was launched with `strata-q2_0.json`; `ni_run-q2_0.sh` (owned by `user0`, mode `rwxr--r--`, so I can't edit it) points at `ni_strata-q2_0.json`.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**notice when running** · 2 chats
- But they say it happens when ni_hypercore-q2_0.json is in use.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- I notice when running the ni_run-q2_0.sh script the lines are tagged with Strata, can you find that and change it to "HYPER" .
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md</sub>

**write_setup_config calls every** · 2 chats
- Hmm, calls==3 means: round1 (WORK) → not done → check round 2 (IGNORED) → text empty → `tail_is_done("")` False → then the guard `not (svc.turn_tail_check and checks < ...
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>
- `write_setup_config()` calls it, so every `hypercore-<model>.json` setup writes now has `host: 0.0.0.0`, `api_key: "0000"`, `turn_tail_check: true`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-07-09.md</sub>

**--all**
- `tools/test_clean_script.py` — 7 passed: default tier, dry-run deletes nothing, source/data/run-configs survive, `--all` takes the venvs and `dist`, unknown flag errors, empty project is not a failure, `--scratch` removes tracked logs and keeps `serve/server.py` tracked
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**--api-key**
- It only happens when the server was started with an API key (`--api-key` or `$HYPERCORE_API_KEY`); without a key the page is served byte-for-byte from disk and the artifact is absent.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**--calibrate**
- `/home/ai-user0/HyperCore-WebUI/setup.py`: `calibrate_notice(a, have)` and `calibration_targets(have, model, family)`; `--calibrate` now scopes to `--model`/`--family`, errors on an uninstalled model listing what is installed, and warns when combined with `--setup`, `--check`, or when nothing is installed.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**--clean**
- The docstring claimed `python3 setup.py --clean` uses it, which is false — `setup.py` has no `--clean` flag at all.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**--no-turn-tail-check**
- To check / turn it off: `GET /v1/status` reports `"turn_tail": {"enabled", "max_checks", "custom_prompt"}`; set `"turn_tail_check": false` or `"turn_tail_checks": 0` in the run config, or launch with `--no-turn-tail-check`.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**--prompts**
- `serve/server.py:637` and `:2013` set `prompts_path` to `prompt.json` in the repo root, overridable only by `--prompts` or the run config's `prompts` key.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**--tail-role-token**
- Compared with Strata's running args, `_hypercore-q2_0.json` is missing `--vram-reserve-mib 512`, has `--pcie-frac 0.75` (the default) instead of the measured `0.00`, `--spec-min-p 0.80` instead of `0.90`, and no `--tail-role-token`.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**--think-budget**
- run_config.py: the deliverable's `tuned_by` conditions should include the thinking numbers, and if `--think-budget` is set, the run config should carry `thinking_budget` so the server actually uses it.
  <sub>2026-10-15 · strata-chat-2026-10-15-18-43.md</sub>

**--think-end**
- One caveat from the current state of the tuner: the default think-end id is the pack's `<|im_start|>` (151667), so a real `build/strata` run needs `--think-end` set to whatever id that pack's tokenizer closes reasoning with; the real-engine smoke run couldn't boot because the GPU was busy.
  <sub>2026-10-15 · strata-chat-2026-10-15-19-50.md · also in strata-chat-2026-10-15-19-50.md</sub>

**--yes**
- Every PC is different: `START-HERE.bat --calibrate` (Linux: `./setup.sh --calibrate`) measures a … [universal] /home/ai-user0/_Strata/docs/INSTALL.md … With `--yes` setup takes the recommended answer to every question.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**-dcmake_cuda_architectures**
- A card outside that set triggers the local compile path, which is per-machine: `-DCMAKE_CUDA_ARCHITECTURES` from the actual GPUs (`setup.py:2891, 2910`) and `-march=native` for the CPU (`CMakeLists.txt:1228`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**-dcmake_make_program**
- Fix — `setup.py` now reads the generator the directory already has: `-DCMAKE_MAKE_PROGRAM` is only passed when the generator is actually Ninja.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**./nih_run-q2_0.sh**
- The five-repeat measurement against the restarted engine is still outstanding — run `./nih_run-q2_0.sh` and I'll re-run the A/B and compare `completion_tokens` turn over turn.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**.bb_cmake_configure.txt**
- The genuine repo dirt is separate and not removable by `git clean`, because it's tracked: 27 scratch files at the root — `.review_.txt` (13), `.mcp_run_all.txt`, `.bb_cmake_configure.txt`, `_kimi_ab.py`, `mcp.yaml.orig`, `mcp.yaml.orig2`, `ni_hypercore-q2_0.json.bak`, `.full_suite_after.txt`.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**.env.example**
- Root is clean: only README, LICENSE, `.gitignore`, `.env.example`, CHANGELOG, `setup.sh`/`run.sh`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**.py**
- I widened it to `_.py` so the project's own tool catches one-off root scripts in future: Run configs (`_hypercore-q2_0.json`, `_run-q2_0.sh`) are not `.py`, so they're untouched.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**.pyenv/**
- Default excludes for `venv/`, `.pyenv/`, `site-packages`, `__pycache__`, plus an `exclude` glob, and an explicit "N files skipped" line so partial coverage is visible.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/config**
- They are the same codebase — identical run-config format (`exe`, `args`, `cwd`, `mmproj`, `model`), identical `--engine strata --config <json>` invocation, identical `/config` settings view, identical engine IPC.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/health**
- Live state: pid 793996 on port 8081, started from `run-q2_0.sh` with `--host 0.0.0.0 --turn-tail-check`; `/health` reports `api_key: true`, and a streamed request emitted `tail_check {"result":"continue","checks":1,"max_checks":3}`, so the turn-tail check is active.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**/home/ai-user0/.cache/strata/server.pid**
- Starts detached with `setsid nohup sh <variant>_run-q2_0.sh &`, writes the PID to `/home/ai-user0/.cache/strata/server.pid`, then polls `http://127.0.0.1:<port>/` until it answers, reporting progress every 10 s (a cold model load takes a minute or two, so `READY_S` defaults to 120).
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**/home/ai-user0/.config/hypercore/settings.json**
- Separately, `setup.py` does write per-PC tuning into `/home/ai-user0/.config/hypercore/settings.json` (this machine's entry is `RTX 4090|i9-14900KF|q2_0|262144`, dated 2026-10-05).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**/home/ai-user0/hypercore-webui/debug_mmvq_il.sh**
- Prompt line to type (or pipe) — "Complete the open tasks", encoded with the pack tokenizer: I put that in `/home/ai-user0/HyperCore-WebUI/debug_mmvq_il.sh` (executable) so the argument list can never drift from `ni_hypercore-q2_0.json` — it reads the args straight out of the config: One-shot variant (no serve loop) if you just want one window:
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md · also in strata-chat-2026-10-13-21-31.md</sub>

**/home/ai-user0/hypercore-webui/ni_run-q2_0.sh**
- `/home/ai-user0/HyperCore-WebUI/ni_run-q2_0.sh` is byte-identical to the Strata one and runs the same server, so it now prints `[HYPER]` too.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md</sub>

**/home/ai-user0/hypercore-webui/setup.py**
- Both symptoms trace to the supervisor refactor, not to a missing feature. `grep -cE "calibrat"` → 24 in both `/home/ai-user0/Strata/setup.py` and `/home/ai-user0/HyperCore-WebUI/setup.py`, and the offer block is identical apart from the name: The prompt is emitted after Step 4 (engine + image encoder).
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md · also in 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**/home/ai-user0/strata/ni_strata-q2_0.json**
- The text-only config exists here: `/home/ai-user0/Strata/ni_strata-q2_0.json` has no `"vision"` section, and `ni_run-q2_0.sh` starts it on the same port 8081.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-36.md</sub>

**/v1/models**
- The `/v1/models` endpoint works without an API key and returns 7639 bytes — that's a list of models.
  <sub>2026-10-09 · strata-chat-2026-10-09-09-50.md</sub>

**/v1/start**
- Tune — a web front end for `max_tune.sh`: objective, trials, sampler, bounds, pinned dimensions; live trial table exactly as the CLI writes it; export the `max_run_` pair; "Apply" writes the winner into the run config and restarts the engine via the existing `/v1/stop` + `/v1/start`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**_hypercore_run-q2_0.sh**
- Next step needs your go-ahead: stop the Strata instance (pid 178995) so the 4090 is free, then re-run `_hypercore_run-q2_0.sh` to see the full READY handshake — or, if you'd rather keep Strata up, I can run the calibration path first so the config gets a VRAM reserve.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**archs**
- The generic path (default) The fit test is `engine_archs()` / `engine_runs_on()` (`setup.py:990-1014`), which reads `BUILD.json`'s `archs` and `ptx` flags: `get_prebuilt()` (`setup.py:2531-2536`) applies it per card and returns `None` if any card has no code: "the installed engine is built for …; your GPU is …: compiling instead".
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**assist_defaults**
- `serve/assist.py` now has its own defaults, documented as deliberately independent of the run config: `converse()` takes `max_tokens=None` and falls back to `ASSIST_DEFAULTS`, and `serve/web/app.js` carries the same note at the call site so nobody wires the two together later.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**ast**
- `best_model.py` reads setup.py's `MODELS` table with `ast` — no download, no GPU, and no duplicated numbers.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**before launching server**
- Before launching the server, you must elevate your terminal environment's memlock ceiling: ulimit -l ulimit -l unlimited ./install.sh ------------------------------
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>

**bench/**
- Scale of the unfinished rename: 170 `strata` references in `setup.py`, 45 in `src/`, 39 in `tests/` (32 files `#include "strata/…"` while the headers are in `include/hypercore/` — the C++ tests can't build), 138 in `bench/`, 58 in `serve/`, 73 in `tools/`, plus `README.md` deleted from HEAD.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**best_model**
- `tools/test_best_model.py` was dead: it imported `best_model` from the repo root, but the module lives in `tune/best_model.py`, and that module read `tune/setup.py`, which does not exist (`setup.py` is at the root).
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**build**
- Build-directory consistency: OK. `build-hip`, `build-sycl`, `build-halo`, `build-cuda12` each have a matching configure line in their own page; the main path uses `build`, as `install.sh` and CI do.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**build-cuda-check/**
- Verified: `build/`, `build-shared/`, `build-cuda/`, `build-cuda-check/` are all referenced (`install.sh`, `tools/backend_parity.py`, CI, `serve/server.py`).
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**build/hypercore-worker**
- `tools/calibrate.py hypercore-q2_0.json` exits with `WorkerUnreachable: worker exited during startup (1)`, and running `build/hypercore-worker` standalone gives `engine init failed (-5)` because the live server (pid 834276, port 8081) holds the GPU.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**calibration already exists**
- If a calibration already exists for this PC/model, `./setup.sh --setup` applies it and does not re-measure; `./setup.sh --calibrate` forces a fresh sweep.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**clients claude code**
- API clients (omp, Claude Code, OpenAI and Anthropic SDKs) see the API exactly as before" — `/home/ai-user0/Strata/docs/DETAILS.md:563-565`, and "The tools can only be used from the chat page itself (a request with another site's Origin or without a JSON content type is refused); if Strata is reachable from other devices, set an API key" (`:569-571`).
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**cmakelists.txt**
- It is not referenced by the build — no `third_party/llama.cpp` path appears in `CMakeLists.txt`, `setup.py`, or `setup.sh`.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**config/run-q2_0.json**
- One thing you have to do The server running now (PID 399364, port 8081, `config/run-q2_0.json`) is still the pre-fix code: I queried it and it still returns `latest` as a single object.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**docs**
- The running hub (pid 2856630) still holds the old env — `docs`, `scrape` and `terminal__process` only come live after `run-q2_0.sh` is restarted.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**done**
- `serve/server.py:3193-3194` — when a turn would end, the server appends a new user-role message and re-runs the turn: and it does that again until the model answers `done`, up to `turn_tail_checks` times (`TAIL_CHECK_MAX = 3`, hard-capped 0–10, `server.py:5223-5231`).
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**engine/**
- Engine binary. `engine/` still holds the stale `strata` / `strata-vision` binaries from Oct 6, while `setup.py` now looks for `engine/hypercore`.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**existing installs switch**
- Existing installs switch to `--prefill auto` the next time START-HERE / setup.sh starts them.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**externally-managed-environment**
- `python3 setup.py` (system interpreter) stops at Step 3 with `externally-managed-environment` (PEP 668).
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**extractor works cmake**
- The extractor works (19 CMake options, 39 setup.py flags).
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**fixing worker count**
- So: fixing the worker count is a speed control for a hybrid or bandwidth-bound CPU (`docs/DETAILS.md:451`, and the 2-socket Xeon tip at `setup.py:651`), not a memory control.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**hypercore-q2_0.json**
- `hypercore-q2_0.json` still carries the debug flags: Every request re-reads its whole prompt from token 0: That's 13.4 s of prefill before the first token, on a 53k-token conversation.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-44.md</sub>

**hypercore_tokenizer.py**
- Stale references that will break at runtime. `setup.py:4810` calls `str(ROOT / "tools" / "strata_tokenizer.py")` — that file is now `hypercore_tokenizer.py`.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md · also in 2026-10-10-strata-chat-2026-10-10-03-30.md</sub>

**logs/strata-hook.log**
- 2.3 `POST /config/strata-hook`. New `serve/strata_hook.py`: runs the upstream `setup.py` as a detached `Popen` with stdout and stderr written to `logs/strata-hook.log`, returns immediately, and exposes `GET /config/strata-hook?offset=N` (byte-offset tail) and `POST /config/strata-hook/stop`.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>

**mcp.yaml**
- Hub defaults (overridable in `mcp.yaml` or the run config's `"mcp"` block): `mcp.yaml` wins a shared name over the run config.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-16.md</sub>

**measures output speed**
- It measures the output speed … [universal] /home/ai-user0/_Strata/docs/DETAILS.md … Setup offers to measure them on your PC after an install; `START-HERE.bat --calibrate` (Linux: `./setup.sh --calibrate`) does it any time.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md · also in hypercore-chat-2026-10-13-08-16.md, strata-chat-2026-10-03-14-03.md</sub>

**ni_run-q2_0.json**
- Your live archive still uses `ni_run-q2_0.json` configs — do you want the personal variants kept as an example, or should the live system be migrated to the standard names?
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**ni_strata-q2_0.json**
- The UI is unauthorised. The server's key comes from `ni_strata-q2_0.json` → `"api_key": "0000"`, and `headers()` only sends it if you typed it into the API key field.
  <sub>2026-10-09 · strata-chat-2026-10-09-12-32.md</sub>

**nih_run-q2_0.sh**
- The engine those runs used was not the one with my fix: `nih_run-q2_0.sh` runs `exe: engine/hypercore`.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**optuna_tune/run_config.py**
- `optuna_tune/run_config.py` (new, 238 lines) builds the deliverable pair in the format `setup.py` writes:
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>

**pack/full**
- `tools/bench_flood.py --model` explicitly refuses to run when `pack/full` is absent, and `serve/server.py:4421` defaults its tokenizer to `pack/full/tokenizer`.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**path same shape**
- The HIP path is the same shape: `shutil.copy2(ROOT / "build-hip" / EXE, eng / EXE)` (`setup.py:2402`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>

**portainer**
- For reference, the API key in `strata-q2_0.json` is `api_key: "0000"`, and AnythingLLM is not running (`docker ps` shows only `portainer`), so the only web UI on this box is Strata on `:8081`.
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**ruff**
- `ruff` on `setup.py` is back at the HEAD baseline (55 issues, no new ones); the new files are clean.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**run-q2_0.json**
- Your `run-q2_0.json` is fine — it just can't be loaded twice.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**scripts _hypercore_run q2_0**
- The run scripts (_hypercore_run-q2_0.sh, run_hypercore_production.sh) are generated/local artifacts — .gitignore ignores run-.sh and hypercore-.json but not run_hypercore_production.sh or _hypercore_run-q2_0.sh.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**scrub replaces keys**
- `scrub()` replaces API keys, emails and handles with `[key]`, `[email]`, `[handle]` before anything is read.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**search_space.py**
- What it does now (`search_space.py`, `data_roots(root)`), in the same order `setup.py` uses: 1.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>

**serve/server.py**
- Strata's backend reads `strata_mcp` (10 occurrences in `serve/server.py`) and its env vars are `STRATA_API_KEY`, `STRATA_ALLOWED_HOSTS`, etc.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**settings.json**
- That needs a machine where the CMake configure step is allowed: rebuild, then `./setup.sh --calibrate` to write the pcie-frac and adapt sweep into `settings.json`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-14-18.md</sub>

**start/.json**
- `turn_tail_check` is absent (null) in ni_hypercore-q2_0.json but present in the `start/.json` configs.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**tools/calibrate.py**
- "Tune" is not a separate subsystem: it is a measurement harness (`tools/calibrate.py`) that writes three of those CLI flags into the run config.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-03.md</sub>

**turn_tail_checks**
- A yes/confirmation ends the turn; a no, an unanswered question, or more work keeps it open and the model continues — up to `turn_tail_checks` (default 3, capped 10) per turn.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md · also in hypercore-chat-2026-10-11-08-01.md</sub>

**universal home user0**
- [universal] /home/ai-user0/Strata/docs/MODELS.md … Files another model shares are not downloaded again (the Coder uses the original's shard 2 and vision encoder).
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-36.md</sub>

**vision.cuda_device**
- Your options right now: `./setup.sh --vision cpu` (encoder on CPU, works — verified), or set `vision.cuda_device` to a spare card, or stop the Strata instance (pid 178995) so the 4090 is free.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**wait actually ni_hypercore**
- Wait — actually in ni_hypercore-q2_0.json, `--pcie-frac 0.00` IS set.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**window.hypercore_api_key**
- A server started with a key now injects `window.HYPERCORE_API_KEY` into the page it serves, so the chat UI works without typing the key into Settings (`serve/test_server.py::WebApp` covers it).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**yi_run-q2_0.sh**
- `yi_run-q2_0.sh` doesn't exist yet. The script handles that cleanly (reports it, starts nothing).
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**socket sanitation supervisor** · 2 chats
- Socket sanitation: supervisor evicts stale socket, pid-private paths, cleanup, tests added to install.sh.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>
- The socket issue is fixed and now covered by install.sh.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>

**what generate restart** · 2 chats
- What is verifiable in the code is the fit test itself, `engine_runs_on()` (`setup.py:1010-1014`): So the generic engine covers a fixed set of generations plus PTX for anything newer than the highest built arch (`docs/OLDER_GPUS.md:64` describes the CUDA 12 variant as "code for sm_60 to sm_89, plus PTX").
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>
- what if you generate a restart script that stops the current /home/ai-user0/Strata/ni_run-q2_0.sh (no image) or /home/ai-user0/Strata/yi_run-q2_0.sh (yes image to be added) .
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-07.md</sub>

**--cntx**
- `search_space.engine_args()` — fixed base block, then one flag per swept dimension: Mapping of the CSV columns to engine flags: `--max-context` is `--cntx` (now 32768 by default); `--max-new` is `--max-new` (512).
  <sub>2026-10-15 · strata-chat-2026-10-15-09-53.md</sub>

**--max-new**
- `optuna_tune/winner_bench.py` (new), one command in the shape of `/home/ai-user0/Strata/setup.sh --setup` / `./bench.sh`: reads the deliverable `max_run_<model>.json`, checks the card with nvidia-smi, boots the engine with that config's own args (`--serve`/`--max-new` added back), and reports per-boot lines plus medians with the off-card share, then the delta against what the sweep recorded.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md</sub>

**./run-q2_0.sh**
- There is no `./run-q2_0.sh` in the fork — the script setup.py generates is `run-<model>.sh`, but this checkout has an underscore-prefixed, untracked copy: `_hypercore_run-q2_0.sh` (config `_hypercore-q2_0.json`, port 8080).
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**.gitignore**
- Also added `_run-.sh` / `_hypercore-.json` to `.gitignore` — those are machine-local run configs like `run-.sh`, including your `_run-q2_0.sh` with `--host 0.0.0.0`
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**/home/ai-user0/hypercore-webui/qwen_config_q2_0.json**
- `/home/ai-user0/HyperCore-WebUI/qwen_config_q2_0.json` — the run config, built through the project's own `run_config.build_run_config` from `hypercore-q2_0.json`, so the model's sampling/reasoning/alias keys are preserved and only `args` is replaced.
  <sub>2026-10-17 · strata-chat-2026-10-17-14-18.md</sub>

**api_key**
- The "FREE" claim — qualified it and added the missing Assist bullet: I verified those details against the code rather than guessing: `serve/assist.py` reads `API_KEY`/`NVIDIA_API_KEY` and raises if absent; `serve/assist_fast_models.json` pins 14 models with `max_lag_s: 1.0`; `serve/web/app.js` has the "Show every model" toggle and the "Check lag" probe.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-31.md</sub>

**app.js**
- The two UI items from the investigation are still open and unfixed: `app.js` re-sends `RESUME_PROMPT` even when the last turn is already that prompt, and `turn_tail_check: true` appends `TAIL_CHECK_PROMPT` up to 3× per turn.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-44.md</sub>

**bench.sh**
- The calibrate/benchmark work is in `setup.py`, not `setup.sh`, so the option had to be added to `black_box`'s `setup.py` and given a `bench.sh` entry point next to `setup.sh`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**build-shared/**
- `serve/server.py` spawned the worker from one hard-coded path: `hypercore-q2_0.json` has no `worker` key, and `build-shared/` is gone (`./clean.sh --builds` removed it — the state file recorded that).
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**build.json**
- CACHE ...)` and `setup.py` `add_argument(...)` choices → writes `config/build_options.json` and diffs it against the previous manifest so the tab can show what changed between releases → cmake/ninja (MSVC on Windows) → copy binary + write `BUILD.json` → delete `tmp/` → restart engine.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**config/run-q2_0.json**
- Housekeeping — `tools/hypercore_tokenizer.py` was missing (the server imports it for the pack tokenizer); the test stub config moved to `tests/`; `config/strata-q2_0.json` renamed to `config/run-q2_0.json` (the plan's convention, and it's gitignored); `engine/strata` + `engine/strata-vision` + `engine/BUILD.json` adopted from your existing build so the project is self-contained.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**destination fixed engine_dir**
- The destination is fixed by `engine_dir()` (`setup.py:885-887`): and the file name by `EXE = "hypercore.exe" if WIN else "hypercore"` (`setup.py:374`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>

**diff home user0**
- `diff -u /home/ai-user0/HyperCore-WebUI/setup.py Strata_1.43/setup.py` is 3418 lines (1211 added, 625 removed), and most of it is the rename, not behaviour.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**exe**
- Guard added to `tune/max_tune.sh` `config_is_readable()` now checks the run config parses and has an `exe` key before the 15-30 minute calibration starts: exit 2, no GPU touched.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-13-18.md</sub>

**extract changes script**
- The extract-changes script — I read it as "parse the new clone's CMake + setup.py options, diff against the previous manifest, show what changed in this release." Correct?
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**hypercore-q2_0.json**
- What survives unchanged: the gate behaviour, the per-engine decisions, the stale `HYPERCORE_ROUTE_TAIL_SKIP=0` in `hypercore-q2_0.json` (confirmed present, and `grep -c "tail skip is on"` on the current log returns 0, so it is still in effect despite the 15:38 run preferring the default), and the recommendation to raise `SWEEP_ROUNDS` or `MIN_GAIN` in `tools/calibrate.py`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>

**install.sh**
- This is covered by: Result: I also added `serve.test_socket_hygiene` to `install.sh`’s supervisor stage so the boundary test runs automatically.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>

**ni_run-q2_0.sh**
- Naming decision. `ni_run-q2_0.sh` is gone by design (T3 replaced it with `restart.sh` + `hypercore_<model>.sh`).
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**rebuild triggers engine**
- Rebuild triggers. The engine is recompiled when the source hash changes, or when a GPU is added whose arch is not in the existing fatbin (`new_arch = local and not set(archs) <= built`, `setup.py:2892`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**setup.py**
- What changed in `setup.py` `SETUP_KEYS` now includes `turn_tail_check`, so a setup rerun does not drop it.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**setup_keys**
- `turn_tail_check` was added to `SETUP_KEYS` so a rerun doesn't drop it.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-07-09.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**hypercore-q2_0.json**
- `hypercore-q2_0.json` was written with `--vram-reserve-mib 700`, from: The BF16 mmproj alone is 907,524,736 B = 865 MiB of tensors, plus the encoder's own CUDA context and its work buffers.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**ranking puts fully**
- Ranking puts a fully resident pack above one that streams its experts from the SSD, which is the distinction setup.py itself documents (UD-Q4_K_XL measures 7–8.5 tokens/s on a 64 GB PC).
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**setup calibrate works**
- `./setup.sh --calibrate` works — the repo's own state record shows a real run on this box (`--pcie-frac 0.75, --spec-min-p 0.30, --pool-workers 12, 2362 tok/s`).
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**setup.py**
- This decides whether `setup.py` needs the ~70–190 GB download logic or just a path to an existing pack.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**strata_models**
- Consider the model setup.py recommends. `strata_models` says Coder IQ1_M at 128K is the fastest that fits here (23.4 GB arena → fully resident), while Q2_0 "fits in the low-RAM mode (slower: the GPU holds part of the experts)".
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

## From outside this project

figures a chat took from a source we did not run here; check the source before repeating one


**tools/make_release.py** · 2 chats
- `tools/make_release.py`, named at `setup.py:168` as the builder of that asset, is not in this checkout.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md · also in hypercore-chat-2026-10-15-05-16.md</sub>
- The published asset is not reachable from this environment: `curl -sIL https://github.com/hypercoreiai/HyperCore/releases/latest/download/hypercore-linux-x64.zip` returns `HTTP/2 404`, and `tools/make_release.py` (referenced at `setup.py:167`) is not in this checkout.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

## Gotchas

things that bit once and will bite again


**/home/ai-user0/hypercore-webui**
- And in `/home/ai-user0/HyperCore-WebUI` it is on in every run config: The Strata config that's running now has no such key, so the tail check is off here (`svc.turn_tail_check = False` unless the config or the flag sets it).
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**/home/ai-user0/strata**
- Do not move `/home/ai-user0/Strata`. It is the live system (PID 1552343, port 8081, HTTP 200), and `ni_run-q2_0.sh` hardcodes `cd "/home/ai-user0/Strata"`.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**api_key**
- The interesting configuration is in `"args"`, which the About tab never shows: Plus `port`, `host`, `api_key`, `gpu`, `gpus_asked`, `lib_dirs` — deliberately left out of the editor ("the network and security keys … are kept as it is").
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**boundary.yml**
- `boundary.yml` has never run on an actual GitHub runner — it needs `DAYTONA_API_KEY` as a secret.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

**config/**
- a bad name now lists what is in `config/` instead of just saying "not readable" Engine resolution — implemented the order the README promised but the code never had: run config `"exe"` → `engine/strata` in this project → `$STRATA_ENGINE`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**daytona_api_key**
- One caveat: the CI cross-toolchain stage reports SKIP unless `DAYTONA_API_KEY` is set on the repo.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**debug_mmvq_il.sh**
- The `--prefill 0` control never ran. `debug_mmvq_il.sh` built its engine arguments only from `ni_hypercore-q2_0.json` and never read `$@`, so `--prefill 0` was dropped silently.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**setup.sh**
- `diff -u Strata_1.42/setup.sh Strata_1.43/setup.sh` is empty: `setup.sh` in both is a 56-line venv bootstrap that ends with `exec .venv/bin/python setup.py "$@"`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

## Still open

not done, deferred, or waiting on a decision


**daytona_api_key**
- Not yet true, and I won't claim it is: the workflow has never run — it needs `DAYTONA_API_KEY` as a secret and a runner with cmake.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

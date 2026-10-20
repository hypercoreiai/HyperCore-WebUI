# HyperCore-WebUI - Plan v2 (version 1.9.1)

Repo: hypercoreiai/HyperCore-WebUI, tagged `v1.9.1`.
UI source: `/home/ai-user0/HyperCore-WebUI/serve/`. Engine: the stock Strata engine
(Niko1221/Strata), built or downloaded by this project. Brand: HyperCore - Powered by Strata.

Merged 2026-10-19 from four sources:

| Source | What it was | What it became here |
|---|---|---|
| `PLAN.md` | Framework specification v3, the compatibility contract | sections 1-12, kept as the contract |
| `PLAN_CHECKLIST1.md` | Robustness checklist, 14 items, with the test that proves each | section 13, deduplicated |
| `PLAN_CHECKLIST2.md` | The same robustness checklist restated, 15 items, ~70% overlap | folded into section 13 |
| the 10-point review of 2026-10-19 | code-quality findings, recorded in `data/memory/hypercore-webui-review-10-improvements.md` | section 14 |
| `.mcp_test_state.json` loop 70 | what has actually been built since v3 was approved | sections 15 and 16 |

Where the spec and the code disagree, the code wins. Each claim below is either a path in
the tree or a run that was executed; the two are separated.

---

## 1. The compatibility contract

The project is the front end plus its own Python server. It ships no engine source. The
only native artefact needed at runtime is `engine/strata`.

1. **Engine IPC** - the server speaks Strata's stdin/stdout protocol exactly:
   `GEN <max_new> <ids>`, token ids back, `READY`, `INFO`, `DONE`, `STOP`, `QUIT`, `SWAIT`,
   `SERR`, `BT`/`BDONE`. Verified against Strata `src/program/measure.cpp`-adjacent
   `src/program/generate.cpp` (0.1.45).
2. **Engine location** - resolved in this order: `--engine` flag -> run-config `engine_url`
   -> `--engine-url` / `$HYPERCORE_ENGINE_URL` -> run-config `exe` key -> `engine/strata` in
   this project -> `STRATA_ENGINE`. No hard-coded absolute paths.
3. **Feature gating** - anything HyperCore-only (`HYPERCORE_MMVQ_IL`, expert-residency
   counters) is read from the engine INFO line and shown only if present. A stock Strata
   engine never breaks the page.
4. **API surface** - OpenAI and Anthropic endpoints unchanged.
5. **`tmp/` is scratch** - deleting `tmp/` must not affect anything at runtime. It is
   gitignored and never read by the server, only by the Build tab. Verified by
   `tests/test_run_scripts.py` and `tests/test_build_api.py`.

### 1.1 One engine per machine (attach mode)

A Strata engine is tens of GB, so a second WebUI on the same PC does not load a second copy:

- `run.sh` checks the port before starting; if the WebUI is already up there it opens that
  page instead of starting another engine.
- `--engine-url`, `"engine_url"` in a run config, or `$HYPERCORE_ENGINE_URL` attaches to an
  engine another server already loaded, over its OpenAI API. `serve/attached_engine.py`
  implements the same `generate(prompt_ids, max_new, sampling, cancel) -> token ids`
  boundary as the direct engine, so no tab knows the difference.
- Attaching is a **read relationship, not an ownership one**: stop, unload and VRAM resize
  are asks to the engine's own server, and a local server restart must not tear down a
  shared host engine. `About` names the address the engine lives at.
  Test: `tests/test_attached_engine.py`.

---

## 2. Repository layout (nothing loose in the root)

```
HyperCore-WebUI/
  README.md  LICENSE  .gitignore  .env.example  CHANGELOG.md  PLAN_v2.md
  setup.sh / setup.bat / setup.py        run.sh / run.bat
  serve/
    server.py                    HTTP, API, engine supervisor
    frontend.py responses.py ipc_parser.py engine_bridge.py engine_supervisor.py
    direct_engine.py attached_engine.py offline_engine.py
    assist.py mcp.py prompts.py rag.py doc_text.py structured.py telemetry.py
    runconfig.py chat_archive.py http_auth.py http_origin.py
    api_lanes.py config_payload.py engine_args.py engine_errors.py narration.py
    proc.py tokenizing.py vision.py          (split out of server.py, review item 1)
    strata_hook.py quant_store.py            (setup hook, per-quant build store)
    tune_api.py build_api.py
    web/  index.html app.js app.css components.css tokens.css sprite.svg
          monitor.html monitor.js fonts/
    tests/                       the MCP hub's own contract suite (collected by pytest.ini)
  optuna_tune/                  bayesian_tune.py search_space.py engine_runner.py hardware.py
                                history.py run_config.py prompt_transfer.py winner_bench.py
                                best_prefill.py max_tune.sh stub_engine.py fixtures/
                                history/ (seed-* priors committed)   results/ (gitignored)
  tools/                        extract_build_options.py import_tune_history.py
                                build_engine.sh / .bat  build_history.py  run_tests.sh
  engine/                       strata, strata-vision, hypercore-worker, BUILD.json
                                versions/<quant>/ + VERSIONS.json     (binaries gitignored)
  engine-src/                   interface/ (worker_main.cpp, engine_process.*, hypercore_bridge.cpp,
                                cli_gateway.cpp, ipc_frame.h, glibc_compat.c)
                                include/hypercore/hypercore_api.h, CMakeLists.txt
  config/                       run configs, mcp.yaml, shared settings, build manifest
  prompts/                      prompt.json and the agent profiles
  data/                         expert profiles, tokenizer, draft vocab, media, history
  logs/  tmp/                   gitignored
  tests/                        27 python suites, 19 node suites, tests/dom_stub.js
  docs/
```

Run configs are `config/run-<model>.json` + `config/run-<model>.sh`; logs go to `logs/`.

---

## 3. Tabs: Chat - Assist - Monitor - Tune - Build - About

Header brand: **HyperCore**, sub-line *Powered by Strata*. Status pill, theme toggle,
surface slider and two-click Shut Down stay as they are.

- **Chat** - unchanged: streaming answers, thinking shown as produced, agent-profile
  dropdown from `prompts/prompt.json` (`GET /prompts`), attachments, image input through the
  vision engine, sampling drawer, token-count guard, MCP tool calls. The ack goes out before
  the run, and the supervisor reads a reply that arrives before it.
- **Assist** - unchanged; `serve/assist.py` reads `ENDPOINT`/`API_KEY` from `.env`, the
  NVIDIA-specific wording is generalised, and the lag-probe cache is keyed by endpoint.
- **Monitor** - unchanged: model-state card, sparklines, context-fill gauge, request table,
  conversation cache, MCP switches, external-assistant log. The card reports
  `worker: not built` rather than failing.
- **Tune** - front end for `optuna_tune/max_tune.sh` as a background subprocess. Objective,
  model, context, trials, seed, repeat, sampler, mode, bounds, and pinning of any dimension.
  The engine is unloaded before a sweep; the page carries the "do not close this page" note;
  live view shows the exact row set the CLI writes; on completion the `winner_bench` ladder
  (512/1024/2048/4096/8192/16383 tokens, medians over `--repeat` boots) is presented; export
  writes the run pair, the CSV and the record JSON; Apply writes the winner into the active
  run config and restarts. Endpoints: `GET /tune/params`, `POST /tune/run`,
  `GET /tune/status`, `GET /tune/results`, `POST /tune/stop`, `POST /tune/apply`,
  `GET /tune/export`. Priors are keyed to the machine; a differing hardware fingerprint is
  listed but only used when the operator ticks "use priors from other hardware".
- **Build** - see section 4, plus the Model version dropdown in the header (section 15).
- **About** - unchanged, plus provenance: engine version, source hash from
  `engine/BUILD.json`, the upstream release the engine came from, this PC, API addresses,
  API key field, dark toggle, model settings form.

---

## 4. Build / Update tab

**A. Ready-made engine** (fast, default): query
`https://api.github.com/repos/Niko1221/Strata/releases` for the latest tag, compare with the
installed version (`engine/BUILD.json` `version`, falling back to the `engine=X.Y.Z` string
compiled into the binary), download the matching release asset for this OS, verify size and
SHA-256 against the published digest, unpack into `engine/`.

**B. Build from source:**
1. `git clone --depth 1 --branch <tag> https://github.com/Niko1221/Strata
   tmp/strata-<version>`. Only one clone ever exists; the older one is deleted first. A
   checkout that matches no setup hook is refused: the only `setup.py` on this box is the one
   `tmp/` keeps.
2. `tools/extract_build_options.py` parses the clone (`option(...)` / `set(... CACHE ...)`
   from `CMakeLists.txt`, `ap.add_argument(...)` choices from `setup.py`), writes
   `config/build_options.json`, and diffs it against the previous manifest so the tab can
   report new / removed / changed options between versions.
3. The tab renders dropdowns from that manifest: backend (CUDA/HIP/SYCL), CUDA toolkit 12/13,
   GPU arch list, `STRATA_PORTABLE`, `STRATA_ISA_FLOOR`, `STRATA_MMQ_KQUANTS`,
   `STRATA_Q6K_EXPERTS`, `STRATA_PREFILL_MMQ`, `STRATA_MMQ_TAB`, `STRATA_NATIVE_EXPERTS`,
   `STRATA_BUILD_TESTS`, `STRATA_WERROR`, plus the `setup.py` choices that map to a build.
4. Options are checked against the backend before cmake runs. `serve/build_api.py` carries
   `BACKEND_REQUIREMENTS` (the guards in the v0.1.45 `CMakeLists.txt`: `:97` PREFILL_MMQ needs
   HIP, `:1413` and `:1416` need NATIVE_EXPERTS, `:1612` MMQ_TAB is a CUDA target) and
   `resolve_build_options(defs, device)` applies them: two GPU backends on at once are reduced
   to the one the machine has a toolchain for, and a feature whose backend is off is forced
   OFF with the reason. Verified on this box: `-DSTRATA_ENABLE_CUDA=ON
   -DSTRATA_PREFILL_MMQ=ON` fails at `CMakeLists.txt:98`; with the resolved flags the same
   configure reaches `Configuring done`.
5. `cmake -G Ninja -S tmp/strata-<v> -B tmp/strata-<v>/build <defs>` then
   `cmake --build <build> --target strata strata-vision hypercore-worker -j N` (MSVC/vcvars on
   Windows). The log is streamed to the tab through a byte-offset tail.
6. Binaries are stored per quant (section 15), `engine/BUILD.json` records version, archs,
   source hash and vision source hash, and the clone is deleted.
7. The engine restarts automatically with the new binary.

Endpoints: `GET /build/status`, `POST /build/check`, `POST /build/fetch-prebuilt`,
`POST /build/start`, `GET /build/log?offset=`, `POST /build/cancel`, `POST /build/apply`.

---

## 5. Engine version detection

| Source | Method |
|---|---|
| Installed | `engine/BUILD.json` `version`; else regex `engine=(d.d.d)` in the binary |
| Latest | GitHub releases API for Niko1221/Strata |
| Polling | on page load, then every N minutes (configurable, default 60); never per request |

---

## 6. Cross-platform

- Linux: `setup.sh` -> `.venv` -> `serve/server.py`. Ninja via pip.
- Windows: `setup.bat` / `setup.py`, MSVC or Ninja, vcvars detection, `strata.exe`.
- Paths via `pathlib`, no POSIX-only assumptions in the server; the Build tab has a Windows
  code path. `run.bat` carries the same parity as `run.sh` (`tests/test_run_scripts.py`).
- Per-quant binaries are materialised as a symlink on Linux and a copy on Windows.

---

## 7. Dependencies

`requirements.txt` (every entry has a lower bound, the oldest version tested against):
jinja2, requests, psutil, pillow, pyyaml, regex, numpy, tqdm, cmake, ninja. Optional: optuna
(Tune tab), torch (only for `--sampler gp`). Python 3.10+ to run, `requires-python >=3.11` in
`pyproject.toml` for the tooling. `requirements-dev.txt`: pytest>=8.0, ruff>=0.16, mypy>=1.12.

---

## 8. Milestones

| | Milestone | State |
|---|---|---|
| M0 | Repo skeleton, README, LICENSE, .gitignore, .env.example, git init | done |
| M1 | Port `serve/` + `web/`, engine path configurable, tabs run against a stock engine | done |
| M2 | Tab re-order, Tune/Build shells with stub APIs | done |
| M3 | Build tab: version check, prebuilt download, clone, option manifest + diff, build, transfer, cleanup, auto-restart | done |
| M4 | Tune tab: background sweep, live table, benchmark report, export, apply, auto-restart | done |
| M5 | Tests: IPC parity vs stock Strata, build-option extractor, tune API smoke tests | done, 711 cases |

---

## 9. What upstream Strata does not have (this project brings it)

Checked against Niko1221/Strata main (0.1.45):

| Feature | Upstream Strata | HyperCore-WebUI |
|---|---|---|
| Run-config format (`exe`, `args`, `cwd`, `mmproj`, `model`) | yes, identical | keep as-is |
| `--engine strata --config <json>` | yes | keep, plus auto-discovery of `engine/strata` |
| `/config` settings view | yes | keep |
| Assist tab (`serve/assist.py`, `/assist/*`) | no | ours |
| Agent profiles (`serve/prompts.py`, `/prompts`, `prompts/prompt.json`) | no | ours |
| RAG / document text (`serve/rag.py`, `doc_text.py`) | no | ours |
| Monitor tab | yes | keep |
| Surface/theme controls, thought panel, token-count guard | no | ours |
| Optuna tuner | no | ours |
| Build tab | no (setup.py does it in the shell) | ours |
| Per-quant build store, Model version dropdown | no | ours |
| Worker target (`hypercore-worker`) | no | ours, injected from `engine-src/` |

---

## 10. Decisions locked in

1. **Model files - no download logic.** The Config view has plain text boxes for the pack
   path, the GGUF shards, the tokenizer, the MTP draft and the expert profile. There is a hook
   to Strata's own setup: `POST /config/strata-hook` shells out to the installed `setup.py` in
   a detached subprocess, so the UI never owns that code.
2. **Branding.** Header **HyperCore**, sub-line *Powered by Strata*. Binary stays `strata`;
   the worker is `hypercore-worker`.
3. **`extract_build_options.py`** does two jobs: produce the dropdown manifest, and diff it
   against the previous version's manifest.
4. **The worker is built too** (section 11); upstream has no worker target.
5. **`strata-vision` is in scope**, built as a second cmake target; the CPU variant is used
   when the GPU has no encoder code, exactly as upstream does.
6. **Tune deliverables** go to `config/` and `optuna_tune/results/`. Nothing in the root.
7. **Version dropdown** on the Build tab: latest by default, any published tag selectable.
8. **Assist is endpoint-agnostic**; the lag-probe cache is keyed by endpoint.
9. **API key `0000`, host `0.0.0.0`** by default.
10. **History retention**: last N records per machine (default 20), gitignored. The distilled
    archive under `data/history/` is the exception and is version-controlled.

---

## 11. The worker: what "build it too" requires

`hypercore-worker` is not in upstream Strata. It is HyperCore's own process boundary:
`engine-src/interface/worker_main.cpp`, `engine_process.cpp/.h`, `hypercore_bridge.cpp`,
`cli_gateway.cpp`, `ipc_frame.h`, `glibc_compat.c`, plus
`include/hypercore/hypercore_api.h`, assembled into `libhypercore_api.so` and linked into the
worker.

It holds a 24-byte framed UNIX-socket protocol (magic `0x53545241`, type, request id, payload
length) and drives the real engine as a child process over the `--serve` text protocol. The
point is blast radius: an engine abort kills one worker, not the web server, and engine
threads never touch the GIL.

The Build tab copies `engine-src/` into `tmp/strata-<version>/` before configuring, so the
clone gains the `hypercore_api` and `hypercore-worker` targets, and one cmake invocation
produces three targets. If the injected interface fails to compile against a given Strata
version, the build falls back to engine-only, the server runs in direct stdin/stdout mode, and
the Monitor says `worker: not built`.

---

## 12. Config view (the text boxes)

Engine binary, worker binary, vision binary, model pack dir, GGUF shard(s), tokenizer dir,
MTP draft dir, expert profile, context / KV / GPUs, host / port / API key
(0.0.0.0 / 8081 / 0000), and the Strata setup hook button. Saved to
`config/run-<model>.json` in Strata's own run-config format, so a config written here opens in
a stock Strata install and vice versa. A template run config is not a run config: a deferred
engine re-reads it after a failed attempt, and a name that matches no run config takes the
newest one.

---

## 13. Robustness contract (the two checklists, merged and deduplicated)

The two checklists said the same thing twice. This is the single version, with the test that
proves each item. Every row here is implemented; the test column is the file that runs it.

| # | Contract | Where | Test |
|---|---|---|---|
| 1 | Engine lifecycle is isolated: a `RuntimeError`, `OSError` or `EngineDied` from `start()`/`restart()` is captured, `engine_offline` is set, and the primary thread loop survives | `serve/server.py` `ensure_started` | `tests/test_offline_tabs.py` |
| 2 | The engine runs as an isolated background subprocess under its own supervisor | `serve/engine_supervisor.py`, `serve/direct_engine.py` | `tests/test_offline_engine.py` |
| 3 | Static routes answer with no engine: `/config`, `/mcp`, `/build/status`, `/tune/status`, `/metrics` skip engine probing and answer from host-side state | `serve/server.py` routing | `tests/test_offline_tabs.py` |
| 4 | No worker binary, or a worker that failed to compile, falls back to an un-framed stdin/stdout pipeline matching the stock Strata spec | `serve/direct_engine.py`, `engine/BUILD.json` worker flag, Monitor card | `tests/test_direct_fallback.py` |
| 5 | Attach mode is stateless: no file-descriptor or ownership tracking, `stop()` only stops talking, a local restart does not tear down a shared engine | `serve/attached_engine.py` | `tests/test_attached_engine.py` |
| 6 | `/health` and `/v1/status` return a valid schema offline: `{"loaded": false, "status": "offline", "engine": {"state": "offline", "reason": "unloaded_or_compiling"}}` | `serve/server.py` `/health`, `serve/offline_engine.py` `reason_code` | `tests/test_offline_tabs.py` |
| 7 | The UI gates on backend state: chat actions freeze with a descriptive placeholder, configuration and system views stay interactive | `serve/web/index.html` engine-offline banner | `tests/test_engine_offline_banner.js` |
| 8 | `GET /config` and `POST /config` read and write `config/run-<model>.json` by plain file operations, without polling the engine process | `serve/runconfig.py` | `tests/test_offline_tabs.py` |
| 9 | `POST /config/strata-hook` runs in an isolated `subprocess.Popen` with stdout and stderr piped to `logs/`, disconnected from the server event loop | `serve/strata_hook.py` | `tests/test_strata_hook.py`, `tests/test_strata_hook_ui.js` |
| 10 | `POST /build/start` and `POST /tune/run` dispatch to a background job handle and return immediately | `serve/build_api.py` `BuildJob.start`, `serve/tune_api.py` | `tests/test_build_api.py` |
| 11 | The build log is streamed by byte-offset tail from `logs/build-<tag>.log`, no thread deadlock | `serve/build_api.py` `tail`, `GET /build/log?offset=` | `tests/test_build_api.py` |
| 12 | A sweep starts only after the chat engine is formally stopped (`POST /v1/stop` / `svc.unload()`), and the Chat tab shows "Tuning in progress - the model is unloaded" | `serve/server.py` `/tune/run`, `card_is_busy` | `tests/test_offline_tabs.py` |
| 13 | `engine-src/` is copied into `tmp/strata-<version>/` before cmake runs | `serve/build_api.py`, `tools/build_engine.sh` | `tests/test_build_api.py` |
| 14 | One cmake invocation produces `strata`, `strata-vision` and `hypercore-worker` | `serve/build_api.py` `build_targets` | `tests/test_build_api.py` |
| 15 | A failed worker compile is caught, the build continues engine-only, and the result is reported as `worker: not built` rather than failing the UI | `serve/build_api.py`, `tools/build_engine.sh`, `tools/build_engine.bat` | `tests/test_build_api.py` |
| 16 | Paired launchers: `setup.sh`/`run.sh` and `setup.bat`/`run.bat` behave the same | `run.sh`, `run.bat`, `setup.sh`, `setup.bat` | `tests/test_run_scripts.py` |
| 17 | Nothing at runtime reads `tmp/`; deleting it never interrupts the web service | `.gitignore`, server code paths | `tests/test_run_scripts.py`, `tests/test_build_api.py` |

---

## 14. The 10-point review of 2026-10-19, status

| # | Finding | Status |
|---|---|---|
| 1 | `serve/server.py` was a 6,163-line monolith with 102 top-level defs/classes | done: 4,882 lines, eight modules out (`api_lanes.py`, `config_payload.py`, `engine_args.py`, `engine_errors.py`, `narration.py`, `proc.py`, `tokenizing.py`, `vision.py`) |
| 2 | No lint or type gate (178 ruff findings, 267 mypy errors, nothing enforcing either) | gate done: `pyproject.toml` carries the ruff and mypy config, `ci.yml` runs the ruff gate. Still open: mypy reports 268 errors in 31 files, so it runs `continue-on-error` |
| 3 | No CI | done locally: `.github/workflows/ci.yml` (push + pull_request, python 3.11, ruff, pytest, the node suites, mypy). Blocked: no commit has been pushed, so CI has never actually run |
| 4 | 37 files with uncommitted changes | closed at the time; the working tree is uncommitted again at loop 70 |
| 5 | `requirements.txt` unpinned, no dev requirements | done: lower bounds on every entry, `requirements-dev.txt` names pytest, ruff, mypy |
| 6 | Broad exception handling (89 `except Exception`, 20 swallowing with `pass`) | done: 87 handlers do something, the 6 silent ones each carry `noqa` with the reason; zero bare `except:` in `serve/`, `tools`, `optuna_tune` |
| 7 | Hardcoded machine paths in `tools/build_history.py` | done: `Path.home()`, overridable with `HYPERCORE_HOME` |
| 8 | JS tests silently skipped when node is missing | done: `tests/test_js_suites.py` runs them inside pytest and skips with a stated reason |
| 9 | Test fixtures inside the production package; JS suites read `app.js` as text and slice it with exact marker strings | fixtures moved out (`git mv` to `tests/` and `tools/`). Half done: `tests/dom_stub.js` now holds the read, the cut, the stub DOM and the counter for six suites, and `tests/test_dom_stub.js` checks the plumbing. Still open: 12 suites still define their own `slice()` and stub DOM; the full fix is to export the app functions as a module, which is a rewrite of `app.js`'s top level |
| 10 | Runtime artifacts tracked inconsistently (`data/*` ignored so the history archive was not version-controlled, while mutable data was committed) | done: `.gitignore` negates `data/history/` for `README.md`, `chats/`, `topics/`, `notes/`, `index.json`, `sources.md`; `_old_chats/` (114 MB) and `_old_topics/` stay ignored |

---

## 15. What was built after spec v3 (additions to the contract)

- **One engine build per quant.** `serve/quant_store.py`: `normalise_quant`, `store_binaries`,
  `materialise`, `select`, `list_versions`, `view`. A build named by quant is copied to
  `engine/versions/<quant>/` with its own `BUILD.json` and indexed in `engine/VERSIONS.json`;
  `engine/strata`, `engine/strata-vision` and `engine/hypercore-worker` are materialised from
  the active version (symlink on Linux, copy on Windows) so every run config keeps pointing at
  `engine/strata`. `GET /versions` and `POST /versions/select {"version": "q2_0"}` drive the
  Model version dropdown in the header. `config/run-example.json` is not a model version and is
  not listed. Test: `tests/test_version_dropdown.js`.
- **The build's runtime files are stored per quant**: the Build tab carries the clone's whole
  `data/` into `engine/engine_<quant>/data/`, and a runtime file stored in a subfolder is
  pointed at where it is (`quant_store.merge_data`, `is_artifact`).
- **An engineless start adopts the engine that is there** and says why when it cannot.
- **The ack goes out before the run**, and the supervisor reads a reply that arrives before it.
- **Test scope**: `pytest.ini` sets `testpaths = tests serve/mcp/tests` and `pythonpath = .`;
  the vendored `serve/mcp/web-scrape-mcp/tests` stays out. Verified: 711 passed in 118.15 s,
  exit 0, at loop 70.
- **Shared JS harness**: `tests/dom_stub.js` plus `tests/test_dom_stub.js`; `ci.yml` and
  `tests/test_js_suites.py` glob `tests/test_*.js` so the helper is not run as a suite.
- **History standing task** runs at runtime: `python3 tools/build_history.py` distils
  `_old_chats/` into `chats/`, `topics/` and `notes/`; the archive route runs the builder
  itself. Verified at loop 70: 226 transcripts in `index.json` equals 226 files in `chats/`,
  13 categories, 1549 entries.

---

## 16. Open items at v1.9.1

1. **mypy**: 268 errors in 31 files over 81 source files, advisory in CI. Blocked by size, not
   by tooling. Next step: clear the files with the most findings, then turn
   `continue-on-error` off.
2. **JS suites**: 12 still carry their own `slice()` and stub DOM, so a marker move in
   `app.js` can break twelve files at once. Next step: move them onto `tests/dom_stub.js` one
   at a time, checking each against the output it produced before the move.
3. **CI has never run**: no commit has been pushed to a remote in this session. Verify on the
   first push.
4. **Per-quant switching on a live GPU engine is untested**: no GPU is available in the
   sandbox session. Needs hardware.
5. **`~/test_webUI/HyperCore-WebUI` is out of sync** with the dev source. Needs the user: the
   copy carries machine-local files (`config/run-example.json` with this box's paths, `.env`,
   `optuna_tune/history`, its own `data/`), so the rsync is the user's call.
6. **The GitHub remote URL embeds a personal access token** in `git remote -v`, which is
   readable by anything that can read `.git/config`. Move it to a credential helper or an
   environment variable.
7. **The working tree is uncommitted** at loop 70 (`tests/dom_stub.js`, `tests/test_dom_stub.js`,
   six converted suites, `tests/test_js_suites.py`, `.github/workflows/ci.yml`, `CHANGELOG.md`,
   `data/memory/`). Commit is the user's call.

---

## 17. Version

`v1.9.1` is the tag for this state of the tree. `pyproject.toml` carries `version = "1.9.1"`
and `CHANGELOG.md` carries the matching entry. The tag points at the commit that was HEAD when
it was created, not at the uncommitted working tree.

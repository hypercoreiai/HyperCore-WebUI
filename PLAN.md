> Superseded by [PLAN_v2.md](PLAN_v2.md) (version 1.9.1, 2026-10-19). This file is kept as the
> record of what was approved; the merged file is the one to read.

# HyperCore-WebUI - Framework Specification v3 (framework closed - approved)

Repo: hypercoreiai/HyperCore-WebUI
UI source: /home/ai-user0/HyperCore-WebUI/serve/ (the HyperCore-CCP web app)
Engine: the stock Strata engine, Niko1221/Strata, built or downloaded by this project
Brand: HyperCore - Powered by Strata

The framework is defined. Nothing is open.

Approved:
- Worker built by the engine-src injection described in section 11.
- Port 8081.
- Host 0.0.0.0, API key 0000.
Every one of those is a text box in the Config view, not a hard-coded constant.

---

## 1. The compatibility contract

The project is the front end plus its own Python server. It ships no engine source.
The only native artefact needed at runtime is engine/strata.

1. Engine IPC - the server speaks Strata's stdin/stdout protocol exactly as
   serve/server.py already does: GEN <max_new> <ids>, token ids back, READY, INFO,
   DONE, STOP, QUIT, SWAIT, SERR, BT/BDONE.
   Verified against Strata src/program/generate.cpp (0.1.45).
2. Engine location - resolved in this order:
   --engine flag -> run-config "engine_url" -> --engine-url / $HYPERCORE_ENGINE_URL
   -> run-config "exe" key -> engine/strata in this project -> STRATA_ENGINE env
   var. No hard-coded absolute paths.
3. Feature gating - anything HyperCore-only (HYPERCORE_MMVQ_IL, expert-residency
   counters) is read from the engine INFO line and shown only if present. A stock
   Strata engine never breaks the page.
4. API surface - OpenAI and Anthropic endpoints unchanged.
5. tmp/ is scratch - deleting tmp/ must not affect anything at runtime. It is
   gitignored and never read by the server, only by the Build tab.

### 1.1 One engine per machine (attach mode)

A Strata engine is tens of GB, so a second WebUI on the same PC does not load a
second copy of it:

- `run.sh` checks the port before starting. If the WebUI is already up there, it
  opens that page instead of starting another engine.
- `--engine-url`, `"engine_url"` in a run config, or `$HYPERCORE_ENGINE_URL` makes
  the server attach to an engine another server already loaded, over the OpenAI API
  that engine's server speaks. `serve/attached_engine.py` implements the same
  `generate(prompt_ids, max_new, sampling, cancel) -> token ids` boundary as the
  direct engine, so no tab knows the difference.
- Attaching is a read relationship, not an ownership one: stop, unload and VRAM
  resize are asks to the engine's own server, and the About tab names the address
  the engine lives at.

---

## 2. Repository layout (nothing loose in the root)

HyperCore-WebUI/
  README.md  LICENSE  .gitignore  .env.example  CHANGELOG.md
  setup.sh / setup.py          # venv + engine detection + start
  run.sh / run.bat             # start the WebUI against engine/strata

  serve/
    server.py                  # HTTP, API, engine supervisor
    frontend.py responses.py ipc_parser.py engine_bridge.py
    engine_supervisor.py       # renamed from hypercore_supervisor.py
    assist.py mcp.py prompts.py rag.py structured.py telemetry.py runconfig.py
    web/
      index.html app.js app.css components.css tokens.css sprite.svg
      monitor.html monitor.js fonts/
    tune_api.py                # NEW: subprocess wrapper + status endpoint
    build_api.py               # NEW: version check, clone, build, transfer

  optuna_tune/                # the tuner, ported from HyperCore-WebUI/optuna_tune
    bayesian_tune.py search_space.py engine_runner.py hardware.py
    history.py run_config.py prompt_transfer.py winner_bench.py
    best_prefill.py max_tune.sh stub_engine.py fixtures/
    history/                  # run data per machine; seed-* priors are committed
    results/                  # raw sweep logs and CSVs                  (gitignored)

  tools/
    extract_build_options.py   # NEW: parses CMakeLists.txt + setup.py from a clone
    import_tune_history.py     # NEW: brings measured priors from another install
    build_engine.sh / .bat     # NEW: clone -> configure -> build -> transfer -> clean

  engine/                     # strata, strata-vision, hypercore-worker, BUILD.json
                              # (the binaries are gitignored)
  engine-src/                 # the HyperCore process boundary the Build tab injects
    interface/                # worker_main.cpp, engine_process.*, hypercore_bridge.cpp,
                            # cli_gateway.cpp, ipc_frame.h, glibc_compat.c
    include/hypercore/       # hypercore_api.h
    CMakeLists.txt           # adds hypercore_api + hypercore-worker to the clone
  config/                     # run configs, mcp.yaml, shared settings, build manifest
  prompts/                    # prompt.json and the agent profiles
  data/                       # expert profiles, tokenizer, draft vocab  (gitignored)
  logs/                       # engine + build + tune logs              (gitignored)
  tmp/                        # the single Strata clone                (gitignored)
  tests/
  docs/

Root holds only the text files and the folders. Run configs are
config/run-<model>.json + config/run-<model>.sh; logs go to logs/.

---

## 3. Tabs: Chat - Assist - Monitor - Tune - Build - About

Header brand: HyperCore, with a small Powered by Strata line. The status pill,
theme toggle, surface slider and two-click Shut Down stay as they are.

### 3.1 Chat - unchanged
Streaming answers, thinking shown as produced, agent-profile dropdown from
prompts/prompt.json (GET /prompts), attachments, image input through the vision
engine, sampling drawer, token-count guard, MCP tool calls.

### 3.2 Assist - unchanged behaviour
Second chat over any OpenAI-compatible endpoint (ENDPOINT + API_KEY in .env,
default NVIDIA). Model dropdown pinned to the lag-probed fast list, Show every model,
Check lag with live progress, Clear, thought panel; the call log is mirrored in
Monitor. serve/assist.py already reads ENDPOINT/API_KEY from .env; only the
NVIDIA-specific wording is generalised.

### 3.3 Monitor - unchanged
Model-state card, sparklines (decode/prefill speed, GPU load, VRAM, temp, power, PCIe,
CPU, disk read), context-fill gauge, request table, conversation cache, MCP switches,
external-assistant log.

### 3.4 Tune - new
Front end for optuna_tune/max_tune.sh, run as a background subprocess.

Controls: objective (--gen | --prefill | --latency | --balance), model, context,
trials, seed, repeat, sampler (tpe|gp|random), mode (joint|staged), bounds
(--vram-min/max/step, --pcie, --spec-min-p, --max-workers), and pinning of any
dimension (--ple-io --kv --kvgrow --hostcore --affinity --worker --tail --prefill-cap).

Behaviour:
- Before a sweep starts the chat engine is stopped (POST /v1/stop) so the GPU is free.
  The Chat tab shows "Tuning in progress - the model is unloaded".
- The page carries the note: "Do not close this page while a tune is running. Results
  are reported on completion."
- Live view: progress, trial count, elapsed, incumbent best, and the exact row set the
  CLI writes (layer vram pcie ple-io kv kvgrow hostcore affinity worker tail
  draft_floor prefill_cap status).
- On completion the benchmark results are presented: the winner_bench ladder
  (512/1024/2048/4096/8192/16383-token prompts, medians over --repeat boots), the
  winner's full argument list, and export links.
- Export: config/run-<model>.json + config/run-<model>.sh, the CSV in
  optuna_tune/results/, the record JSON.
- Apply: writes the winner into the active run config and restarts the engine
  automatically.

Endpoints: GET /tune/params, POST /tune/run, GET /tune/status, GET /tune/results,
POST /tune/stop, POST /tune/apply, GET /tune/export.

Priors: `optuna_tune/history/` is keyed to the machine, and a sweep folds its own
trials into it, so the next one starts from what already worked. `tools/import_tune_history.py`
brings measured configurations from another install (default `~/HyperCore-WebUI/optuna_tune/history`):
DRY rows dropped, duplicates folded to the best score, only the top N kept, written as
`seed-<slug>_<model>_<objective>_<context>.json` with provenance. Seven such priors are
committed (best 30 per objective from an i9-14900KF / RTX 4090 box, 228 KB). A record
whose hardware fingerprint differs is listed in the tab but only used when the operator
ticks "use priors from other hardware". Raw `optuna_tune/results/` logs are not imported.

### 3.5 Build - new
See section 4.

### 3.6 About - unchanged, plus provenance
Engine version, source hash from engine/BUILD.json, the upstream Strata release the
engine came from, this PC, API addresses, API key field, dark toggle, model settings
form.

---

## 4. Build / Update tab

Two paths, both offered.

A. Ready-made engine (fast, default)
- Query https://api.github.com/repos/Niko1221/Strata/releases for the latest tag.
- Compare with the installed version (engine/BUILD.json "version", falling back to the
  engine=X.Y.Z string compiled into the binary - the method Strata setup.py uses).
- Download the matching release asset for this OS (strata-linux-x64.zip,
  strata-windows-x64.zip, ...-cuda12.zip, ...-hip.zip), verify size + SHA-256 against
  the published digest, unpack into engine/.

B. Build from source
1. git clone --depth 1 --branch <tag> https://github.com/Niko1221/Strata tmp/strata-<version>
   Only one clone ever exists in tmp/; the older one is deleted first.
2. tools/extract_build_options.py parses that clone:
   - option(...) / set(... CACHE ...) from CMakeLists.txt
   - ap.add_argument(...) choices from setup.py
   -> writes config/build_options.json and diffs it against the previous manifest, so
   the tab can report what changed between versions (new / removed / changed options).
3. The tab renders dropdowns from that manifest: backend (CUDA/HIP/SYCL), CUDA toolkit
   12/13, GPU arch list, STRATA_PORTABLE, STRATA_ISA_FLOOR, STRATA_MMQ_KQUANTS,
   STRATA_Q6K_EXPERTS, STRATA_PREFILL_MMQ, STRATA_MMQ_TAB, STRATA_NATIVE_EXPERTS,
   STRATA_BUILD_TESTS, STRATA_WERROR, plus the setup.py choices that map to a build.
4. cmake -G Ninja -S tmp/strata-<v> -B tmp/strata-<v>/build <defs> then
   cmake --build <build> --target strata -j N (MSVC/vcvars on Windows). The log is
   streamed to the tab.
5. Copy build/strata (and strata-vision if built) into engine/, write
   engine/BUILD.json with version, archs, source hash.
6. Delete the tmp/ clone. Nothing in the project depends on it.
7. Restart the engine automatically with the new binary.

Endpoints: GET /build/status, POST /build/check, POST /build/fetch-prebuilt,
POST /build/start, GET /build/log, POST /build/cancel, POST /build/apply.

---

## 5. Engine version detection

Source | Method
Installed | engine/BUILD.json "version"; else regex engine=(d.d.d) in the binary
Latest | GitHub releases API for Niko1221/Strata
Polling | on page load, then every N minutes (configurable, default 60); never per request

---

## 6. Cross-platform

- Linux: setup.sh -> .venv -> serve/server.py. Ninja via pip.
- Windows: setup.bat / setup.py, MSVC or Ninja, vcvars detection, strata.exe.
- Paths via pathlib, no POSIX-only assumptions in the server; the Build tab has a
  Windows code path.

---

## 7. Dependencies

requirements.txt: jinja2, requests, psutil, pillow, pyyaml, regex, numpy, tqdm,
cmake, ninja. Optional: optuna (Tune tab), torch (only for --sampler gp).
Python 3.10+.

---

## 8. Milestones

M0 | Repo skeleton, README, LICENSE, .gitignore, .env.example, git init + push
M1 | Port serve/ + web/; engine path configurable; Chat/Assist/Monitor/About run against a stock engine/strata
M2 | Tab re-order; Tune/Build shells with stub APIs
M3 | Build tab: version check, prebuilt download, clone, option manifest + diff, build, transfer, cleanup, auto-restart
M4 | Tune tab: background sweep, live table, benchmark report, export, apply + auto-restart
M5 | Tests: IPC parity vs stock Strata, build-option extractor, tune API smoke tests

---

## 9. What upstream Strata does NOT have (we bring these)

Checked against Niko1221/Strata main (0.1.45):

| Feature | Upstream Strata | HyperCore-WebUI |
|---|---|---|
| Run-config format (`exe`, `args`, `cwd`, `mmproj`, `model`) | yes, identical | keep as-is |
| `--engine strata --config <json>` | yes | keep, plus auto-discovery of `engine/strata` |
| `/config` settings view | yes | keep |
| Assist tab (`serve/assist.py`, `/assist/models`, `/assist/chat`, `/assist/log`, `/assist/probe/status`) | **no** | ours - must be ported |
| Agent profiles (`serve/prompts.py`, `/prompts`, `prompts/prompt.json`) | **no** | ours - must be ported |
| RAG / document text (`serve/rag.py`, `doc_text.py`) | **no** | ours |
| Monitor tab (`web/monitor.html`, `monitor.js`) | yes | keep |
| Surface/theme controls, thought panel, token-count guard | no (app.js 62 KB vs ours 116 KB) | ours |
| Optuna tuner | no | ours |
| Build tab | no (setup.py does it in the shell) | ours |

So the port is: take upstream Strata's server as the base it already matches, add the
HyperCore serve modules, and add the two new tabs.

---

## 10. Decisions locked in

1. **Model files - no download logic.** The Config view has plain text boxes for the
   pack path, the GGUF shards, the tokenizer, the MTP draft and the expert profile.
   The user fills them in. There is a **hook** to Strata's own setup/download:
   `POST /config/strata-hook` shells out to the installed Strata `setup.py` (if one is
   found) so a user can run the official model download from the UI without the UI
   owning that code.
2. **Branding.** Everything is HyperCore. Header: **HyperCore**, sub-line
   *Powered by Strata*. About tab: *HyperCore-WebUI - Powered by Strata*, with the
   engine version and the upstream release it came from. Binary stays `strata` (that
   is what upstream produces); the worker is `hypercore-worker`.
3. **extract_build_options.py** does two jobs: (a) produce the dropdown manifest,
   (b) diff it against the previous version's manifest so the Build tab can show
   "new / removed / changed options in this release".
4. **The worker is built too.** See section 11 - upstream Strata has no worker target,
   so the repo carries the interface source.
5. **strata-vision is in scope.** Built from `tools/vision` in the clone as a second
   cmake target; the CPU variant is used when the GPU has no encoder code, exactly as
   upstream does.
6. **Tune deliverables** go to `config/` (the run pair) and `optuna_tune/results/`
   (records, CSVs). Nothing in the root.
7. **Version dropdown** on the Build tab: latest by default, any published tag
   selectable.
8. **Assist** is endpoint-agnostic. The lag-probe cache is keyed by endpoint, so
   switching endpoints gets its own list.
9. **API key 0000, host 0.0.0.0** by default.
10. **History retention** - keep the last N records per machine (default 20), gitignored.

---

## 11. The worker: what "build it too" requires

`hypercore-worker` is not in upstream Strata. It is HyperCore's own process boundary:
`src/interface/worker_main.cpp`, `engine_process.cpp/.h`, `hypercore_bridge.cpp`,
`cli_gateway.cpp`, `ipc_frame.h`, `glibc_compat.c`, plus `include/hypercore/hypercore_api.h`,
assembled into `libhypercore_api.so` and linked into the worker.

What it actually does: it holds a 24-byte framed UNIX-socket protocol
(magic `0x53545241`, type, request id, payload length) and drives the real engine as a
child process over the `--serve` text protocol. The point is blast radius - an engine
abort kills one worker, not the web server, and engine threads never touch the GIL.

**How the Build tab builds it:** the repo ships `engine-src/` containing exactly those
interface files and a `CMakeLists.txt` fragment. Before configuring, the build copies
`engine-src/` into `tmp/strata-<version>/`, so the clone gains the `hypercore_api` and
`hypercore-worker` targets. One cmake invocation then produces three targets:

```
cmake --build tmp/strata-<v>/build --target strata strata-vision hypercore-worker
```

`engine/` then holds `strata`, `strata-vision`, `hypercore-worker` and a `BUILD.json`
recording version, archs, source hash and vision source hash.

If the injected interface fails to compile against a given Strata version, the build
falls back to engine-only and the server runs in direct stdin/stdout mode - the UI
still works, the Monitor says "worker: not built".

---

## 12. Config view (the text boxes)

A single Config panel, reachable from About and Tune, holding:

| Field | Box |
|---|---|
| Engine binary | `engine/strata` (path text box) |
| Worker binary | `engine/hypercore-worker` |
| Vision binary | `engine/strata-vision` |
| Model pack dir | text box |
| GGUF shard(s) | text box |
| Tokenizer dir | text box |
| MTP draft dir | text box |
| Expert profile | text box |
| Context / KV / GPU(s) | text boxes |
| Host / port / API key | text boxes (0.0.0.0 / 8081 / 0000) - approved |
| Strata setup hook | button: run the installed Strata setup to fetch models |

Saved to `config/run-<model>.json` in Strata's own run-config format, so a config
written here opens in a stock Strata install and vice versa.

# Issues, root causes and fixes

Accumulated from 44 transcripts, 60 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**"no frame for 30.0s" / StreamStalled.** The supervisor used the 30 s submit-ack timeout as the
per-frame wall. A 116k-token prompt takes ~31 s to read, so a healthy request was declared stalled
in the middle of its prefill. The silence wall now belongs to the supervisor (`self.silence_s`,
from the config's `engine_silence_s`, default 300 s; 0 waits as long as the request needs) and
`stream()` measures from the last frame. `timeout_s` is only the ack wall.

**The agent dropdown showed only the system prompt.** The server looked for `ROOT/prompt.json`;
the profiles are in `prompts/prompt.json`. `find_prompt_file` now checks both.

**Monitor cards blank (Reused, Tok/s, hit rate, prefill).** The terminal summary's `prompt_read`
and `prefill_ms` were parsed nowhere, so the derived fields never existed. `derive_monitor_field`
in `serve/ipc_parser.py` now computes what the worker does not report.

**"Exception occurred during processing of request".** An unhandled exception inside `do_POST`
left a half-written SSE body and the stdlib traceback. The handler now records the failure, closes
the stream with an error event, and returns a 500 JSON only when nothing was sent yet.

**401 from the web page.** A server started with an API key serves a page that does not know it.
The page now carries the key it requires.

**Worker socket EADDRINUSE.** A Unix socket file outlives the process that bound it; the next bind
on the same name fails. Two supervisors sharing a name also share the `{socket}.config.json` side
file. Socket names are pid- or port-namespaced for this reason.

**A stalled link is not a dead link.** `WorkerUnreachable` and `StreamStalled` are different
exceptions and the HTTP layer must distinguish them; conflating them caused needless engine
restarts.

**The reason a health route carries is a state, not a label.** A stand-in's reason is true only
while nothing has been asked for. Once a real engine has been taken and it is not running, the
cause belongs to that engine, and the only thing a worker adapter can say is its death note - it
has no `status()`. Reporting the stand-in's sentence after that is reporting a fact that has
already stopped being true.


## What is true now

how the thing works, as the chats established it


**/api-monitor**
- Root cause (`serve/server.py:3629`, the `/` and `/api-monitor` handlers): Replacing `<title>` with the script deletes the opening tag, so the served head becomes: Confirmed against the live server: The browser parses `HyperCore` as a stray text node in `<head>`, which is why it shows up in the top-left corner and pushes the banner down.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**/metrics**
- Live proof: `/metrics` shows a 146,161-token prompt with `prompt_ms 36888.0` finishing `finish: "stop"`, and `grep -c "no frame for" hypercore-q2_0.log` is 0.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**/proc**
- Fixture `/proc` text through a recording stand-in for `open()`, asserting every handle is closed — a leak is caught by the test, not by the OS
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>

**4daf35b**
- The engine fix (`4daf35b`) The root cause of the garbling was a stale interleaved copy: `xil_` is built for one `(n_in, ncols)` of one group's activations and reused for calls sharing it.
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>

**_created**
- Two caveats worth knowing: denials come back as error text, so the hub still marks the call `ok=True` — the reason is in the payload; and `_created` is per-process, so after the restart nothing counts as session-owned until a tool writes it.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**aborted**
- One new finding, and it is not a bug in the engine's logic. `CUDA_LAUNCH_BLOCKING=1` stalls: The `Aborted` is the #29 watchdog's deliberate stop — `generate.cpp:7584 std::abort()` after `release_gpu_waits()`, not a segfault.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**build/testing/temporary/lasttest.log**
- Test 49 `ple_parity` still SEGFAULTs when the card is free (1.31 s, empty output in `build/Testing/Temporary/LastTest.log`; stdout is block-buffered so the crash point is not visible).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>

**cfunctype**
- The callback is rebound each iteration. The previous `CFUNCTYPE` object is freed while the request thread still holds the raw pointer: The segfault is in `destroy()`, not in the engine.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**do_post**
- Request exception — an unhandled exception inside `do_POST` no longer leaves a half-written SSE body and the stdlib traceback; it now closes the stream with an error event and returns a 500 JSON only when nothing was sent.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**doc_to_target**
- I re-ran it through `score_agent.py` and it reproduced — after fixing a real bug: for `multiple_choice` tasks `doc_to_target` returns a choice index, not the answer string, so the original string-comparison scored all 60 MC items as misses.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**docs/details.md**
- `docs/DETAILS.md` says of this exact path: "the PCIe share of the missed experts (computed on the GPU instead of the CPU) still made the first answer after a start differ from the next ones." And issue #31 is a documented bug in precisely this expert-copy path for the IQ packs ("the experts are now copied by a GPU kernel, `--pcie-mode dma` restores the old way").
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**example debugging input**
- Example of debugging an input gray image (raw, not preprocessed): Now both the LLaMA part and the image encoder is in … [universal] /home/ai-user0/Strata/third_party/llama.cpp/docs/multimodal/llava.md … the LLaVA model to LLaMA and multimodel projector constituents: 4.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-36.md</sub>

**filter.note**
- The reason is printed to stderr and surfaced in the UI as `filter.note`.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**generating**
- Against HEAD it reproduces your exact traceback: With the fix, the probe returns: And the prefill test confirms the `reading` → `generating` transition is real, not a guess: with `prefill_s=0.25` the view shows `["reading", "idle"]` during prefill and all-idle after the request finishes.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**hypercore_worker_binary**
- Root cause: `src/interface/hypercore_bridge.cpp:445` reads the worker binary from the environment before the JSON, matching the documented precedence (defaults → env → JSON): So with `HYPERCORE_WORKER_BINARY` set, `{"engine_mode":"worker"}` does have a binary and init correctly succeeds.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**hypercoreengine**
- Legacy text protocol — `HyperCoreEngine` lives in `serve/fixtures/legacy_engine.py` and is instantiated only by the four test files; production reaches the engine through `HyperCoreProcessSupervisor`.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

**il_builds**
- bad: `hypercore serve: MMVQ_IL CHECK FAILED: X of Y interleaved calls differ from native_mmvq (Z values):` followed by ` Q4_K 6144 x 4 columns -> 2560: 3 of 120 calls differ, 7 values` `il_builds` ≈ `il_uses` means every call rebuilt its copy — the shapes alternate and nothing is reused, which is the signature of the stale-copy bug the `il_fresh(n_in, ncols)` guard fixes.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**il_ready**
- To confirm the isolation: If that run garbles and `HYPERCORE_MMVQ_IL=0` does not, the `il_ready` guard is the root cause and the other two switches are innocent.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**merges.txt**
- Found a real bug while testing this: an empty `merges.txt` crashed the tokenizer read (`merge 0 is not a pair`); it is now filtered.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>

**mmvq_il**
- The prefill path (`src/prefill/`) is a different code path from the verify window where `MMVQ_IL` lives, so this crash is a separate fault — though a CUDA fault is sticky, so it needs reproducing on a free card before we call it independent.
  <sub>2026-10-14 · strata-chat-2026-10-14-13-13.md</sub>

**mmvq_il_check**
- That is itself a code bug to fix before trusting `MMVQ_IL_CHECK`.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**mtp_max_t**
- Root cause: the `suffix_draft` / `mtp_max_t` arms were built with `step=2`, so the grid was `{0,2,4,6,8}`.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**mypy**
- Fix: add a minimal CI that runs `tools/run_tests.sh`, `ruff check`, and `mypy`, and add a `pyproject.toml` with the ruff/mypy config so the gate is reproducible rather than ad hoc.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**numerous other features**
- Numerous other new features, improvements, and bug fixes are fully listed in the [Change History](ChangeHistory.md) file.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-18-39.md</sub>

**pool.cpp**
- Crash isolation is tested, but with a caveat I've written down: the three `std::abort()` calls in `pool.cpp` are not reachable from the fixture, so the test SIGKILLs the worker.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**printf**
- The beginning and the end are kept] This tool takes start_line/end_line: read the next range instead of repeating the same call.prefix; the guard test proves an 8-byte caller is untouched beyond 8 bytes | One test bug worth recording, because it is the kind that hides a real failure: reading the caller's struct in the same `printf` argument list as the call that fills it.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**retrying model fits**
- Retrying with a model that fits: Root cause found: torch 2.10.0+cu128 has no kernels for sm_61 (GTX 1060 is Pascal) — `torch.cuda.is_available()` returns True but every kernel call fails.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**serve/web/test_large_document.js**
- `serve/web/test_large_document.js` is new (40 checks: a 660 KB transcript fixture at caps 132000/32768/16384/8192/4096, idempotence, multi-turn folding, the tool fallback, the anchored ratio, the fence crash).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**start_engine**
- It is a traceback from `tools/calibrate.py:401`: It died at the first `start_engine` call (`calibrate.py:258`), before any sweep ran.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>

**terminal__run/pipe/process/repl/symbols/check/install/build/compile/tidy/debug/query/compare/status**
- Terminal: `terminal__run/pipe/process/repl/symbols/check/install/build/compile/tidy/debug/query/compare/status` — git, ruff, mypy, pylint, pytest, cmake, clang-tidy, gdb, valgrind, jq, yq, sqlite3, strace are all present.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

**tests/**
- The rename script itself is the root cause. `tools/rename_hypercore.sh` greps only `src include sycl serve tools bench CMakeLists.txt` — `tests/` is not in the list.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**trace.py**
- I instrumented the identical code path with a stall in the unlocked window so the schedule is reproducible rather than luck-dependent (`trace.py`, actual output): What the pointer state is doing. Three inserts against a cache of three slots produced three evictions when only two were needed.
  <sub>2026-10-07 · strata-chat-2026-10-07-14-36.md · also in 2026-10-07-strata-chat-2026-10-07-14-36.md</sub>

**verdict initio prediction**
- Verdict: a ±10 K ab-initio Tc prediction for an unsynthesized compound is not currently achievable, and the reason is specific and well-understood. The pipeline. DFPT phonons → Eliashberg/Allen–Dynes: `ω_log` and `λ` are genuinely computable from first principles.
  <sub>2026-10-07 · strata-chat-2026-10-07-18-16.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**fixed _callee_name helper** · 6 chats
- One bug I found and fixed. My first version used `zip -x "/home/ai-user0/HyperCore-WebUI/.venv"`, and it silently archived the whole virtualenv — 15,035 extra entries.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-31.md</sub>
- Also fixed, because it was blocking the run: the `no frame for 30.0s` stall.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-07-09.md</sub>
- The socket issue is fixed and now covered by install.sh.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>
- Verified: Two bugs I fixed in your version: `self._session_counter += 1` inside worker threads is load/add/store, so two threads can land on the same id and overwrite each other's event — now behind a lock, with `_next_session_id()` as the single source.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>
- Fixed with `pip install --user "huggingface-hub<1.0"` then `transformers==5.19.0 accelerate`.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>
- Fixed with a `_callee_name()` helper, plus a self-test that feeds the guard a deliberately leaking function so it cannot degrade into a no-op again.
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>

**--full**
- Two bugs fixed on the way: `main()` initialised the product counter to 0 so `--full` reported "configurations: 0", and the script aborted on the root-owned `tune/hypercore_max_tuning_matrix.log` (preflight now checks writability and falls back to `tune/results/`).
  <sub>2026-10-12 · hypercore-chat-2026-10-12-13-18.md</sub>

**--glm-pack**
- Reproduced trial 1's exact recorded args against the Maya engine: The engine's contract is fixed in `/home/ai-user0/project-maya/src/core/glm_model.cu`: `--glm-pack` takes a directory only — there is no flag to name the shards.
  <sub>2026-10-15 · strata-chat-2026-10-15-09-32.md</sub>

**--port**
- Three bugs it caught and I fixed: `status` printed nothing because `describe()` read `$pid/cmdline` instead of `/proc/$pid/cmdline`; a transient `/proc` entry vanished mid-scan and killed the script under `set -e`; and `--port` parsing used `shift` inside a `for` loop.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**.mcp_test_state.json**
- `.mcp_test_state.json` — loop 121, parses OK: root cause recorded, guard implemented and compiled, the on-card confirmation and the sm_89 rebuild as the remaining queue.
  <sub>2026-10-14 · strata-chat-2026-10-14-15-52.md</sub>

**/etc/sudoers.d/ai-user0**
- Current (installed): `sudoers.ai-user0.fixed`. It is byte-for-byte the logic running as `/etc/sudoers.d/ai-user0`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/usr/sbin/visudo**
- The two bugs I flagged earlier are fixed in the installed file: the visudo grant now names `/usr/sbin/visudo` (the real path), and docker is granted — `sudo -n /usr/bin/docker ps` returns an empty container table, exit 0.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**args**
- Reproduced directly: Fixed. Moved it out of `args` into the top level: Verified: `jq -e` parses, and `./engine/hypercore $(jq -r '.args[]' …)` now gets all the way through argument parsing to `--tokens is required` — the expected stop for generate-mode.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**bench/results/2026-10-09-blackbox-api/worker_mode_test.c**
- Two bugs fixed in `bench/results/2026-10-09-blackbox-api/worker_mode_test.c`: 1.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**checked same class**
- I checked for the same class of bug everywhere (`grep -E '\[[0-9]+\] = "(HYPERCORE|HyperCore|hypercore)'`) — those two were the only fixed-size buffers affected.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**download_auth**
- Verified it still works for real: Two bugs the tests exposed, fixed along the way The `download_auth` tests failed for a different reason than the other nine — they were testing functions that didn't exist (`download_refusal`, `download_headers`, `hf_access_token`).
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**floatdistribution**
- Two bugs found and fixed on the way: `(0, 0)` is a truthy tuple, so `display or any_link` returned the empty one; and Optuna 5.0.0 has no `UniformIntDistribution`, so `distributions_for()` now picks `IntDistribution`/`FloatDistribution` with fallbacks to the older names.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>

**leak_demo.cpp**
- One subtle bug fixed: the command guard's name pattern didn't accept `+`, so `g++` was read as an argument and `leak_demo.cpp` was treated as the program.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-19-51.md</sub>

**min_width**
- Fixed a precedence bug in the `min_width` filter Rewritten as an explicit loop.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**mute_ping**
- Added `mute_ping` to `serve/frame_fixture.py` and `test_a_silent_link_is_called_unreachable_not_stalled`, which also asserts the host reaps its slot (`sup.in_flight() == 0`) either way.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

**note these bench**
- Note these are bench numbers for a fixed 10240-token prompt at `--max-new 512`; a live conversation will not reproduce them.
  <sub>2026-10-15 · strata-chat-2026-10-15-18-43.md</sub>

**serve/terminal_mcp.py**
- Added to `serve/terminal_mcp.py` (server now exposes 14 tools: `run, pipe, process, repl, symbols, check, install, status` + `build, compile, tidy, debug, query, compare`).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-19-51.md</sub>

**test_a_prior_off_the_step_is_outside_the_grid_not_a_crash**
- Tests: 2 added — `test_the_engine_defaults_are_a_point_in_the_grid` (the invariant that would have caught this) and `test_a_prior_off_the_step_is_outside_the_grid_not_a_crash`; the existing bounds test now asserts `3 in suffix_draft.grid()`.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**vram collapse root**
- The vram collapse has a root cause, and it is fixed. Both 25-trial sweeps put every trial at `vram=1280` despite `--vram-min 768 --vram-max 1280 --vram-step 256`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**black_box engine start**
- Any black_box engine start dies before it can answer: I reproduced that with the control arm `HYPERCORE_MMVQ_IL=0 ./debug_mmvq_il.sh` — it fails the same way ("native embedding: cannot pin 260 MiB, nor place it in VRAM").
  <sub>2026-10-14 · strata-chat-2026-10-14-13-13.md</sub>

**nothing ladder gets**
- Nothing in the ladder gets under 10%, and the reason is that prompt size is not the variable that controls it.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**speed_lines**
- `maya.py:2424` (`speed_lines`) adds the guard for issue #8: a request that only read a prompt has no decode, so a `glm stat` line with `decode < 1 ms/tok` or `vram hit 0.00%` is rewritten to `glm stat (prompt only): prompt …` rather than reporting a 96,000 tok/s decode speed.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

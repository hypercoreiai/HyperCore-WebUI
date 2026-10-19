# Testing and code quality

Accumulated from 66 transcripts, 129 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**Tests are stdlib unittest, run as modules.** `python3 -m unittest serve.test_server` — there is
no test runner dependency. `serve/test_*.py` covers the server and the engine adapters;
`tools/test_*.py` covers setup, the MCP servers and the harnesses.

**Nothing in a test touches a real GPU.** `serve/frame_fixture.py`'s `ScriptedWorker` is a
in-process stand-in for the worker that speaks the frame protocol: it accepts a submit, emits
token frames after a configurable prefill delay, and sends a terminal summary. `serve/fixtures/
legacy_engine.py` does the same for the older engine interface. A test that needs the real binary
is a contract test (`tools/test_abi_contract.py`), and it is run separately.

**Mocking is by patching the function, not the process.** `tools/test_setup_golden.py`'s `install()`
patches `gpus`, `ram_gb`, `cpu_info`, `is_wsl`, `data_folder`, `load_settings`, `save_settings`
and `subprocess.call`, then calls `setup.main()` and returns the exit code, the printed text, the
config that was written and the questions asked. `quiet()` swallows the output so a test can
assert on it.

**Golden baselines are recorded, not hand-written.** `tools/test_setup_golden.json` holds the
expected config for 25 machine profiles (RAM × GPU count × model). When a change to setup
legitimately changes what a config contains, the baseline is re-recorded with
`python3 tools/test_setup_golden.py --write` and the diff is inspected before accepting it — that
is how the host/api_key/turn_tail_check defaults were verified to be the only change.

**Collection is scoped to `tests/`, by `pytest.ini`.** Loop 41 keeps the Strata clone in
`tmp/` instead of removing it, so a bare `pytest` from the repo root walks into that clone and
collects its own test files; they import `tools/_paths.py`, which calls `sys.exit()` when
llama.cpp's gguf-py is not on the box. Before `pytest.ini` that ended the run with 61
collection errors before it reached this project's tests. `testpaths = tests` and
`norecursedirs` exclude `tmp/`, so the bare run and `tools/run_tests.sh` collect the same
thing: 326 tests, no errors. `tests/test_pytest_scope.py` asserts both halves - the scope, and
that no file under `tests/` imports gguf-py, so the scope is not hiding a dependency the suite
can't run without. The clone's own tests are still runnable on their own from inside the clone.

**Lint is advisory, not a gate.** `ruff check` is run on changed files and the pre-existing
findings are separated from the new ones; a change is accepted only if it adds no new finding.


## What is true now

how the thing works, as the chats established it


**mypy** · 3 chats
- Fix: add a minimal CI that runs `tools/run_tests.sh`, `ruff check`, and `mypy`, and add a `pyproject.toml` with the ruff/mypy config so the gate is reproducible rather than ad hoc.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>
- `pytest`, `ruff`, `mypy`, and `pyright` are used by the project but are not declared anywhere.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>
- `mypy` is clean on `serve/strata_hook.py`, `serve/offline_engine.py` and the new tests.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**terminal__check** · 3 chats
- `terminal__check` — ruff, mypy, pylint, pytest (whichever are installed)
  <sub>2026-10-14 · hypercore-chat-2026-10-14-16-25.md · also in strata-chat-2026-10-03-15-05.md</sub>
- `terminal__check` — lint/type/test with ruff, mypy, pylint, pytest (whichever are installed)
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md · also in 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>
- `terminal__check`'s ruff stage fails — `Failed to create temporary file, Permission denied /home/.ruff_cache/0.16.10/…`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/home/.ruff_cache** · 2 chats
- As ai-agent: writes to `/home/ai-user0/.terminal`, `/home/ai-user0/0`, and `/home/.ruff_cache` all succeed; `sudo -n apt-get update` succeeds.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- Workaround: run it directly — `RUFF_CACHE_DIR=/home/ai-user0/.ruff_cache /home/ai-user0/.pyenv/versions/3.12.9/bin/python3 -m ruff check …` (the default `/home/.ruff_cache` is not writable).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/.terminal/cache** · 2 chats
- `RUFF_CACHE_DIR` fixes the "Failed to initialize cache at /home/.ruff_cache: Permission denied" failure — `/home/ai-user0/.terminal/cache` is writable by both user0 and ai-agent.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- `RUFF_CACHE_DIR` points at `/home/ai-user0/.terminal/cache`, which is writable by both users — the ruff cache failure is gone.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/opt/ai-workspace/.tmp/pytest-of-ai-user0** · 2 chats
- Needs `TMPDIR` pointed at a writable dir; the default `/opt/ai-workspace/.tmp/pytest-of-ai-user0` is not owned by this user.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- The 16 errors are an environment artifact — pytest's temp root `/opt/ai-workspace/.tmp/pytest-of-ai-user0` is owned by `ai-agent` and I can't remove it; with `TMPDIR=/tmp` those 18 web-scrape tests pass.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md · also in strata-chat-2026-10-06-23-05.md</sub>

**hypercoreengine** · 2 chats
- Legacy text protocol — `HyperCoreEngine` lives in `serve/fixtures/legacy_engine.py` and is instantiated only by the four test files; production reaches the engine through `HyperCoreProcessSupervisor`.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>
- `serve/server.py` is untouched — 5,324 lines, and `test_server.py`, `test_lifecycle.py`, `test_detok.py`, `test_structured.py` still import `HyperCoreEngine`.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**pytest tmpdir system** · 2 chats
- `pytest test_bayesian_tune.py` → 47 passed in 83.4s (two new tests: one asserts the key and flag are absent when the server lacks them, one asserts they are kept when a server defines them)
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>
- Run pytest with `TMPDIR=$PWD/.tmp` — the system tmpdir on this box is owned by another user.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-20-11.md · also in 2026-10-07-strata-chat-2026-10-07-20-11.md</sub>

**python3** · 2 chats
- CI runs `python3 -m unittest tools.test_glibc_floor` from the root; the rest of the suite is unreachable from the documented invocation.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-03-30.md</sub>
- ruff is available in the pyenv 3.12.9 interpreter but not the system `python3`.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**restart script text** · 2 chats
- The text-protocol integration tests (test_slots.py, test_control_cancel.py, test_fatal_recovery.py) must be rewritten using ipc_parser and binary frame fixtures rather than retired.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- The restart script and the text primitives behind the document tools are tested separately, with no server and no workspace: The restart script and the text primitives behind the document tools are tested separately: `python3 -m unittest serve.test_restart_script serve.test_doc_text` - 24 cases, no server needed.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-23-05.md</sub>

**ruff** · 2 chats
- `ruff` on `setup.py` is back at the HEAD baseline (55 issues, no new ones); the new files are clean.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>
- Script is at `tools/daytona_bridge_test.py`; it's `ruff`-clean.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-18-55.md</sub>

**serve/test_abi_contract.py** · 2 chats
- `serve/test_abi_contract.py` proves the lock isn't vacuous — it edits the real header (insert above `top_k`, delete `top_p`, widen `top_k` to `uint64_t`) and asserts the checker fails.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>
- Test culture is genuinely strong: 94 Python test files, 37 C++/CUDA/HIP test files, ABI alignment checks (`tools/check_abi_alignment.py`, `serve/test_abi_contract.py`), a glibc-floor gate, cross-toolchain test (Daytona), backend-parity tests.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-03-30.md</sub>

**serve/test_prompts.py** · 2 chats
- (`serve/test_prompts.py` had the same wrong path, which is why `test_the_real_prompt_json_offers_the_shipped_profiles` was failing.) Fix — resolve the file instead of assuming where it is:
  <sub>2026-10-11 · 2026-10-11-hypercore-chat-2026-10-11-06-34.md</sub>
- One failure in `serve/test_prompts.py` is pre-existing — it fails at HEAD too, and none of the files I touched are involved.
  <sub>2026-10-06 · strata-chat-2026-10-06-00-55.md</sub>

**suite runs fixture** · 2 chats
- The fixture transcript must have the format the parser expects: `## You` / `## Assistant` sections.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>
- The C suite runs on the fixture/synthetic token path (`src/interface/hypercore_bridge.cpp:559`).
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-11-52.md</sub>

**terminal_check terminal_install check** · 2 chats
- `terminal__install` closed the gap I flagged earlier — pytest 9.1.1 and ruff 0.16.10 are now installed in pyenv 3.12.9, and `check` uses them: `symbols` gave a real read of the MCP hub: `StdioTransport`, `HttpTransport`, `McpServer`, `McpHub`, and the `McpError → McpTimeout` hierarchy.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-09.md</sub>
- terminal_check / terminal_install — ⚠️ the check tool only probes binaries on PATH, so it reported ruff/mypy/pylint unavailable even though ruff 0.16.10 is installed under pyenv 3.12.9.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**valgrind cmake ripgrep** · 2 chats
- Fix: split into `requirements.txt` (runtime, pinned) and `requirements-dev.txt` (pytest, ruff, mypy), and note that `cmake`/`ninja` via pip are only needed for the Build tab.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>
- `gdb valgrind cmake ripgrep sqlite3 ffmpeg mypy pylint python3-pytest shellcheck acl` via apt, and `ruff black vulture pyright` via pip (pyright fetches its own node, so it works despite no system node).
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**--prompt-tokens**
- Trial 1 of the run you posted (verbatim from `results/maya-q2_0.record.json`, now at the 32768 default): Then on stdin, once the session is up: (the ids are `fixtures/prompt.json` cycled to `--prompt-tokens`, default 10240).
  <sub>2026-10-15 · strata-chat-2026-10-15-09-53.md</sub>

**--prompt-tokens-file**
- The fixture prompt (`optuna_tune/fixtures/prompt.json`, a 20-token id cycle) makes the model EOS at 292 tokens on every rung except 2048, so a real prompt via `--prompt-tokens-file` is what gets the decode past the 540-token line.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**--turn-tail-check**
- `tools/test_setup_config.py`: new `ServerDefaults` class (new install gets all three; `--host 127.0.0.1 --api-key s3cret --no-turn-tail-check` wins; an existing config keeps its own values; the run script carries `--turn-tail-check` and honours a config that turns it off).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**.env**
- Two candidate items are already ruled out by direct checks: zero bare `except:` in `serve/`, `tools`, `optuna_tune`, and `logs/`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.env` are already in `.gitignore`.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**.mcp_test_state.json**
- Baseline: `pytest -q` gives 448 passed in 77.16 s, `.mcp_test_state.json` parses and is current (loop 66, `remaining_queue: []`, 1 recorded error).
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**.mypy_cache**
- Verified with a full run to a temp directory: `README.md`, `serve/server.py`, `docs/media/Assist.png` and the script itself are all present; `.venv`, `.venv_daytona`, `.mypy_cache`, `.pytest_cache` and `__pycache__` are absent (0 matches).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-31.md</sub>

**.review_pytest_all.txt**
- `.review_pytest_all.txt` ends with 24 collection errors — that number is from the old tree, not this one.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**.sh**
- `pytest optuna_tune/test_bayesian_tune.py -q` → 13 passed in 31.9s (covers DONE/INFO parsing, the argument builder including the on/off switches, seed determinism, the latency objective picking the minimum row, staged layer labelling, dry run launching nothing, and that both deliverables are written and the `.sh` passes `bash -n`).
  <sub>2026-10-12 · hypercore-chat-2026-10-12-14-31.md</sub>

**.so**
- Note also: the current `.so` is still the fixture spine (`run_request` sleeps `40 + max_new/2` ms per request), so these are dispatch numbers, not engine numbers.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**.ssh/authorized_keys**
- `pytest serve/test_docs_mcp.py serve/test_server.py serve/test_responses.py tools/test_hypercore_agent.py` → 246 passed, 1 failed — the single failure is the pre-existing `.ssh/authorized_keys` guard issue, not caused by the copy.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**.venv_daytona**
- Dependency missing in the interpreter pytest uses — 1 error The SDK is installed — in `.venv_daytona`, which I created earlier.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**/etc/sudoers.d/ai-agent**
- For reference, `/etc/sudoers.d/ai-agent` only allows NOPASSWD for `apt-get`, `pip3 install`, nginx `systemctl`, the ruff_cache mkdir/chmod, and `visudo -c` — `sudo` with that password refuses anything else ("I'm sorry ai-agent.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**/home/ai-user0/.pyenv/shims**
- `/home/ai-user0/.pyenv/shims` is already on the PATH of the MCP server process, so installing into a pyenv version and rehashing makes the tools visible to `shutil.which`: Verified now: `mypy → /home/ai-user0/.pyenv/shims/mypy`, `pylint → /home/ai-user0/.pyenv/shims/pylint`, `ruff → /home/ai-user0/.pyenv/shims/ruff`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/.pyenv/versions/3.12.9/lib/python3.12/site-packages**
- `terminal_check` can't see tools installed in the pyenv interpreters. It reported `ruff` missing even though ruff 0.16.10 is installed at `/home/ai-user0/.pyenv/versions/3.12.9/lib/python3.12/site-packages` — it only probes binaries on PATH.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md · also in 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/.pyenv/versions/bin**
- Suggested fix: when a `python` argument is given, probe `<interpreter> -m ruff --version` / `-m mypy --version`, and otherwise scan `/home/ai-user0/.pyenv/versions/bin`.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/hypercore-webui**
- Live repo: `/home/ai-user0/HyperCore-WebUI` (git-tracked, HEAD `05ab2edd` — "the worker now runs the real engine instead of a fixture").
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-11-52.md</sub>

**/home/ai-user0/hypercore-webui/tools/test_expert_residency_diff.py**
- `/home/ai-user0/HyperCore-WebUI/tools/test_expert_residency_diff.py` (pytest differential suite) `python3 -m pytest tools/test_expert_residency_diff.py -q` gives 6 passed, 3 skipped (the three skipped ones need the GPU and are gated by `RESIDENCY_LIVE` / `RESIDENCY_SWEEP` / `RESIDENCY_CURVE`).
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**/home/ai-user0/lm-eval-bench/summarise_results.py**
- Artifact `/home/ai-user0/lm-eval-bench/summarise_results.py` (runs, prints the table above; ruff flags only a shebang-not-executable and import-order nit).
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-17-37.md</sub>

**/home/ai-user0/strata**
- `python3 -m pytest -q` in `/home/ai-user0/Strata`: 265 passed, 5 skipped, 149 subtests passed (the 5 skips are genuinely conditional: no pack tokenizer, Windows‑only job objects).
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-16.md</sub>

**/home/ai-user0/strata/hypercore-tune/.mcp_test_state.json**
- Run this when you have a GitHub session: Everything else is done: the repository is built, 45 tests pass, ruff is clean, paths are absolute, and the state file at `/home/ai-user0/Strata/HyperCore-Tune/.mcp_test_state.json` records the two remaining items (the push, and a real GPU sweep once `optuna` is installed into the Strata venv).
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**/proc**
- Fixture `/proc` text through a recording stand-in for `open()`, asserting every handle is closed — a leak is caught by the test, not by the OS
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>

**/tmp/residency_.log**
- Every live run therefore rewrote the fixture the differential tests are built on, and the EngineSession's own `/tmp/residency_.log` stayed empty, which is why the curve run reported "no stderr captured".
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**/usr/bin/python3**
- `pytest -q tools/` under `/usr/bin/python3` fails at collection (8 errors) on `import numpy` — `tools/strata_pack.py:30`.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-06-01.md</sub>

**after.rs**
- Tools are tested with golden diffs. `crates/agent/src/tools/evals/` contains per‑task fixtures (`before.rs`, `after.rs`, `possible-01.diff` … `possible-07.diff`) behind a `unit-eval` feature flag.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**agent_user**
- `PERM` and the user/group names are built from the script's own constants (`WORKSPACE_DIR`, `BIN_DIR`, `BIN_SUBDIR`, `NPM_GLOBAL_DIR`, `TMP_DIR`, `PROFILE_HOOK`, `AGENT_USER`, `DEV_GROUP`, plus a local `RUFF_CACHE_DIR="/home/.ruff_cache"`), so the permission grants can't drift from the directories section 2 creates.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-31.md</sub>

**assignment**
- `mypy` reports 58 `union-attr` and 52 `assignment` errors, concentrated in the deferred-engine block: One variable is reused for an engine, a request handler, and `None`.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**assist.py**
- ruff on `assist.py` reports only the three pre-existing `BLE001`; mypy's two errors are pre-existing.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**base**
- `serve/test_mcp.py`: `test_get_mcp` now asserts the reported prefix equals `svc.encode_prompt` of the same probe; `test_the_prefix_grows_with_the_tool_surface` checks it increases with a second tool, is cached for an identical surface, and that a no-tool chat pays only `base`; the `NoServers` case was updated for the new key.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**caps**
- `ruff` clean; the pre-existing mypy complaints in `ipc_parser.py` and the `stderr_target`/`caps` assignments are untouched by these changes.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>

**changelog.md**
- Nothing is committed yet: `git status --short` shows the new modules, `pytest.ini`, `CHANGELOG.md` and `tests/test_pytest_scope.py` as modified/untracked.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**cls.srv**
- The other test files get away with `cls.srv` because their methods are unannotated and mypy skips unannotated bodies by default; my new typed helper exposed it.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**consequence quality findings**
- The consequence is that the quality findings just sit there: 39 of the ruff findings are auto-fixable (`ruff check --fix`).
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**core**
- `tests/` is the C++ suite tree (`core`, `cuda`, `hip`, `prefill`); `serve/fixtures/` already exists and already holds test data.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**data/experimental-speed-projection/qwen3.8-flash-next-experimental-speed-projection.gguf**
- `tools/test_backend_parity.py` is fixture-level, and there are no `pack/full` weights in the workspace — only `data/experimental-speed-projection/Qwen3.8-Flash-Next-experimental-speed-projection.gguf`.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**defaults**
- Also left alone are test fixtures that pass explicit values to exercise the mechanism rather than define the limit (`serve/test_mcp.py`, `serve/mcp/tests/test_hub.py`, `tools/test_setup_config.py`); `test_hub.py:73` compares against `DEFAULTS`, so it tracks the new value automatically.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-09-01.md</sub>

**docs/mcp_server.md**
- `docs/MCP_SERVER.md` presents it as a server for external assistants: To make it available in this chat, add it beside the others: Its tests are separate from the retest harness: `python -m unittest tools/test_strata_mcp.py` runs against a fake setup and server, so adding it to `mcp.yaml` wouldn't bring it into the 221-case `run_all.py` count.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**early_close_test.py**
- Scripts collected as test modules — 2 errors `early_close_test.py` built its URL from `sys.argv[1]` at import; under pytest that's the file's own path, hence `unknown url type: 'tools/v1/chat/completions'`.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md · also in strata-chat-2026-10-10-07-17.md</sub>

**except**
- Lint debt: ruff on `serve/` reports 79 `SIM115` (file opened without a context manager), 19 blind `except`, 16 unused imports, 14 mutable class defaults.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**five other files**
- Five other files already do it correctly: `serve/test_parallel.py:20` and `serve/test_lifecycle.py:15` both use `from serve.fixtures.legacy_engine import HyperCoreEngine`.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**fixtures/prompt.json**
- `token 248045 out of range [0, 154880)` — `fixtures/prompt.json` is another model's vocabulary.
  <sub>2026-10-15 · strata-chat-2026-10-15-16-58.md</sub>

**grep**
- Allowlist errors are good (they name the binary and the `TERMINAL_ALLOW_EXTRA`/`TERMINAL_ALLOW_ALL` escape hatch) but could suggest close matches — `rg` is absent while `grep` is present; adding ripgrep, `rg`, `sqlite3` and `mypy` would cover a lot.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**here actual breakdown**
- Here's the actual breakdown from `python3 -m pytest tools/`: Note what that means: pytest aborts before running a single test.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**iq_parity.cpp**
- `iq_parity.cpp` checks dequantization against gguf-py on generated fixtures and states the error metric explicitly: "bitwise equality is not claimed and not required."
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>

**liverate**
- `serve/test_server.py:844` (`LiveRate`) is the test that pins that behaviour.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**min_engine**
- `tools/test_setup_strix_halo.py:383,392,399` fail for the same reason: the fixture zips carry `MIN_ENGINE`-era versions and the refusal message now reads "it is version 0.1.40; this setup needs 0.1.42".
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>

**moving tests fixtures**
- Moving it to tests/fixtures/legacy_engine.py keeps the test suites passing without contaminating the clean production runtime.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**optuna_tune/test_prompt_transfer.py**
- `optuna_tune/test_prompt_transfer.py`: 16 passed (fixtures are verbatim lines from `run-2026-10-16_060014-draft-floor,topology-1.log`).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**permissionerror**
- Also, `rocm_root()` wrote its marker into `sys.prefix`, which is read-only for a system interpreter — that was the `PermissionError` in `test_setup_unsloth`.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**pool.cpp**
- Crash isolation is tested, but with a caveat I've written down: the three `std::abort()` calls in `pool.cpp` are not reachable from the fixture, so the test SIGKILLs the worker.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**prompt.json**
- Nothing is committed to git yet (`prompt.json`, `serve/prompts.py`, `serve/test_prompts.py` are new; `serve/server.py`, `serve/web/`, README and docs are modified).
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**prompts/**
- `serve/test_prompts.py`: the shipped-file test now uses `find_prompt_file(ROOT)`, and a new test covers the search order (root wins over `prompts/`, and neither → the "is not there" error).
  <sub>2026-10-11 · 2026-10-11-hypercore-chat-2026-10-11-06-34.md</sub>

**pytest.ini**
- `pytest.ini` sets `testpaths = tests` and `tools/run_tests.sh` runs `pytest tests/ -q`.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md · also in strata-chat-2026-10-17-20-23.md</sub>

**resident engine class**
- The resident engine class lives in `serve/fixtures/legacy_engine.py:50`, which is exactly how `serve/test_parallel.py:20` and `serve/test_lifecycle.py:15` already reach it.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**run_request**
- `run_request` takes three timestamps instead of two (`src/interface/hypercore_bridge.cpp:219-231`): The fixture sleeps `prompt.size()` ms for prompt evaluation, so the two windows are independent and the subtraction is testable rather than asserted.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**serve/**
- The one thing I did not finish: a fresh full `serve/` + `tools/` pytest run — it was still running when I stopped it, so the pass/fail total above is the recorded figure plus my spot-check, not a new full-suite number.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**serve/docs_mcp.py**
- Pre-existing lint and mypy findings in `serve/mcp/web_scrape_mcp.py` and `serve/docs_mcp.py` are known and not blocking; no test failure is caused by them.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>

**serve/test_mcp.py**
- If you want, I can patch `app.js:95` to call `loadMcp()` after the key is saved — a one-line change, covered by `serve/test_mcp.py`.
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**serve/web/test_large_document.js**
- `serve/web/test_large_document.js` is new (40 checks: a 660 KB transcript fixture at caps 132000/32768/16384/8192/4096, idempotence, multi-turn folding, the tool fallback, the anchored ratio, the fence crash).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**setup.py**
- `mypy --ignore-missing-imports` on `hardware.py history.py bayesian_tune.py search_space.py engine_runner.py run_config.py` → no errors in those files (the 81 errors reported are all in `setup.py`, pre-existing).
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>

**src/kernels/_parity.cpp**
- Parity/bench/test files are colocated with the kernels they check (`src/kernels/_parity.cpp`), plus a separate `tests/` tree: `tests/core` 12 files, `tests/hip` 16, `tests/cuda` 3, `tests/prefill` 1, `tests/platform` 1, `tests/data/native_experts` 6 fixtures.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**src/kernels/ple_parity.cpp**
- `src/kernels/ple_parity.cpp` now refuses a placeholder fixture instead of emitting seven unrelated failures: after the geometry check it prints `the capture carries a signal` and exits 1 with `BLOCK SKIPPED; the fixture has no signal`.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-07-50.md</sub>

**strata.example.com**
- `strata.example.com` is a test fixture domain.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**system_prompt**
- At the moment of the full run the file had only `system_prompt` and no profiles at all, which is why pytest showed `[]` against the expected three.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**terminal__run/pipe/process/repl/symbols/check/install/build/compile/tidy/debug/query/compare/status**
- Terminal: `terminal__run/pipe/process/repl/symbols/check/install/build/compile/tidy/debug/query/compare/status` — git, ruff, mypy, pylint, pytest, cmake, clang-tidy, gdb, valgrind, jq, yq, sqlite3, strace are all present.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

**terminal__status**
- If you want the actual installed set (git, ruff, mypy, pytest, cmake, clang-tidy, gdb, valgrind, jq, yq, sqlite3, docker, strace) rather than the capability list, `terminal__status` gives versions in one call.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**test_a_model_the_endpoint_does_not_answer_for_is_not_listed**
- `python3 -m pytest serve/test_web_client.py serve/test_monitor.py serve/test_assist.py` → 55 passed, 8 subtests, 0 failed, including a new `test_a_model_the_endpoint_does_not_answer_for_is_not_listed` that checks each of the four filter sites and asserts the old group is absent.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>

**test_lifecycle.py**
- `test_server.py` and `test_lifecycle.py` are untouched because they still exercise the old path.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**there tokenizer vocab**
- There is tokenizer vocab.json in fixtures.
  <sub>2026-10-15 · strata-chat-2026-10-15-16-58.md</sub>

**tools/**
- `tools/` suite: 551 passed, 4 skipped, 189 subtests, 0 failed (the golden baseline was re-recorded; the only diff across all 25 profiles is those three keys).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-07-09.md</sub>

**tools/test_best_model.py**
- `tools/test_best_model.py` imports a module that does not exist in the repo, so pytest aborts collection on it.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md · also in hypercore-chat-2026-10-13-00-06.md</sub>

**when flag active**
- When this flag is active, the server bypasses the strict file-presence checks for vocab.json and spins up a dedicated ScriptedWorker proxy backend instead of the real CUDA daemon.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**window.hypercore_api_key**
- A server started with a key now injects `window.HYPERCORE_API_KEY` into the page it serves, so the chat UI works without typing the key into Settings (`serve/test_server.py::WebApp` covers it).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**ruff** · 3 chats
- What changed `ruff check` clean, `python3 -m pytest tools/test_expert_residency_diff.py -q` gives 9 passed, 3 skipped (the 3 need the GPU).
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>
- `ruff` on the changed files reports only pre-existing findings.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md · also in hypercore-chat-2026-10-11-07-09.md, hypercore-chat-2026-10-11-11-46.md</sub>
- `ruff` clean on the changed files (dropped the now-unused `struct` import)
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**--help**
- Seven had zero references anywhere in code, CI, or docs: Kept — all four are genuinely referenced: Also removed 11 untracked `.build.log` / `.ctest.log` / `.pytest_all2.log` dumps (440 KB), and a 0-byte socket file literally named `--help` — the residue of a mistyped command.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**/home/ai-user0/.terminal/cache**
- Fixed by re-adding `RUFF_CACHE_DIR: $HOME/.terminal/cache` to `mcp.yaml` (validated: `load_mcp_yaml` expands it to `/home/ai-user0/.terminal/cache`, and `ruff check` with that env returns "All checks passed!").
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/proc/2400540/environ**
- The `mcp.yaml` env I added is picked up (`PYENV_ROOT`, `RUFF_CACHE_DIR`, `TERMINAL_ALLOW_EXTRA` all present in `/proc/2400540/environ`), so `sudo`, `setfacl`, `getfacl`, `nproc` are now on the allowlist and `sudo -n apt-get update` succeeds without a password.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**8a5a67b..f3f48d4**
- Targeted re-run of the suites this session added or changed: The test pass turned up four small defects in code I wrote last turn, fixed and committed as `f3f48d4` (pushed, `8a5a67b..f3f48d4`): `ruff check` on all nine touched files: "All checks passed!".
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**condense**
- `git diff --stat`: `serve/mcp.py +172`, `serve/server.py +73`, `serve/test_mcp.py +68`, `docs/DETAILS.md +64`; 54 added lines mention `condense`.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**expert_x_comment_agent**
- What the test asserts. `serve/test_prompts.py:132` checks the live file against a hard-coded list: What your file has now. `prompt.json` is at `_meta.version: 5`, "v4: added expert_x_comment_agent", so `catalog()` returns four profiles: the three the test expects plus `expert_x_comment_agent`.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**fixture page plain**
- The fixture page had a plain `<a href="youtube…">` link, which is not media — replaced with a real `<iframe>` embed, plus `<video poster>`/`<audio>` so video and audio are actually covered.
  <sub>2026-10-04 · strata-chat-2026-10-04-05-39.md</sub>

**home__read**
- `ruff check` and `mypy` clean on the four changed files One caveat: the MCP server processes already running in this session are the pre-edit code, so `home__read` and `terminal__query` here still behave the old way until those servers restart.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**hypercore.**
- I renamed the primary keys to `hypercore.` (including the fixture files in `tests/data/native_experts/`) and added a read-only legacy fallback in `src/kernels/ngram.cpp` and `src/kernels/cpu/expert_layout.cpp` so pre-rename packs still load.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**max_tune.bat**
- Exec bits are correct in the committed tree: `max_tune.sh` and `max_tune.bat` 100755, `maya_env.py` 100755 (the mode change ruff flagged is fixed).
  <sub>2026-10-16 · strata-chat-2026-10-16-02-29.md</sub>

**mute_ping**
- Added `mute_ping` to `serve/frame_fixture.py` and `test_a_silent_link_is_called_unreachable_not_stalled`, which also asserts the host reaps its slot (`sup.in_flight() == 0`) either way.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

**patches/test-docs-mcp-portable.patch**
- The single failure is exactly the portability problem from the last turn, not a tool bug: Fixed in `patches/test-docs-mcp-portable.patch` (derive the repo root from the already-imported package, use the running interpreter): Verified: `pytest _test_docs_mcp_patched.py` → 7 passed.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**serve.test_abi_contract**
- Verification: `serve.test_turn_tail`, `serve.test_monitor_fields`, `serve.test_runconfig`, `serve.test_web_client`, `serve.test_server`, `serve.test_stream_loop`, `serve.test_parallel`, `serve.test_fatal_recovery`, `serve.test_lifecycle`, `serve.test_engine_process`, `serve.test_abi_contract`, `serve.test_slots` — 322 tests, all green; ruff clean on the changed files.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**serve/test_stream_loop.py**
- `serve/hypercore_supervisor.py:685` read that frame and threw the reason away: Test added at `serve/test_stream_loop.py` (`StreamReaderTest.test_a_dead_link_says_why_it_closed`, using the fixture's `die_after=2`): passes on its own, and the module runs 16 tests OK (`python3 -m unittest serve.test_stream_loop -v`, exit 0).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>

**serve/test_terminal_mcp.py**
- `serve/test_terminal_mcp.py`: added `test_the_device_files_are_exempt_from_the_root` in the existing unittest style.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**test added serve**
- Test added: `serve/test_server.py AmdTelemetry.test_pcie_link_is_the_narrowest_slowest_hop`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-14-18.md</sub>

**tools test_setup_golden write**
- `tools/test_setup_golden.py --write` re-recorded: across all 25 PC profiles the only config difference is those three keys, nothing else changed.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**.env**
- The project is 8.6 GB, and 8.0 GB of it is regenerable: `git clean -ndx` would sweep 429 items — but it would also take things you probably want to keep: `engine/` (the 120 MB engine binary), `third_party/llama.cpp/` (167 MB, needed by `tools/iq_fixture.py` and `tools/_paths.py` for `gguf-py`), `.env`, and the transcript `strata-chat-2026-10-10-00-02.md`.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**fixture deterministic runs**
- With the 40 ms fixture that's deterministic (10/10 runs).
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

## Gotchas

things that bit once and will bite again


**exe002**
- A mock-engine server started with the default path now serves all four profiles: `ruff` and `mypy` on the changed files report nothing new (only the pre-existing `EXE002` executable-bit warning).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**memset**
- Is `NONE = 0` a real state or a `memset` artifact? If it means "not reported", the header should say so and the fixture should never emit it.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**serve/test_mcp.py**
- Tests — `serve/test_mcp.py` now has `test_results_condense_under_context_pressure` (with an 8,192-token mock context, rounds 1–2 are untouched, round 3 crosses 0.4 and condenses, including the current round when still over the mark) and `test_condensing_off_keeps_every_result` (a 5-round loop with `condense_at: 0` never condenses).
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

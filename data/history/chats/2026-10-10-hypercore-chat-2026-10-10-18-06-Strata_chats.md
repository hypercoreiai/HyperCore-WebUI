# hypercore-chat-2026-10-10-18-06.md

2026-10-10 · 3537 KB · 5 requests · topics: engine-internals, hardware-environment, issue-log, memory-vram, performance-benchmarks, testing-quality, web-ui-monitor

## What was asked
- What tools do you have
- NEXT Optimization:
- implement #1 This is what strata does i think.
- wire that call site up and run the A/B
- 1, Can I have the GPU for ~10 minutes? sure 2. It seems like large context over long runs. 3. What is the method that Strata uses?

## What came of it
- `web__search` — DuckDuckGo search (returns titles, URLs, snippets)
- `terminal__repl` — persistent Python or Bash session that keeps state between calls
- `terminal__status` — what's installed and which versions are available Build & analysis tooling
- `terminal__check` — lint/type-check/test with ruff, mypy, pylint, pytest (whichever are installed)
- Found a hang in my test (eager `last_stderr()` reads a still-open pipe).
- An empty `socket_path` now defaults to a unique path rather than `""` (which the worker would bind as an empty sun_path).
- `stop()` takes the name back, so the next run doesn't inherit it.
- `src/interface/worker_main.cpp` — the failure is now self-diagnosing: `tools/backend_parity.py` / `tools/bench_flood.py` — pid-namespaced paths, matching the pattern `tools/calibrate.py` already used.
- `serve/test_socket_hygiene.py` (new) — 10 tests against the real worker: stale file, directory at the path, restart-after-stop, two supervisors on one name, and "the name is gone after shutdown".
- Three consecutive parity runs leave zero socket files in `/tmp`.
- `ruff` clean; the pre-existing mypy complaints in `ipc_parser.py` and the `stderr_target`/`caps` assignments are untouched by these changes.
- One note on the test I wrote: `sup.last_stderr()` blocks forever while the worker is alive, because its stderr pipe has no EOF.
- Before launching the server, you must elevate your terminal environment's memlock ceiling: ulimit -l ulimit -l unlimited ./install.sh ------------------------------
- Once mlockall is active and memory limits are lifted, execute a fast stress run using your local benchmarking tools: python3 tools/backend_parity.py --repeat 3 1.
- And the engine's own telemetry says it plainly: `0 blob reads from the file` on every request.

## Status: implemented, with the test that proves it

| Item | State | Where |
|---|---|---|
| 1.1 ensure_loaded captures an engine failure | already in place, now tested | serve/server.py ensure_started, tests/test_offline_tabs.py |
| 1.2 static routes answer with no engine | verified | tests/test_offline_tabs.py |
| 1.3 no worker binary -> direct stdin/stdout engine | added | serve/server.py hypercore branch, serve/direct_engine.py, tests/test_direct_fallback.py |
| 1.4 attach mode is a read relationship | already in place | serve/attached_engine.py (stop() only stops talking), tests/test_attached_engine.py |
| 2.1 /health offline schema | added | serve/server.py /health, serve/offline_engine.py reason_code, tests/test_offline_tabs.py |
| 2.2 config read and write without an engine | verified | tests/test_offline_tabs.py |
| 2.3 POST /config/strata-hook in a detached subprocess | added | serve/strata_hook.py, tests/test_strata_hook.py, tests/test_strata_hook_ui.js |
| 3.1 non-blocking build and tune jobs | already in place | serve/build_api.py BuildJob.start, serve/tune_api.py |
| 3.2 live streamed build log | added (byte-offset tail) | serve/build_api.py tail, GET /build/log?offset=, tests/test_build_api.py |
| 3.3 the engine is stopped before a sweep | already in place | serve/server.py /tune/run calls engine.unload(); the retry pauses via card_is_busy |
| 4.1 engine-src injected before cmake | already in place | serve/build_api.py, tools/build_engine.sh |
| 4.2 one cmake invocation, three targets | already in place | serve/build_api.py build_targets |
| 4.3 graceful worker fallback flagged in BUILD.json | added | serve/build_api.py build_targets, tools/build_engine.sh, tools/build_engine.bat |
| 5.1 paired setup/run scripts | run.bat parity added | run.bat, tests/test_run_scripts.py |
| 5.2 nothing at runtime reads tmp/ | verified | tests/test_run_scripts.py, tests/test_build_api.py |

1. Robust Server Architecture & Uncoupling

• Isolation of ensure_loaded Lifecycle: Refactor Service.ensure_loaded() so that if self.engine.restart() or self.engine.start_worker() raises a RuntimeError, OSError, or EngineDied exception, it gracefully captures the error, sets an internal boolean state (self.engine_offline = True), and returns without crashing the primary thread loop of serve/server.py.
• State-Resilient Core API Loop: Audit all Handler.do_GET and Handler.do_POST routing methods. Ensure that when svc.loaded() returns False, requests to static tabs (/config, /mcp, /build/status, /tune/status, /metrics) skip the engine-probing logic and answer directly using host-side memory or fallback static state definitions.
• Fallback Direct IPC Integration: Write serve/engine_supervisor.py to check for compilation flags and target architecture outputs inside engine/. Ensure HyperCoreRequestHandler dynamically falls back to an un-framed stdin/stdout streaming pipeline matching the stock Strata engine spec if engine/hypercore-worker is missing or exits with a compilation error indicator ("worker: not built").
• Stateless Attach Mode Safety: Isolate the AttachedEngine construction pipeline inside serve/attached_engine.py from active file descriptors or ownership tracking structures. Ensure it treats target endpoints (--engine-url, "engine_url") strictly as a stateless "read relationship" over the standard OpenAI API specification, eliminating all cascaded tear-downs on local server restarts.

2. Standalone UI & View-State Protections

• Dynamic UI State Gating: Update Handler.do_GET to ensure that when serving /health or /v1/status while the backend compute nodes are offline, it returns a valid JSON schema payload structured with {"loaded": false, "status": "offline", "engine": {"state": "offline", "reason": "unloaded_or_compiling"}}, signaling web/app.js to dynamically mask chat features while keeping other visual layers fully interactive.
• Persistent Configuration Access: Ensure that paths mapping to GET /config and POST /config read and write explicitly to config/run-<model>.json maps via standard file system operations without validating or polling the active binary state of the inference engine process.
• Subprocess Shell Separation: Wrap the execution sequence mapped to POST /config/strata-hook within an isolated subprocess.Popen configuration that pipes stderr and stdout directly to files inside logs/, completely disconnecting it from the synchronous server runtime event loop to prevent blocking behavior while checking remote assets.

3. Background Tooling APIs (Build & Tune Tabs)

• Non-Blocking Compilation Threading: Refactor the integration hooks for serve/build_api.py and serve/tune_api.py to ensure that execution triggers (POST /build/start, POST /tune/run) immediately dispatch work units to a background worker engine thread using a non-blocking execution handle (build_api.JOB.start, tune_api.JOB.start).
• Live Streamed Log Handlers: Route active build updates over GET /build/log by building an incremental byte-offset reader that tails raw log lines written from background tasks to logs/, feeding them straight to the client socket stream without thread deadlocks.
• State Coordination Isolation: Before spawning optimization routines, modify POST /tune/run to explicitly invoke svc.unload() or issue an automatic POST /v1/stop command to formally clean and tear down running GPU contexts, moving the local chat engine cleanly to a standby state ("Tuning in progress - the model is unloaded") prior to executing Optuna parameter sweeps.

4. Build Injection & Fallback Compilation

• Pre-Configure Injected Modules: Script a structural preparation sequence within tools/build_engine.sh that checks for the existence of engine-src/ and forces a recursive directory copy directly into the targeted extraction target tmp/strata-<version>/ prior to executing CMake build generator steps.
• Unified Multi-Target Invocation: Construct the explicit build instruction string inside tools/build_engine.sh to target all core binaries simultaneously within a single compilation task block using the syntax:bash
cmake --build tmp/strata-<version>/build --target strata strata-vision hypercore-worker -j N
Use code with caution.
• Graceful Compilation Fallback: Integrate an advanced conditional trap block around the compilation task string. If the target build target hypercore-worker produces a compilation fault against the specified upstream Strata source revision, catch the status code, resume compilation targeting only strata and strata-vision, and flag the fallback condition inside engine/BUILD.json so the Monitor view accurately displays "worker: not built".

5. Deployment Framework & File Protections

• Dual-Platform Execution Scripts: Maintain mirror-image pairs of platform configuration controllers (setup.sh and run.sh targeting Linux systems alongside paired setup.bat and run.bat targeting Windows platforms natively using standard MSVC/vcvars environment detection schemes).
• Scratchpad Path Isolation: Enforce a strict file access contract stating that no live generation path, application routing method, or API endpoint managed within serve/server.py may reference or traverse paths inside the tmp/ root folder, guaranteeing that an interactive user session or an active file purge of tmp/ during code maintenance operations never triggers runtime faults.

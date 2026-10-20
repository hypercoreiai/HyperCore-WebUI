> Superseded by [PLAN_v2.md](PLAN_v2.md) (version 1.9.1, 2026-10-19); its section 13 is this
> list, deduplicated against PLAN_CHECKLIST1.md.

## Status: implemented, with the test that proves it

| Deliverable | State | Where |
|---|---|---|
| Engine as an isolated background subprocess | already in place | serve/engine_supervisor.py, serve/direct_engine.py |
| Server keeps answering when the engine exits | verified | tests/test_offline_tabs.py, tests/test_offline_engine.py |
| Direct pipe fallback when the worker is not built | added | serve/server.py hypercore branch, tests/test_direct_fallback.py |
| Attach mode as a pure reader | already in place | serve/attached_engine.py |
| UI freezes the chat when the engine is offline | already in place | serve/web/index.html engine-offline banner, tests/test_engine_offline_banner.js |
| Editable config saved to config/run-<model>.json | already in place | serve/runconfig.py, tests/test_offline_tabs.py |
| Strata setup hook in a background subprocess | added | serve/strata_hook.py, tests/test_strata_hook.py, tests/test_strata_hook_ui.js |
| Clone, compile and binary mapping in the background | already in place | serve/build_api.py |
| Persistent streaming build endpoint | added (offset tail, logs/build-<tag>.log) | GET /build/log?offset=, tests/test_build_api.py |
| Optuna wrapper with a stop handshake before a sweep | already in place | serve/tune_api.py, serve/server.py /tune/run |
| engine-src copied into tmp/strata-<version>/ before CMake | already in place | serve/build_api.py, tools/build_engine.sh |
| Three CMake targets in one build stream | already in place | serve/build_api.py build_targets |
| Report "worker: not built" rather than failing the whole UI | added | engine/BUILD.json worker flag, Monitor Model state card |
| Paired Unix and Windows launchers | run.bat parity added | run.sh, run.bat, setup.sh, setup.bat, tests/test_run_scripts.py |
| Runtime insulated from tmp/ | verified | tests/test_run_scripts.py |

1. Robust Server Architecture & Uncoupling

• Independent Lifecycles: Implement a decoupled multi-process layout where the Python server (serve/server.py) manages the lifecycle of the engine (engine/strata) as an isolated background subprocess via an independent engine supervisor.
• State-Resilient Core API: Ensure that if the engine exits with an error or is unlinked, the web server continues to accept HTTP requests on port 8081 without raising unhandled internal exceptions.
• Fallback Direct IPC Integration: Write serve/engine_supervisor.py to seamlessly fall back to a direct stdin/stdout pipe relationship with a stock Strata engine if the custom hypercore-worker wrapper fails to compile or crashes.
• Attach Mode Resilience: Establish a separate connection mechanism (serve/attached_engine.py) using the OpenAI API spec so the interface can bind to a pre-existing engine instance as a pure "reader," ensuring local server restarts don't kill a shared host engine.

2. Standalone UI & View-State Protections

• Dynamic UI Control Gating: Update web/app.js and CSS assets to listen to backend status updates; if the engine goes offline (GET /build/status or GET /tune/status), freeze chat actions with descriptive placeholders while keeping configuration and system views fully unlocked.
• Persistent Configuration View: Provide text-editable configuration controls inside the UI for variables like host, port, API keys, and model artifact paths, saving explicitly to standard config/run-<model>.json maps.
• Engine Hook Isolation: Map the "Strata setup hook" button to run setup.py through an isolated background subprocess (POST /config/strata-hook), so downloading dependencies or checking remote components never blocks the primary server event loop.

3. Background Tooling APIs (Build & Tune Tabs)

• Non-Blocking Build Engine: Implement serve/build_api.py and tools/build_engine.sh to handle Git cloning, CMake compilation, and binary mapping entirely in the background.
• Live Streamed Logging: Serve active build outputs via a persistent streaming endpoint (GET /build/log) so operators can track build status without freezing or locking the global frontend layout.
• Isolated Optimization Engine: Build serve/tune_api.py to wrap the Optuna max_tune.sh process.
• Explicit State Handshaking: Before a sweep starts, send a formal POST /v1/stop signal to free GPU VRAM and place the Chat tab into a clean "Tuning in progress" state, preventing multi-process hardware conflicts.

4. Build Injection & Fallback Compilation

• Pre-Configure Inject Module: Script a sequence that automatically copies the localized engine-src/ process isolation layer directly into tmp/strata-<version>/ prior to invoking CMake commands.
• Unified Multi-Target Invocation: Configure CMake to process three discrete output targets simultaneously within a single build stream:
	• strata (The baseline processing engine)
	• strata-vision (The graphical processing auxiliary)
	• hypercore-worker (The isolated process boundary controller)
• Graceful Compilation Fallback: If the engine-src code fails to build against the targeted Strata repository version, immediately catch the error, fallback to compiling standard binaries only, and report "worker: not built" via the Monitor tab rather than failing the whole web UI.

5. Deployment Framework & File Protections

• Dual-Platform Launch Frameworks: Package setup.sh/run.sh script layers for Unix environments alongside fully matched setup.bat/run.bat scripts for MSVC/vcvars Windows deployments.
• Scratchpad Segregation: Fully insulate all runtime components from the tmp/ folder. Ensure the deletion of tmp/ during code maintenance operations never interrupts active web service features or chat operations.

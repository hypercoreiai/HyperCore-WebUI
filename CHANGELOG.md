# Changelog

## 0.1.0 - in development

The HyperCore web app (Chat, Assist, Monitor, About) running on a stock Strata
engine, plus two tabs the engine does not ship: Tune and Build.

- **Tabs** in the order Chat, Assist, Monitor, Tune, Build, About. Chat, Assist,
  Monitor and About are the existing views, unchanged in behaviour; anything
  HyperCore-only in the engine's INFO line is shown only when the engine reports it.
- **Attach mode** (`--engine-url`, `HYPERCORE_ENGINE_URL`, or an `"engine_url"` in
  a run config): the WebUI talks to an engine another server already loaded, over
  its OpenAI API, instead of starting a second copy of a tens-of-GB model.
  `serve/attached_engine.py` implements the same
  `generate(ids, max_new, sampling, cancel) -> token ids` boundary as the direct
  engine, so no tab knows the difference. It is now automatic: before starting an
  engine the server probes the port the run config names and attaches to whatever
  answers. `--no-attach` forces a local engine.
- **Run configs**: `config/run-example.json` is a template and is never picked by
  accident; `run.sh`/`run.bat` skip it and say so when asked for it. Every path an
  engine argument names is checked before the process is spawned, and an engine that
  exits is reported in its own words - a missing model shard is called a missing
  model shard, not a memory problem.
- **A fresh clone starts the app**: with nothing installed yet `run.sh` and `run.bat`
  do not exit, they serve the page with no engine. The Build tab is what produces the
  first engine and the first run config, so a page that never starts cannot build one.
  An engine that is present is still started, a real run config is still used, and
  `--ui-only` on the command line is forwarded once rather than twice.
  `tests/test_run_scripts.py` runs `run.sh` against a stub server in all four cases and
  checks the same branch exists on both platforms.
- **Build tab**: version check against the Niko1221/Strata releases, ready-made
  download with SHA-256 verification, or a from-source build whose dropdowns are
  extracted from that release's own `CMakeLists.txt` and `setup.py` and diffed
  against the previous manifest. The releases box is a list of every published tag
  with `main` as the fallback, so it is never empty offline. The card order of the tab
  is now: **Setup choices** at the top (it is what a build needs before anything is
  cloned), then the releases box, the build options, the log, and the **Engine** card at
  the bottom, since it reports what a build produced. `tests/test_build_tab_order.js`
  reads the order out of `serve/web/index.html` and fails if a card is moved back.
  The **Setup choices** card holds the model, quant, context, KV cache, image and
  experimental flags of
  the selected release are read from that release's own `setup.py` (a raw file fetch,
  not a clone) before anything is built, each one shown as a control with the command
  line they stand for, and `Run setup with these` posts them to the setup hook. The default is no images: image controls are left unset or shown as `no`, dependent image controls are disabled while images are off, and the hook is sent no image flags unless an image choice is explicitly selected. Flags
  that are housekeeping (paths, ports, mirrors) are counted in one line rather than
  listed. If the status call itself fails the card is still opened and says why, rather
  than leaving the tab with no build controls at all, and the page is served with
  `Cache-Control: no-store` so a browser cannot show a copy of the tab from before a
  restart. Every element the tab reaches is addressed by id now: `$` is `getElementById`,
  and the CSS selectors `"view-build .build-log"` and `"view-tune .tune-progress"`
  returned null, so a status payload carrying a build job (a finished or failed one
  included) threw inside the render and emptied the choices card. Those cards have ids,
  and `tests/test_build_choices_render.js` runs the whole `loadBuild` against a stub DOM
  built from the real page with a real `build/status` payload.
  The hook finds the checkout it can run: the path the request names, then
  `HYPERCORE_STRATA_SRC`, then the `cwd` of the run config the server is using (that
  is the Strata folder the engine runs from), then `strata/` in this project, then a
  Strata folder beside it. `tmp/` is never a candidate, and when it is the only clone
  on the machine the message says so rather than just "no checkout". A setup
  started from the page is never interactive: `--yes` and `--no-start` are added to the
  command the page sends (a flag already there is not repeated) and stdin is closed, so a
  question the boxes left unanswered takes the answer `setup.py` recommends for this
  machine instead of waiting on a prompt nobody can type into. The command shown under the
  boxes is the command that runs, hook flags included. While a build runs in the
  background the button that started it reads **Kill Build** and stops it, and a status
  card under the boxes says which step `setup.py` has reached (read from its own
  `=== Step N: ... ===` markers, not from a list we maintain), how long it has been
  going, the percent of a download in progress, and every `[X]`, `[!]` or traceback it
  printed. A from-source build reports its own stage (cloning, configuring, compiling,
  transferring) in the same card. One
  clone in `tmp/` at a time; it is deleted when the build finishes and nothing at
  runtime reads it.
- **A build plan cmake accepts**: the Build tab applies the same guards upstream's
  `CMakeLists.txt` uses before cmake is called. `BACKEND_REQUIREMENTS` in
  `serve/build_api.py` lists them (v0.1.45: `:97` `STRATA_PREFILL_MMQ` needs HIP,
  `:1413` `STRATA_PREFILL_MMQ` needs `STRATA_NATIVE_EXPERTS`, `:1416` the same for
  `STRATA_ORCA_Q4KS_MMQ`, `:1612` the MMQ pointer table is a CUDA target) and
  `resolve_build_options` applies them: two GPU backends on at once are reduced to the
  one the machine has a toolchain for, and a feature whose backend is off is sent OFF
  with the reason in the build log. The defaults the tab shows are the resolved ones,
  and an option this machine cannot build says in its help what it would need. On this
  box (Intel CPU, RTX 4090) `-DSTRATA_PREFILL_MMQ=ON` with HIP off failed the configure
  with `STRATA_PREFILL_MMQ requires STRATA_ENABLE_HIP=ON`; with the guard the same
  configure reaches `Configuring done (12.7s)`. Seven cases in `tests/test_build_api.py`
  cover it.
- **Model versions, one engine build per quant**: `serve/quant_store.py` stores a build
  in `engine/engine_<quant>/` with its own `BUILD.json`, and `engine/VERSIONS.json`
  records which one is active. `engine/strata` is materialised from the active build
  (symlink on Linux, copy on Windows), so every existing run config keeps saying
  `engine/strata`. The header has a **Model version** dropdown fed by `GET /versions`
  and `POST /versions/select`: choosing a version places that build's binaries, points
  the engine at the run config that names that quant, and restarts it; with no engine
  loaded the version is set and the Load button starts it. The Build tab's *Build for
  quant* field is what names the quant a build is stored under. A missing or corrupt
  `VERSIONS.json` is rebuilt by scanning the engine folders in `engine/` (the older
  `engine/versions/<quant>/` layout is still read) and `config/`. Tests:
  `tests/test_quant_store.py` (30 cases), `tests/test_version_routes.py` (6 cases
  against a `--ui-only` server), `tests/test_version_dropdown.js`, and six cases in
  `tests/test_build_api.py` that build a fake `build/` directory and check that a named
  quant is stored in `engine/engine_<quant>/`, that two quants do not overwrite each
  other, that an unquantised build behaves exactly as before, and that the shared
  libraries a build links are found with the engine. Also fixed: `serve/build_api.py`
  used an undefined `UA` constant in the GitHub request headers, so every release-file
  fetch fell into its "could not reach GitHub" branch; the user agent is now defined.
- **A build installs itself end to end**: naming a quant in the Build tab now produces
  the whole deliverable, not just a binary. `serve/quant_store.py:ensure_run_config()`
  writes `config/run-<quant>.json` when none names that quant, and repairs one that does:
  `exe` to `engine/engine_<quant>/strata`, `log` into `logs/`, `cwd` out of the build
  scratch directory, and every runtime file the config names inside the clone (`data/expert-profile.bin`, the speed-projection GGUF) copied to `engine/engine_<quant>/data/` and rewritten, because `tmp/` is deleted when the build finishes. `libstrata_kq.so` and any other `libstrata*.so`/`libhypercore*.so` a build links are stored next to the engine, which is where strata dlopen's them from. Nothing a build writes is outside `~/HyperCore-WebUI/`: the clone and the cmake build in `tmp/`, the binaries in `engine/`, the config in `config/`, the log in `logs/`. `GET /versions` carries the engine folder and the run config for each version; the header dropdown re-reads it when a build or a ready-made install finishes and preselects the new one. `tools/build_engine.sh --quant <quant>` does the same from the command line.
- **A build that cannot produce an engine is named before it runs**: every GPU backend
  defaults to OFF in the release's own `CMakeLists.txt`, and upstream adds the `strata`
  executable only inside a GPU branch, so a from-source build started with nothing
  changed died in ninja with `unknown target 'strata'` and put nothing in `engine/`.
  `serve/build_api.py` now reads the machine: `nvcc` on PATH or a `/usr/local/cuda*`
  toolkit, the card's compute capability from `nvidia-smi` (falling back to the arch of
  the engine already installed), and shows those as the defaults in the Build tab -
  `STRATA_ENABLE_CUDA=ON`, `CMAKE_CUDA_ARCHITECTURES=<the card>`, and
  `CMAKE_CUDA_COMPILER=<that toolkit's nvcc>`, because CMake does not find nvcc when it
  is not on PATH. Before cmake is run the configured project's `build.ninja` is read, so a
  configuration with no engine target is reported as "the configured build has no engine
  target: turn on STRATA_ENABLE_CUDA / STRATA_ENABLE_HIP / STRATA_ENABLE_SYCL" rather
  than a ninja error, and a build that produces no binary is a failure rather than an
  empty store. Two bugs found on the way: `engine-src/` was copied into the clone but
  nothing in upstream's `CMakeLists.txt` ever read it, so `hypercore-worker` was never
  built - `wire_in_interface()` now adds `add_subdirectory(hypercore-interface)` to the
  clone; and `tools/vision` is its own project, so the image encoder is configured and
  built in `build-vision/` separately, its failure reported and the engine still
  delivered. A build named for a quant also writes `config/run-<quant>.json` when none
  exists: the nearest run config with the quant in its paths rewritten, plus a `_note`
  naming every path that is not on this box yet. Tests: `DeviceTests`, `EngineTargetTests`,
  `InterfaceWiringTests`, `VisionBuildTests` in `tests/test_build_api.py`, and
  `RunConfigTests` in `tests/test_quant_store.py`.
- **Tune tab**: `optuna_tune/bayesian_tune.py` as a background subprocess, with the
  chat engine unloaded first, live trial rows read from the CLI's own stdout, the
  winner's benchmark, and export of the run config pair. The pin boxes are filled
  from the search space, so a value the tuner would reject cannot be typed.
- **Priors**: `tools/import_tune_history.py` imports measured configurations from
  another HyperCore install, trimmed to the best N per objective. Seven priors from an
  i9-14900KF / RTX 4090 box are committed (228 KB); the Tune tab lists what run data a
  sweep would load and whether it matches this machine, with boxes for "start cold",
  "use priors from other hardware" and "reuse priors from other objectives".
- **Chat archiving**: the composer has a second save button. The download button
  writes the Markdown to the browser's downloads folder as before; the archive
  button posts the same transcript to `POST /history/archive`, which writes it into
  `data/history/_old_chats/` beside the transcripts the history builder reads
  (`serve/chat_archive.py`, `HYPERCORE_OLD_CHATS` to point it elsewhere).
- **The app survives an engine that will not start.** A GPU held by a sweep, a build
  or another server no longer takes the web app down with it: the server binds the
  port, serves the page, and reports the engine as offline. Chat is answered
  `503 engine_unavailable` with the reason and a `Retry-After`; a banner in the Chat
  tab says what is unavailable and offers Load. `--ui-only` runs the app with no
  engine at all, and `--engine-retry-s` (default 30, `"engine_retry_s"` in the run
  config) controls how often a failed engine is retried in the background until it
  comes back without restarting the WebUI (`serve/offline_engine.py`). While a tune
  sweep or a build has the card the retry holds off and says so: the retry status
  reports `paused` with the reason, and the banner offers "Try anyway" rather than
  promising a retry that would fail and disturb the measurement being taken. Archiving
  then starts `tools/build_history.py` in a daemon thread, so the transcript is
  distilled into `chats/`, `topics/` and `notes/` without the button waiting for it;
  a second archive while a rebuild is running does not start another. `GET
  /history/distil` reports the state, `POST /history/distil` runs one now,
  `HYPERCORE_AUTO_DISTIL=0` turns the automatic pass off and `HYPERCORE_DISTIL_SCRIPT`
  points the hook at a builder kept elsewhere.
- **The banner's red button stops the engine, not the app.** It reads "Stop Engine", arms
  on the first click ("Confirm") and stops the model, the image encoder and the MCP
  servers on the second (`POST /v1/stop`, the engine-only pair that already existed for
  the API and the Monitor). The port stays bound, every tab keeps working, and the same
  button turns into "Start Engine" (`POST /v1/start`) so the model comes back without
  restarting anything. `/health` now reports `engine.stopped` so the page does not paint
  a Stop label over an engine that is already down, and the offline banner's Load button
  starts a stopped engine rather than trying to load into a process that is gone.
  Closing the whole server moved to the Monitor tab under Model state, "Close this
  server", which is the same path Ctrl+C and SIGTERM take.
- **The engine still runs when the worker was not built**: `hypercore-worker` is
  HyperCore's process boundary, not part of upstream Strata. A server started with
  `--engine hypercore` and no worker binary now falls back to driving the engine over
  its own stdin/stdout protocol (`serve/direct_engine.py`) instead of exiting, and says
  so in the server output and in the Monitor's Model state card
  (`tests/test_direct_fallback.py`). `tools/build_engine.sh` and `tools/build_engine.bat`
  do the same at build time: if the worker target fails, `strata` and `strata-vision`
  are still built and `engine/BUILD.json` records `"worker": false`.
- **`/health` says what the engine is, not just that the server is up**: `status` is
  `"offline"` when no model is loaded, and `engine` carries `state`, a machine-readable
  `code` (`ui_only`, `gpu_busy`, `stopped`, `no_model`, `unloaded_or_compiling`) and the
  reason. Every non-generation route (`/config`, `/mcp`, `/build/status`, `/tune/status`,
  `/metrics`, `/v1/status`, the page itself) is verified to answer with no engine at all
  (`tests/test_offline_tabs.py`).
- **The Config view can fetch the model**: "Run Strata setup" in the Model settings card
  runs the upstream `setup.py` of a Strata checkout as a detached subprocess whose output
  goes to `logs/strata-hook.log`, and streams that log into the page from a byte offset.
  `POST /config/strata-hook`, `GET /config/strata-hook`, `POST /config/strata-hook/stop`
  (`serve/strata_hook.py`, `tests/test_strata_hook.py`, `tests/test_strata_hook_ui.js`).
  A checkout inside `tmp/` is refused: that folder is deleted when a build finishes.
- **The build log is tailed, not re-sent**: `GET /build/log?offset=<n>` returns only the
  lines written since that offset, and the same lines are written to
  `logs/build-<tag>.log` as the build runs (`serve/build_api.py`, `tests/test_build_api.py`).
- **`run.bat` takes the same flags as `run.sh`**: every `-` flag after the config name is
  forwarded to the server, and `--ui-only` is documented on both platforms
  (`tests/test_run_scripts.py` checks the pairs match).
- **Monitor history card**: the Monitor tab shows what is in the archive (transcripts, chat
  summaries, topic files, notes, the newest transcript, the last rebuild and its duration)
  and has a "Rebuild now" button that runs the builder on request. The card is hidden until
  the data arrives and disabled while a rebuild is in flight. `GET /history/status` supplies
  it, and the counts are of `.md` files only, so a helper script left in the folder is not
  reported as a transcript (`tests/test_monitor_history_card.js`).
- **Install-wide off switch for the end-of-turn check**: `HYPERCORE_NO_TURN_TAIL_CHECK` beats
  `--turn-tail-check` and `"turn_tail_check": true` in any run config, so a machine running
  several models can turn it off once instead of in every config. The decision is a single
  function, `serve/server.py:resolve_turn_tail`, and the server says out loud when the switch
  overrode a request (`tests/test_turn_tail_check.py`).
- **The Build tab had no scrollbar.** `body` is `overflow: hidden` and `main` is a fixed-height
  flex row, and only the Monitor, About and Assist views carried their own `overflow-y: auto`.
  The Build tab is taller than the screen as soon as the setup-choices and build-options cards
  fill in, so everything below the fold was unreachable. `.view--build` and `.view--tune` now
  scroll like the other tabs and take the same centred 1240px column and card spacing as the
  Monitor (`tests/test_build_tab_scroll.js`).
- **The Build tab's release dropdown was empty.** `GET /build/status` put the newest
  single release under `latest` while the page read `latest.releases`, so the box never
  filled and the note fell back to "could not reach GitHub". The route now returns the
  whole listing under `latest` and the newest release under `newest`, assembled by one
  function `serve/build_api.py:build_status`. The dropdown also carries `main (the current
  source)` as a target, so a machine with no network can still build, and the note says
  why GitHub gave nothing (`tests/test_build_api.py`, `tests/test_build_tab_releases.js`).
- **A harness for the end-of-turn check**: `tools/turn_tail_measure.py` runs one multi-part
  prompt twice, once against the server you already have running and once against a second
  server started in attach mode against that same model with `--turn-tail-check` on, so no
  second copy of a tens-of-GB model is loaded. It counts the `tail_check` events the stream
  actually emitted, the characters the answer grew by, and how many of the parts the prompt
  named were covered. `--self` runs the whole comparison against two mock engines, which is
  the GPU-free way to prove the wiring (`tests/test_turn_tail_measure.py`).
- **History builder, two determinism fixes**: `~34%` is an approximation, not a home path, so it
  is no longer rewritten to `/home/ai-user0/34%`; and a figure a chat took from an outside source
  is filed under "From outside this project" rather than "Measured on this machine", so the agent
  does not report a survey percentage as a local measurement.
- **Tool call cap**: `mcp.max_tool_calls` (default 500) bounds the tool calls a single
  request may run. `mcp.max_rounds` bounds the assistant turns that may emit calls, and
  one turn can emit several calls, so the round cap alone is not a call cap. Calls past
  the cap are closed as "not run" and the turn ends; the chat line now names the cap that
  was actually hit and how many calls ran. The shipped `config/mcp.yaml` sets 200 rather
  than the 500 default, with `condense_after` 4 and `condense_chars` 2400, and the comment
  above those lines says why and how to raise it.
- **Windows**: `run.bat`, `setup.bat`, `tools/build_engine.bat`.
- **Tests**: `tools/run_tests.sh` runs the pytest suite and the browser-free JS
  checks that read `serve/web/app.js` as text.
- **README**: each tab is described and then shown, with the screenshots in
  `data/media/` (the only files under `data/` that are committed).
- **Standing tasks in the system prompt** (`prompts/prompt.json`, v9): every chat
  carries two background duties, the request in front of you still coming first. One
  distils `data/history/_old_chats/` into `chats/`, `topics/` and `notes/` through
  `tools/build_history.py`; the other keeps the state files (`.mcp_test_state.json`,
  `data/history/notes/`, `CHANGELOG.md`) matching what has actually been done, with
  nothing left in a queue without a reason. Each has a written check that says when it
  is done. `tests/test_prompt_standing_tasks.py` checks the file parses, the tasks name
  paths that exist here, and the rendered prompt stays under the 20,000-character
  ceiling `serve/prompts.py` cuts profiles at.

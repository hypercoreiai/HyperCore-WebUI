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
- **Build tab**: version check against the Niko1221/Strata releases, ready-made
  download with SHA-256 verification, or a from-source build whose dropdowns are
  extracted from that release's own `CMakeLists.txt` and `setup.py` and diffed
  against the previous manifest. One clone in `tmp/` at a time; it is deleted when
  the build finishes and nothing at runtime reads it.
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
  was actually hit and how many calls ran.
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

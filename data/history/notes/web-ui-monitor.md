# The chat page and the Monitor

**One server, one page.** `serve/server.py` serves the app at `/` and the Monitor at
`/api-monitor`; `serve/web/index.html`, `monitor.html`, `app.js`, `app.css`, `tokens.css` and
`components.css` are the front end. The page is served by the same process that answers the API,
so it carries the server's API key in the HTML (`window.HYPERCORE_API_KEY`) and `app.js` uses it
when nothing is stored in the browser — a default-key install works without typing anything into
Settings.

**The Monitor reads `/metrics`.** `/metrics` returns the last 12 requests (`?requests=all` for
everything kept) plus a `history` block of rolling means. The cards are: Reused (the share of the
prompt the GPU did not have to read), Tok/s (decode rate, live while generating, otherwise the
last finished request), VRAM hit rate, and Prefill (the engine's own `prompt_read / prefill_ms`).
When a prompt is still being read there are no decode tokens yet, so the Speed card falls back to
the last completed request instead of showing a blank.

**Fields the engine does not report are derived, not left empty.** `serve/ipc_parser.py`'s
`derive_monitor_field` computes what the terminal summary omits from counters that are present.
A missing number in a card has always meant either the wire did not carry it or the Python side
did not look for it.

**The agent dropdown is built at run time from `prompt.json`.** Any top-level key that is not
`system_prompt` is an agent profile. The server finds that file beside itself or in `prompts/`;
a config that points at the wrong path is why the dropdown showed only "System prompt".

**Context-cap pruning is a server decision, not a client one.** A request larger than the model's
window is condensed before it reaches the engine; the page shows the condensing progress rather
than a dead button.

**A chat is saved two ways.** The composer's download button writes the Markdown to the browser's
downloads folder; the archive button next to it posts the same transcript to `POST /history/archive`,
which `serve/chat_archive.py` writes into `data/history/_old_chats/` as
`YYYY-MM-DD-HH-MM-<slug>.md`, the folder `tools/build_history.py` reads. The transcript is the
input of the history, not the output: the builder distils it into `chats/`, then into `topics/`,
and keeps `notes/` as the current state.

**Archiving runs the builder behind the response.** `serve/chat_archive.py` exposes
`run_builder()`, `queue_distil()` and `distil_status()`; the archive route calls `queue_distil()`
after the write, so the transcript is distilled without the button waiting. A rebuild already in
flight is not started twice, and `HYPERCORE_AUTO_DISTIL=0` turns the automatic pass off.

**The app serves when the engine cannot.** `serve/offline_engine.py` gives the server an
`OfflineEngine` stand-in so the port still binds and every tab that does not need weights works;
chat gets `503 engine_unavailable` with the reason and a `Retry-After`, and the Chat tab shows a
banner with a Load button. `EngineRetry` brings the engine back in the background
(`--engine-retry-s`, `"engine_retry_s"` in the run config, default 30 s). The retry is told when
the card is busy: `serve/server.py` passes `busy=card_is_busy`, which reads `tune_api.JOB.state`
and `build_api.JOB.state`. While a sweep or a build holds the GPU the retry makes no attempt and
`/health` reports `state: "paused"` with `paused_reason`; pressing Load in that window is refused
with that reason rather than thrashing the card.
**The banner's red button is the engine, not the app.** `serve/web/app.js` paints
`#shutdown-btn` from `/health`: "Stop Engine" while an engine is running, "Confirm" after the
first click, "Start Engine" when it is down. It calls `POST /v1/stop` and `POST /v1/start`, the
engine-only pair; the HTTP server stays up the whole time. `/health` reports `engine.stopped`
when the stop came from the app, because a stopped `DirectEngine` still has an object behind it
and would otherwise read as "running". The offline banner's Load button uses `v1/start` in that
case, since `ensure_loaded` raises `EngineStopped` rather than starting a process the user asked
to stop. Closing the whole server is the Monitor tab's `#close-server-btn`, which posts
`/v1/shutdown` and is the same path Ctrl+C and SIGTERM take.
`GET /history/distil` reports whether one is running and what the last returned; `POST
/history/distil` runs one immediately. `HYPERCORE_DISTIL_SCRIPT` points the hook at a builder kept
elsewhere.

**The Monitor has a Chat history card.** `loadHistory()` reads `GET /history/status`, which returns
the counts of transcripts in `_old_chats/`, of the generated `chats/`, `topics/` and `notes/` files,
the newest transcript name, and the distil state; `renderHistory()` fills the card and disables the
"Rebuild now" button while a rebuild is in flight. The card stays `hidden` until the first load, and
is refreshed with the MCP and Assist cards every tenth poll tick. The counts are of `.md` files only
(`count_markdown` in `serve/chat_archive.py`), because a helper script sitting in the archive folder
is not a transcript.

**The Build tab builds for the machine it is on.** A from-source build took the release's own
defaults, which are OFF for every GPU backend; upstream defines the `strata` executable only
inside a GPU branch, so the build died in ninja with "unknown target 'strata'" and nothing was
written to `engine/`. `serve/build_api.py:device_defaults()` reads the box (nvcc on PATH or a
`/usr/local/cuda*` toolkit, the card's compute capability from nvidia-smi, otherwise the arch of
the engine already installed) and those become the defaults the tab shows, including
`CMAKE_CUDA_COMPILER` because CMake does not find nvcc when it is not on PATH.
`BuildJob.configured_targets` reads the configured project's `build.ninja` before cmake runs: a
configuration with no engine target is named, and a build that produces no binary is a failure
rather than an empty store. **The configuration is also checked against the backend before cmake
runs.** Upstream's CMakeLists.txt FATAL_ERRORs on combinations it cannot build (v0.1.45 `:97`
`STRATA_PREFILL_MMQ` needs HIP, `:1413` it needs `STRATA_NATIVE_EXPERTS`, `:1612` the MMQ pointer
table is CUDA-only): `serve/build_api.py:BACKEND_REQUIREMENTS` and `resolve_build_options()` apply
the same rules to the defaults the tab shows and to the flags actually sent, two backends on at
once are reduced to the one the machine has a toolchain for, and a feature this box cannot build
says in its help what it would need. Seven cases in `tests/test_build_api.py`. `wire_in_interface()` adds `add_subdirectory(hypercore-interface)` to
the clone: `engine-src/` was copied in but nothing in upstream's CMakeLists.txt read it, so the
worker was never built. The image encoder (`tools/vision`, its own project) is configured and
built in `build-vision/`; its failure is reported and the engine is still delivered. A build
named for a quant also writes `config/run-<quant>.json` when none exists
(`quant_store.ensure_run_config`), with a `_note` naming the paths that are not on the box yet.

# The web app and the Monitor

Accumulated from 60 transcripts, 192 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

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


## What is true now

how the thing works, as the chats established it


**app.js** · 9 chats
- 3.2 tailed build log. `BuildJob.log_line` also writes to `logs/build-<tag>.log`, `BuildJob.tail(offset)` returns only new lines, and `GET /build/log?offset=N` uses it; `app.js` appends instead of re-rendering 400 lines every 3 s.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>
- If the garbling returns only with those three set, the engine is the cause; if it appears either way, it is the injected `Complete the open tasks` prompt and the `app.js` fix is the one that matters.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-22-15.md</sub>
- If the garbling disappears with these off and reappears with `HYPERCORE_MMVQ_IL=1 HYPERCORE_NAN_GUARD=1 HYPERCORE_ADAPT_OVERLAP=1`, the engine is the cause; if it persists either way, it's the injected `Complete the open tasks` prompt, and the `app.js` fix is the one that matters.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-22-03.md</sub>
- `bindTranscript()` refactor in `app.js` — the click/toggle handlers are currently hard-bound to `#chat` only.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>
- The `app.js` change is static and only needs a browser reload.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>
- New harness `serve/web/test_assist_thought_panel.js` (a small DOM stand-in; it reads `updateAssistant`, `transcriptOf`, `bindTranscript` and the actual `bindTranscript(...)` wiring lines out of `app.js`, so it fails when the app changes): 5 cases, 5 pass, exit 0 — panel opens and shows the reasoning in both transcripts, copy reads the right array, a missing container is a no-op.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>
- The new cases run the real `paintAssistTop`/`foldAssistTop`/`wireAssistTop` source out of `app.js` against the stand-in DOM: the summary names the selected model and count; a first visit stays open; closing is stored and restored on reload; a programmatic open does not overwrite the user's choice; an older page without the panel does not throw.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>
- Negative control: deleting `wireAssistTop();` from `app.js` makes 5 of the 10 fail — the harness is sensitive to the wiring, not just to the strings.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>
- New `serve/test_web_client.py` (8 cases) asserts the shipped `app.js`/`index.html` actually do these things.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-18-57.md</sub>

**serve saving does** · 6 chats
- Change, `serve/web/app.css:107-114` The second and third lines are the same reading column the Monitor uses: the Build and Tune cards were flush against the page edges with no spacing between them.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>
- So sentences like "serve/chat_archive.py writes the transcript to data/history/_old_chats/" survive.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>
- good: `hypercore serve: interleaved matvec: N copies built, M calls reused one (K calls)`
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>
- `node --check serve/web/app.js` → exit 0; `node serve/web/test_surface_slider.js` → all surface checks passed (the Surface slider from the previous turn is intact).
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>
- The git diff for serve/web/app.js is only 16 insertions.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>
- `serve/web/app.js:95`: Saving the key does not re-run `loadMcp()`.
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**prompt.json** · 5 chats
- Two things worth knowing. Only `prompt.json` is read, looked for beside the server first and then in `prompts/`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>
- And the catalog is re-read on file change, per `serve/prompts.py`: adding or removing a profile in `prompt.json` changes the number on the next page load, no restart needed.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>
- The profiles are read from `prompt.json` beside the server or `prompts/prompt.json` (the one this repo ships): adding a profile to that file adds a menu entry with nothing to change in the page or the code.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>
- `prompt.json` beside the server is one JSON object: a "system_prompt" that every chat starts with, and one entry per agent after it.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md · also in 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>
- Nothing is committed to git yet (`prompt.json`, `serve/prompts.py`, `serve/test_prompts.py` are new; `serve/server.py`, `serve/web/`, README and docs are modified).
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**data/history/** · 4 chats
- The distillation that exists in `data/history/` predates this session, and it is stale.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>
- `data/history/` holds topics, notes, chats, `index.json`, and `sources.md`; I read it before re-deriving a fact rather than re-searching.
  <sub>2026-10-14 · hypercore-chat-2026-10-14-16-25.md</sub>
- If you want `data/history/` in the repo, I need write access to that directory, or you can grant the secret at the GitHub URL above and I'll re-commit it as-is.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>
- What the permission fix let me do `data/history/` is now `ai-user0:dev-sandbox` and writable.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**/home/ai-user0/hypercore-webui/data/history/_old_chats** · 3 chats
- For comparison, the transcripts in `/home/ai-user0/HyperCore-WebUI/data/history/_old_chats` do show real duplication — `hypercore-chat-2026-10-13-08-16.md` has `continue` as a user turn 14 times, and one question repeated 3×.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md · also in 2026-10-06-strata-chat-2026-10-06-18-22.md, hypercore-chat-2026-10-11-17-31.md</sub>
- One thing you should decide on. The previous tree was built from `/home/ai-user0/HyperCore-WebUI/data/history/_old_chats` (78 transcripts, 106 files).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>
- That drops `context.txt`, `end_hypercore.txt`, `strata_upgrades.txt` and `Text File.txt` from `/home/ai-user0/HyperCore-WebUI/data/history/_old_chats`, which are notes rather than transcripts.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**full check probe** · 3 chats
- When the card is free, these three runs decide it, and the per-request line `interleaved matvec: N copies built, M calls reused one` is the evidence: If `CHECK` names a shape, the trace line identifies which write the guard missed; if it reports all equal, `MMVQ_IL` can be turned back on for the speed.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-53.md</sub>
- Actually, let me check whether Assist.png is referenced in serve/web/index.html or docs.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>
- A full "Check lag" probe ran through the live route: 80 models in 46.5 s, and the result written to the same file the dropdown reads.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**prompt json plain** · 3 chats
- Prompt and tool-output costs grow non-linearly, so read narrowly: query the specific key in a config instead of dumping the file … [universal] /home/ai-user0/_Strata_/prompt.json … 144 tokens (max_context=262144).
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- The prompt cache does resume. From turn 3 on, 52 of the 57 tokens are reused and only 5 are read.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- prompt.json is a plain JSON file: a "system_prompt" object that every chat starts with, and any number of other top-level objects, each one an agent profile ...
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**prompts/prompt.json** · 3 chats
- `payloadChars()` (app.js:1120) counts every message's content, and app.js:1330-1331 `unshift` the profile system turns into the payload, so `prompts/prompt.json`'s `system_prompt` (5,804 chars, ~1,451 tokens) and an agent profile (~2,200 chars, ~550 tokens) are counted by the character term, not the constant.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>
- The message comes from `serve/web/app.js:623`: That runs when the page fetches `GET /prompts`, which `serve/prompts.py` builds from `prompts/prompt.json`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>
- Cause. The dropdown is built at run time from `GET /prompts`, and that endpoint reads `ROOT / "prompt.json"` — but this repo ships the profiles in `prompts/prompt.json`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md · also in 2026-10-11-hypercore-chat-2026-10-11-06-34.md</sub>

**text home user0** · 3 chats
- `tools/build_history.py:63-64` hardcodes `Path("/home/ai-user0/HyperCore-WebUI/data/history/_old_chats")` and `Path("/home/ai-user0/HyperCore-WebUI/data/history/_old_chats")`; `:314` falls back to `"/home/ai-user0/0"` when `HOME` is unset.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>
- The … [universal] /home/ai-user0/_Strata/docs/AMD_HIP.md … Since 0.1.39 a tuning run by hand (`./setup.sh --calibrate`) is saved for the AMD card it ran on and reused when setup runs again.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md · also in hypercore-chat-2026-10-13-08-16.md</sub>
- text /home/ai-user0/Strata/zprompt.system_prompt.json json "system_prompt": "You are the Zed coding agent running inside the Zed editor.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-01-06.md · also in 2026-10-06-strata-chat-2026-10-06-01-06.md</sub>

**/home/ai-user0/hypercore-webui** · 2 chats
- So `/home/ai-user0/HyperCore-WebUI` IS the previous incarnation of this same project (HyperCore-WebUI): it has serve/, tools/, tests/, data/history/, optuna_tune/, prompts/, engine/.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>
- Confirmed: `/home/ai-user0/HyperCore-WebUI` is the previous home of this same project (serve/web identical structure, same data/history).
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**also need ensure** · 2 chats
- Also need to ensure the builder writes into this repo's data/history by default: HIST = Path(__file__).resolve().parents[1] / "data" / "history" → tools/build_history.py is at HyperCore-WebUI/tools/..., so parents[1] = HyperCore-WebUI.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>
- Also update data/history/README.md (the builder writes it) to describe the pipeline including _old_chats → chats/ → topics/ → notes/.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**assist** · 2 chats
- But I can check the assist card text in index.html to see what the Assist page does.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>
- There is no server-side record of Assist traffic: `strata-q2_0.log` has zero `assist` entries, `telemetry.py` has none, and the Monitor has no Assist card.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-12-32.md</sub>

**data/history/_old_chats/** · 2 chats
- Add a second button beside the existing Save this Chat Markdown button, and add a backend endpoint that writes the chat text to `data/history/_old_chats/`.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>
- Run the test after applying the patch: If the WebUI is not FastAPI, the same function can be attached to the existing request handler; the important part is the `save_chat_text_to_old_chats` function and the target directory `data/history/_old_chats/`.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**live engine 3318870** · 2 chats
- That is the same number the engine prints beside its hit rate (`generate.cpp:10435-10439`).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md · also in hypercore-chat-2026-10-16-23-49.md</sub>
- Live engine pid 3318870 reports `interleaved matvec: 205 copies built, 98 calls reused one (303 calls)` and no `CHECK FAILED` line
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>

**mmvq_il** · 2 chats
- Status. The `MMVQ_IL` work is done and verified twice: `interleaved matvec: 367 copies built, 87 calls reused one (454 calls) - compared with native_mmvq, all equal`, identical build/reuse counts across runs including the T=1 window (0 builds, 49 reuses).
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>
- The `MMVQ_IL` guard holds again in these runs: `interleaved matvec: 367 copies built, 87 calls reused one (454 calls) - compared with native_mmvq, all equal` (fifth and sixth consecutive clean comparisons).
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**reuse path exercised** · 2 chats
- But test_stop_start_buttons uses path.join(WEB, "app.js") — path is imported there.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>
- The reuse path is exercised, not bypassed — the 3-token window built 70 copies and reused 17; the 1-token window built 0 and reused 49 (the `ncols==1` path reads the existing copy, and the check still compares it).
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**reused** · 2 chats
- The request table's `Reused`, `Tok/s` and `VRAM hit rate` columns are populated.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>
- Native build check: Result: passed, including the ABI alignment check: So the Monitor tab’s `Reused`, `Tok/s`, and `VRAM Hit Rate` columns are no longer empty placeholders; they are fed from the worker terminal frame and rendered by the web app.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-19-47.md</sub>

**wait maybe build_history** · 2 chats
- But wait: maybe the build_history.py writes to data/history and it was built by root previously (files owned by root, dated Oct 11 10:24).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>
- But the change needed is in build_history.py.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**--out**
- Try to rebuild; if data/history is not writable, report it and offer the `--out` path.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**--prompt-tokens**
- Trial 1 of the run you posted (verbatim from `results/maya-q2_0.record.json`, now at the 32768 default): Then on stdin, once the session is up: (the ids are `fixtures/prompt.json` cycled to `--prompt-tokens`, default 10240).
  <sub>2026-10-15 · strata-chat-2026-10-15-09-53.md</sub>

**--prompt-tokens-file**
- The fixture prompt (`optuna_tune/fixtures/prompt.json`, a 20-token id cycle) makes the model EOS at 292 tokens on every rung except 2048, so a real prompt via `--prompt-tokens-file` is what gets the decode past the 540-token line.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**--prompts**
- `serve/server.py:637` and `:2013` set `prompts_path` to `prompt.json` in the repo root, overridable only by `--prompts` or the run config's `prompts` key.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**-rwxrwxr-x**
- I could not redact them: `data/history/` is root-owned and every write to it returns `Errno 13 Permission denied` for this account, despite the `-rwxrwxr-x` mode.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**./run.sh**
- The run needs the GPU, so I did not start it: The WebUI on port 8081 (PID 336066) still predates the paused-retry code and the Monitor history card, so `./run.sh` needs a restart for those to show.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>

**.gitignore**
- `.gitignore` has `data/` with only `data/media/.png` re-included, so `data/history/` (the 225-transcript archive and `index.json` that standing task 1 depends on) is machine-local and lost on a clone.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**.mcp_test_state.json**
- `.mcp_test_state.json` is at loop 16, parses (`json.load` ok, 38 completed, 9 queue items), and the archive check holds: `index.json` 210 transcripts equals 210 files in `data/history/chats/`, 13 topics, newest transcript 10:47 matched by a build at 10:47.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>

**/api-monitor**
- Root cause (`serve/server.py:3629`, the `/` and `/api-monitor` handlers): Replacing `<title>` with the script deletes the opening tag, so the served head becomes: Confirmed against the live server: The browser parses `HyperCore` as a stray text node in `<head>`, which is why it shows up in the top-left corner and pushes the banner down.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**/health**
- Fix: derive the paths from `ROOT`/`XDG_CONFIG_HOME`, commit `data/history/` (or a `data/history/seed/` subset like the tuner does with `seed-.json`), and add one `__version__` that `/health` reports.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**/home/ai-user0/hypercore-ccp/serve/server.py**
- The same wrong path is in `/home/ai-user0/HyperCore-CCP/serve/server.py`, which also has no root `prompt.json`; say the word and I'll apply the fix there too.
  <sub>2026-10-11 · 2026-10-11-hypercore-chat-2026-10-11-06-34.md</sub>

**/home/ai-user0/hypercore-webui/data/history**
- `data/history/` in this repo is a byte-identical copy of `/home/ai-user0/HyperCore-WebUI/data/history` (`diff -rq` on the two `_old_chats` folders returned no differences), and the builder that produced it is not in this repo: it is `/home/ai-user0/HyperCore-WebUI/tools/build_history.py`, 538 lines.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**/home/ai-user0/hypercore-webui/serve**
- The WebUI in `/home/ai-user0/HyperCore-WebUI/serve` is a single-page app served by `serve/server.py` (no CDN, same-origin): `index.html` + `app.js` (116 KB) + `app.css`/`components.css`/`tokens.css` + `sprite.svg` + Outfit font.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/metrics**
- Monitor reads `/metrics` for sparklines (speed, GPU load, VRAM, temp, power, PCIe, CPU, disk), context-fill gauge, request table, conversation cache, MCP switches, Assist log.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/prompts**
- `serve/web/app.js` (1,073 → 1,148) — agent-profile dropdown built from `/prompts`, API-key save re-runs `loadMcp()`/`loadPrompts()`, tool heartbeat in the composer hint, context-full 400 explained instead of shown raw.
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>

**/tok**
- Per MoE route of `K` experts, `glm_fast_path.cu:3700–3776` partitions every routed expert into exactly one bucket: So the reported quantities are counts per token (`ram_fetch`, `disk`, `promo`, `cpu_experts` — the dashboard's unit is literally `/tok`, `app.js:263-277`) and milliseconds per token (`disk_ms/tok`, `cpu_ms/tok`).
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**192.168.74.9**
- `serve/web/app.js:161` polls it continuously; your dashboard at `192.168.74.9` was polling it every few seconds, which is why the identical traceback appears ~20 times.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**4daf35b**
- The engine fix (`4daf35b`) The root cause of the garbling was a stale interleaved copy: `xil_` is built for one `(n_in, ncols)` of one group's activations and reused for calls sharing it.
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>

**_old_chats**
- (The five sub-500-byte files in `_old_chats` are builder stubs that already say "nothing worth keeping", so nothing real is lost there.)
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**_old_topics**
- `data/history/_old_chats` and `_old_topics` (the pre-scrubber tree, 78 root-owned files) are still on disk and still gitignored.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**_read_path**
- `_writable` now returns a writable sibling with the content copied, so a read-only file can't empty the dropdown, and `_read_path` picks the newest existing copy for reading.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**_request_last**
- `prompt_read` / `prefill_ms` are parsed from the terminal summary in `serve/ipc_parser.py` (`derive_monitor_field`), carried through `_request_last` into the request record, and rendered in `serve/web/app.js`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**actually first double**
- Actually, first let me double-check the README's "The web app" section claims are accurate by checking index.html for the Monitor cards and the Assist tab.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**ai-user0**
- Blocker, and it is not a code problem. I could not rebuild `data/history/` in place: `data/history/` and the files inside it are root-owned; this account is `ai-user0`/`dev-sandbox`, so every write fails.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**app.css**
- Both UIs are the same four-file kit (`index.html`, `app.js`, `app.css`, `components.css`, `tokens.css`, `monitor.html`, `monitor.js`, `sprite.svg`).
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**archs**
- GPU generation. `archs` is taken from the actual cards (`archs = sorted({int(x) for x in gpu.get("archs", [gpu["arch"]])})`, `setup.py:2891`) and passed as `-DCMAKE_CUDA_ARCHITECTURES={cuda_archs}` (`setup.py:2910`).
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**assignment**
- `mypy` reports 58 `union-attr` and 52 `assignment` errors, concentrated in the deferred-engine block: One variable is reused for an engine, a request handler, and `None`.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**assist_defaults**
- `serve/assist.py` now has its own defaults, documented as deliberately independent of the run config: `converse()` takes `max_tokens=None` and falls back to `ASSIST_DEFAULTS`, and `serve/web/app.js` carries the same note at the call site so nobody wires the two together later.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**assist_mcp.py**
- Real output from the card after two sends: The MCP bridge writes to the same file — I ran `assist_mcp.py` standalone and its call appeared in the log, so Monitor visibility covers both paths.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-12-32.md</sub>

**better keep absolute**
- Better: keep absolute as /home/ai-user0/HyperCore-WebUI/data/history/_old_chats since it exists and is a real source folder.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**body**
- Fixed: the Build tab had no scroll rule. `body` is `overflow: hidden` and `main` is a fixed-height flex row (`serve/web/app.css:5-6`), so a tab is only scrollable if it carries its own `overflow-y: auto`.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md · also in hypercore-chat-2026-10-11-08-01.md, strata-chat-2026-10-04-06-37.md</sub>

**build_history.py**
- `build_history.py` now runs `scrub()` on note text too.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**calls**
- Accounting. `hub.usage` tracks `calls`, `chars`, `dropped_chars`, `truncated`, `errors`, `condensed_results`, `condensed_chars`; the answer's meta line appends `N tool calls` (`app.js:1424-1425`), and any tool left in `writing`/`running` when the stream ends is marked `skipped`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**changelog.md**
- `CHANGELOG.md`, `README.md`, `serve/quant_store.py`, `serve/server.py`, `serve/web/app.js`, and 4 new test files (`conftest.py`, `docs/strata-0.2.0-adjustments.md`, `tests/test_build_off_flags.js`, `tests/test_build_source_choices.js`) are untracked or modified.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**changes session limited**
- The web UI changes in this session are limited to app.js (2 changes).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**chat_filename**
- `tests/test_build_history.py`, 16 tests: alias resolution, "no tilde survives", "every rewritten path exists on this machine", engine/model paths stay absolute, relative repo paths untouched, identical transcripts counted once, sentence filter keeps a concrete finding and drops reasoning/questions/tool output, `kind()` filing, `chat_filename`.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**clean**
- Also `clean` and `kind` are in build_history.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**content**
- Everything I can see proposed in this thread is already in the working tree: the `renderAssist`/empty-placeholder fix (app.js:1449-1464), the user-bubble `text`/`content` split, truncation capture (`converse()` → `finish_reason` → `truncated`), the `over_limit` table, the probe panel, the Monitor card, the 500-line log.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**could note notes**
- I could add a note in notes/web-ui-monitor.md by hand documenting the new button (hand-written current state, which the builder keeps).
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**cp_blackbox.sh**
- `unzip -l | grep -c build-cov` returns 0, and the source files are all present (`serve/web/app.js`, `serve/hypercore_supervisor.py`, `src/platform/memory_test.cpp`, `serve/test_fatal_recovery.py`, `cp_blackbox.sh`).
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-20.md</sub>

**data/history/_old_chats**
- History distillation (`f7df2ce`) `tools/build_history.py` now reads `data/history/_old_chats` by default: 169 transcripts → 13 categories, 1044 entries.
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>

**data/history/chats**
- Actually earlier: `data/history/chats` is `drwxrwxr-x 2 root root` and files are `-rwxrwxr-x root root`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**defaults**
- Also worth checking: `DEFAULTS` in `app.js:597` sets `ctx: 132000`, but if your Chat-tab Context cap field is empty or 0, `pruneToContext` returns the payload untouched ("no cap").
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-12-05.md</sub>

**expected interleaved matvec**
- Expected: `interleaved matvec: N copies built, M calls reused one`, no `MMVQ_IL CHECK FAILED`, and clean text at `MMVQ_IL=1`.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**extract_history.py**
- The tool sources (`tools/build_history.py`, `extract_history.py`, `scan_chats.py`, `test_build_history.py`) are committed; the generated tree is not.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**fast**
- Two separate causes, both real: (a) The toast reported `fast`, the dropdown is `kept`. `probe_models()` (serve/assist.py:505-512) builds `kept = fast + held`: a model already on the allowlist is not dropped when the endpoint 429s or times out, because a throttle says nothing about its speed.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**filter.file**
- The Monitor card does surface this (`filter.file` + the "not writable by this user" note), and `_shared_lines()` (assist.py:311) unions the two call logs so the history is one history.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**fixtures/prompt.json**
- `token 248045 out of range [0, 154880)` — `fixtures/prompt.json` is another model's vocabulary.
  <sub>2026-10-15 · strata-chat-2026-10-15-16-58.md</sub>

**hashlib**
- Now `hashlib` must be imported in build_history.py.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**health.images**
- If your client or browser tab was opened against an `ni` instance (or an old tab still holding a cached health response), it will say images off — the UI's "Images on/off" line is read straight from `health.images` (`serve/web/app.js:340`), which is `svc.vision is not None` (`serve/server.py:882`).
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-36.md</sub>

**hypercore-webui/data/history/_old_chats/**
- The archive button writes to `HyperCore-WebUI/data/history/_old_chats/`.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**il_builds**
- bad: `hypercore serve: MMVQ_IL CHECK FAILED: X of Y interleaved calls differ from native_mmvq (Z values):` followed by ` Q4_K 6144 x 4 columns -> 2560: 3 of 120 calls differ, 7 values` `il_builds` ≈ `il_uses` means every call rebuilt its copy — the shapes alternate and nothing is reused, which is the signature of the stale-copy bug the `il_fresh(n_in, ncols)` guard fixes.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**il_ncols**
- The counters (`N copies built, M calls reused one`) will show whether `il_ncols` is flipping every call, which would mean I've missed a write to `xq_`.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**index.html**
- Against the server running on 8081: So `renderAgents()` in `serve/web/app.js` has nothing to add, and the only entry left is the one hard-coded in `index.html`: "System prompt only".
  <sub>2026-10-11 · 2026-10-11-hypercore-chat-2026-10-11-06-34.md</sub>

**innerhtml**
- `serve/web/app.js` is 3,098 lines with 45 `innerHTML` assignments, so there is real behaviour to test.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**j.strata_mcp**
- `serve/web/app.js:1151` still reads `j.strata_mcp`.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-03-30.md</sub>

**kimi pinned list**
- kimi-k3 is in the pinned list at 0.692 s, so you should see it in the dropdown now.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**logicprompt.json**
- The other files in that folder, `logicprompt.json`, `zedprompt.json`, `zprompt.json`, `many_prompt.json`, are source material and are not served.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**many_prompt.json**
- Both `prompt.json` and `many_prompt.json` are untracked (`??` in git status), so there is no committed version to fall back to.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**messages**
- On load or New Chat, insert a system message into the chat showing the injected system prompt + selected agent prompt (rendered as a collapsed card), and set the `messages` array to include `{role: "system", ...}` so apiMessages() includes it as the first message in the API request.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**ms/tok**
- It changes tok/s by amortising the boundary, so watch it against `ms/tok` and `disk reads/tok` together — a bigger window raises hit rate and lowers per-token transfers.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**needs conditions transcript**
- That needs the conditions from the transcript: a ~90k-token shared prefix (`prompt 88903 tokens = 0 reused + 89031 read` is in the log right now from the other client), thinking enabled, and `reasoning_budget_tokens: 24756` reached — at which point `serve/server.py:2682` injects `"\n\nI have thought about this long enough; time to give my answer.\n</think>\n\n"` into the resumed prefix.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**phone open monitor**
- On the phone: open the Monitor tab once (it calls `loadMcp()` at `app.js:80`), confirm it reads `35 tools · 5 of 5 servers connected`, then return to Chat.
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**probe_models deliberately keeps**
- `probe_models()` deliberately keeps already-pinned models whose probe failed (`serve/assist.py:454`), so the dropdown stays at 12 while the fast count drops to 11 So the popup was reporting two different numbers as if they were one.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**prompt_tokens**
- Estimation without a tokenizer. The browser has no BPE, so the count is anchored on the `prompt_tokens` the server reported for the last request (`app.js:1409-1414`), then adjusted by characters: `estimateTokens()` = `anchor.tokens + (chars - anchor.chars) / CHARS_PER_TOKEN`, with `CHARS_PER_TOKEN = 4` and `PREFIX_TOKENS = 6200` for the tool block plus system prompt the template adds.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**prompts**
- Server: add an endpoint `prompts` (GET) that reads prompt.json and returns the system prompt plus a list of agent profiles (key, name, role, description/capabilities).
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**prompts/logicprompt.json**
- `prompts/logicprompt.json` is left untouched.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-13-18.md</sub>

**rebuild history python3**
- Then try to rebuild history: `python3 tools/build_history.py --topics` (no writes) to verify the new source is read.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**renderassistprobe**
- `serve/web/app.js` — `renderAssistProbe` The `group("Not served by this endpoint", unavailable, …)` block is gone; a single line replaces it: > `56 model(s) the endpoint does not serve are not listed: the chat call answers 404 for them, so there is nothing to measure.` Why keep the count.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>

**returns dynamic level**
- Returns: Dynamic: any top-level key in prompt.json is treated as a profile; "system_prompt" is the base; everything else is an agent.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**running server port**
- The running server on port 8081 already serves the new rules: `curl http://127.0.0.1:8081/web/app.css` returns them at lines 111-114.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**serve/frontend.py**
- And if you want, I can look at how the system prompt is assembled in `serve/frontend.py` / `src/program/generate.cpp` to find where such a line would have to go for it to actually take effect.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-40.md</sub>

**serve/server.py**
- No code change was needed; the dropdown is built from the endpoint, and no agent key is hard-coded in `serve/server.py`, `serve/web/app.js`, or `serve/tool_permissions.py`.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-13-18.md</sub>

**serve/test_assist.py**
- `serve/test_assist.py`, `serve/test_monitor.py`, `serve/test_frontend.py`: 44 passed (18 subtests), including four new `SharedPaths` tests covering the "file owned by the other account" case, the chmod-to-666 convergence, and "Check lag writes to the file the dropdown reads".
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**serve/test_mcp.py**
- If you want, I can patch `app.js:95` to call `loadMcp()` after the key is saved — a one-line change, covered by `serve/test_mcp.py`.
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**serve/web/app.js**
- `serve/web/app.js` fills it from three endpoints, and only one of them is the config: `loadConfig()` is explicit about it:
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**simplest change inserted**
- Simplest: change the inserted line to use a computed string without path: `const WEB = __dirname + "/../serve/web";` That works with string concatenation for `WEB + "/app.js"`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**stop_reason**
- `stream()` yields `(token_id, prompt_reused, summary)` where `summary` is a decoded dict with `stop_reason` already normalised to `"limit"`/`"cancel"`/`"eos"`.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**summary.prompt_reused**
- Requests are the unit; conversation reuse is inside the box, observable through `summary.prompt_reused`.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-18-55.md</sub>

**switch browser settings**
- The switch is off in that browser. Settings live in that browser's localStorage: `settings = {...DEFAULTS, ...store.get("sampling", {})}` (`app.js:479-480`).
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**system_prompt**
- The count is simply the number of top-level entries in that file (anything not starting with `_` and not `system_prompt`), and the same list fills the composer's dropdown, the one labelled "Give this chat an agent profile (from prompt.json)", whose default is "System prompt only".
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**test_a_model_the_endpoint_does_not_answer_for_is_not_listed**
- `python3 -m pytest serve/test_web_client.py serve/test_monitor.py serve/test_assist.py` → 55 passed, 8 subtests, 0 failed, including a new `test_a_model_the_endpoint_does_not_answer_for_is_not_listed` that checks each of the four filter sites and asserts the old group is absent.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>

**tools/build_history.py**
- The text written is byte-for-byte the Markdown the download button produces, including tool-call `<details>` blocks and thinking, so `tools/build_history.py` reads it the same way.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**want versioned ignore**
- If you want it versioned, the ignore rule needs `!data/history/`.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**write test builder**
- Now write a test for the builder: tests/test_build_history.py covering relocate(), dedup, chat_filename, and that the topic extraction keeps a concrete fact and drops a vague one.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**app.js** · 5 chats
- A "Run Strata setup" button plus a Stop button and log panel were added to the Model settings card and wired in `app.js`.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>
- The two UI items from the investigation are still open and unfixed: `app.js` re-sends `RESUME_PROMPT` even when the last turn is already that prompt, and `turn_tail_check: true` appends `TAIL_CHECK_PROMPT` up to 3× per turn.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-44.md</sub>
- Live check (just now): The served page contains `assist-card`, `assist-chat`, `assist-probe-results`, and `app.js` has the seven assist hooks — so the UI you're looking at is the patched one.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>
- The served `app.js` on 8081 is the fixed file (89,842 bytes, probe panel present); the tab you were reading still holds the pre-fix script — hard-refresh it (Ctrl+Shift+R).
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>
- Negative control: the same harness against `app.js` with `bindTranscript($("assist-chat"));` removed fails on `#assist-chat is not bound` and `opening the panel left it empty in #assist-chat` — so the test is not vacuous.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-16-17.md</sub>

**home user0 strata** · 2 chats
- They then say they changed the source to /home/ai-user0/HyperCore-WebUI/data/history/_old_chats and there are new files to add.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md · also in hypercore-chat-2026-10-11-17-31.md</sub>
- /home/ai-user0/Strata/prompt.json The dropdown menu shuould populate dynamically so that additional agent profiles can be added.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**./prompt.json**
- `serve/prompts.py`: added `PROMPT_FILE` / `PROMPT_PLACES = ("", "prompts")` and `find_prompt_file(root)`, which returns the first of `./prompt.json`, `./prompts/prompt.json` that exists.
  <sub>2026-10-11 · 2026-10-11-hypercore-chat-2026-10-11-06-34.md</sub>

**.gitignore**
- So I dropped `data/history/` from the commit and added it to `.gitignore` with a note that it is generated locally by `tools/build_history.py`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**api_key**
- The "FREE" claim — qualified it and added the missing Assist bullet: I verified those details against the code rather than guessing: `serve/assist.py` reads `API_KEY`/`NVIDIA_API_KEY` and raises if absent; `serve/assist_fast_models.json` pins 14 models with `max_lag_s: 1.0`; `serve/web/app.js` has the "Show every model" toggle and the "Check lag" probe.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-31.md</sub>

**browser**
- Switching agent profiles, adding an MCP server (I added `browser` this session — the tool catalog is part of the system prompt), or changing reasoning effort when it's rendered at the top of the prompt.
  <sub>unknown · unknown-context.md · also in strata-chat-2026-10-07-09-31.md</sub>

**chats/**
- The `topics/` and `chats/` subdirectories were still root-owned, so I renamed them aside and created fresh ones I own; `notes/` is still root-owned but the builder only reads it.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md</sub>

**data/history/_old_chats/**
- Live check against the mock engine (`python3 -m serve.server --engine mock --port 8124`): I deleted that test artifact from `data/history/_old_chats/` afterwards, so the history folder is unchanged apart from the new capability.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**expert_logic_reasoning_agent**
- Added as `expert_logic_reasoning_agent`, the last entry in `prompts/prompt.json` (file now 24,057 bytes, valid JSON, 8 top-level keys).
  <sub>2026-10-12 · hypercore-chat-2026-10-12-13-18.md</sub>

**expert_x_comment_agent**
- What the test asserts. `serve/test_prompts.py:132` checks the live file against a hard-coded list: What your file has now. `prompt.json` is at `_meta.version: 5`, "v4: added expert_x_comment_agent", so `catalog()` returns four profiles: the three the test expects plus `expert_x_comment_agent`.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**files chat transcripts**
- The new files are the 4 chat transcripts in /home/ai-user0/HyperCore-WebUI/data/history/_old_chats Those are outside the repo (in /home/ai-user0/HyperCore-WebUI/data/history/_old_chats).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-17-31.md · also in hypercore-chat-2026-10-11-17-31.md</sub>

**held**
- The probe response now carries `previous`, `removed`, `held`, and `pinned` separately, and `renderAssistProbe()` (app.js:1401) names each model that left and why.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**prompt.json**
- A silent refresh fires when the menu is focused and when New Chat is pressed, so a profile added to `prompt.json` while the page is open reaches the menu without a reload.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-56.md</sub>

**prompt_reused**
- The Monitor path is now connected end to end: The populated fields include: I also found and fixed a real bridge bug: the terminal frame was discarding `prompt_reused`.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-19-47.md</sub>

**py_compile**
- `node --check serve/web/app.js` ok; `py_compile` clean; ruff reports nothing on any changed line (3 pre-existing findings elsewhere).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md · also in hypercore-chat-2026-10-11-07-09.md</sub>

**serve/assist_fast_models.json**
- Why the lag test "changed nothing". Two separate lists, both current: the dropdown is the pinned list (13 of 80, `serve/assist_fast_models.json`, measured 14:42), the long list is the full catalog behind "Show every model" (80) plus the "Probe results" panel naming the 67 left out and why (slow / 404 / throttled).
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**there file monitor**
- There is now one file, and the Monitor card prints the merge note (`merged 2 copies of … into …; removed …`) in the "Path" fact when a merge happens.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**tools/test_build_history.py**
- 69 such duplicates removed, `chat_filename()` fixed, and `tools/test_build_history.py` covers both: 27 passed, 13 subtests.
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>

**web_mcp.py**
- SSRF guard reused, not reimplemented. `public_url()` is the same function `web_mcp.py` uses, so both servers refuse private/loopback hosts identically.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**--decode** · 2 chats
- Both gaps are real: `thinking_tok_s`/`answer_tok_s` are `0.00` in all 10 rows; the `--decode` winner CSV (2026-10-16_004542, 9.04 tok/s, 7 rows) has `off_card_ms_per_tok`, `transfer_share`, `compute_tok_s`, `disk_reads_per_tok` all 0/empty.
  <sub>2026-10-16 · strata-chat-2026-10-16-02-29.md</sub>
- Run it when the card is free and compare the winner against your `--decode` winner; the 8.78 tok/s config (55% bus) would score above the 8.91 one (61%), so the ranking will change.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**prefix reuse long** · 2 chats
- Fix the long-context routing miss. 94.1% hit rate at 68k context vs 98.7% at 67 tokens is the measurable gap.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-16-40.md</sub>
- Prefix reuse is why long sessions stay fast: 128,894 of 128,934 tokens reused, only 40 read in 204 ms.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**--resident-experts**
- The Q2_0 arena is 34.0 GB on a 32 GB PC, so setup chose `--resident-experts` — and it's working: the log reports "0 blob reads from the file" and an 87–92% expert hit rate.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**bench-...-20261016t230645.json**
- From `bench-...-20261016T230645.json`, rung by rung: The 512 rung decoded one token: `gen 13.49 tok/s` is the length of the model's answer, not a decode speed, and the 61% off-card is the cold first token, not a steady state.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md</sub>

**defensible statement workspace**
- The defensible statement from this workspace is narrower: on this RTX 4090 the working configuration is `--pcie-frac 0.00 --spec-min-p 0.50 --pool-workers 10` (recorded in `2026-10-03/04/08-community-rtx4090-iq3xxs-200k-{code,ru}/env.json`), with decode in the 135-155 tok/s band, and `STRATA_KV_PREFETCH=1` and `"parallel": 2` both measured as losses here (2-4% and 28% respectively).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>

**live decode throughput**
- From the live log, decode throughput against prompt length: Expert cache hit rate is 97.5–98.3% across that range, with only 1.3–2.7% of routed experts read over PCIe — at ~785 KB per blob that is well under 1 GB/s, nowhere near the PCIe limit.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

**reserving 1024 instead**
- Reserving 1024 instead takes ~324 MiB off that — roughly 2.3% fewer slots, so the hit rate (currently 87–92%) will drop slightly.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**telemetry internally consistent**
- Telemetry is internally consistent for the winner: 1000/9.54 = 104.8 ms/tok, 60.72 ms off-card = 57.94%, card-alone 22.69 tok/s, and 16.52 reads × 3.652 ms = 60.3 ms of that 60.7 ms.
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>

## Gotchas

things that bit once and will bite again


**/home/ai-user0/hypercore-webui/data/history/_old_chats**
- The builder's default sources are `black_box_chats`, `Strata_chats`, and `/home/ai-user0/HyperCore-WebUI/data/history/_old_chats` - it never reads this repo's folder.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**agent-select**
- [12] `GET /prompts` is served, and the web app builds the agent dropdown from it at run time (`agent-select` in `index.html`, `loadPrompts`/`renderAgents` in `app.js`), with a stamp check so an unchanged catalog never touches the menu.
  <sub>2026-10-06 · strata-chat-2026-10-06-18-57.md</sub>

**app.js**
- Measured with the real functions lifted out of `app.js`: `serve/server.py` defaults to `trim_context=False` ("requests are never truncated"), so the engine answers 400 and the chat is left with an empty bubble.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**decode_ms**
- Those were empty for every real request, and the cause was in the engine adapter, not the dashboard: Closing the generator at the end-of-turn token means the worker's terminal frame is never read, so `HyperCoreRequestHandler.last` stayed `None` and every column fed from it (`reused`, `prompt_read`, `decode_ms`, `decode_tok_s`, `hit_rate`) rendered as "–".
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**expert cache rate**
- Expert cache hit rate 87–92% on a 512-expert MoE: 11,022 slots (14.5 GiB) in VRAM plus 20.72 GiB of experts page-locked in RAM, and the log says "0 blob reads from the file" — the SSD is never touched during decode.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**il_uses**
- `Verifier::il_builds` / `il_uses` were declared but never written anywhere in the tree, so even in serve mode the "N copies built, M calls reused" line could never fire.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**inferred point flagged**
- One inferred point, flagged as such: the `PREFIX_TOKENS = 6200` constant and `CHARS_PER_TOKEN = 4` are hardcoded client estimates, not measured per request; the code comment at `app.js:1112-1114` describes the intent as "exact at the moment the cap bites, never worse than a proportion away from it".
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**many_prompt.json**
- `many_prompt.json` (76 profiles, owned by `user0`) is never read.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**measured under dropdown**
- So "10 measured under 1.0 s" and "12 in the dropdown" are both true and were never reconciled.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**rendermcp sets hidden**
- `renderMcp()` sets `$("mcp-row").hidden = true` (`app.js:392-393`), so the switch is never shown.
  <sub>2026-10-04 · strata-chat-2026-10-04-06-37.md</sub>

**s.latest.releases**
- The fix `serve/build_api.py` now has `build_status()`, and the route calls it: `loadBuild()` reads `s.latest.releases` for the dropdown, `s.newest` for the update note, appends `main (the current source)` as a target (that is what `tools/build_engine.sh` defaults to, so the box is never empty even with no network), and states the reason when GitHub returns nothing.
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**several gpus when**
- Several GPUs (#498): when the RAM holds … [universal] /home/ai-user0/Strata/prompt.json … not fetch a web page to check what a screenshot shows unless the question needs an outside fact.", "encoder_available": "Images reach the model through the strata-vision … [universal] /home/ai-user0/Strata/docs/DETAILS.md … from-mcp-servers) above, where the Strata model calls your MCP tools.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-36.md</sub>

**vram free everything**
- And "193 MiB of VRAM free with everything loaded - LOW: requests may stall" and "WARNING: only 51 MiB of VRAM free at the start of this request (below 512 MiB): when the card is this full the driver can page GPU memory to system RAM, decode falls to a few tok/s and a window may never finish (#2065)".
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**want reload model**
- If you don't want to reload the model, `cp prompts/prompt.json prompt.json` makes the running server pick the profiles up on its next fetch — it re-reads the file when its mtime/size changes.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

## Still open

not done, deferred, or waiting on a decision


**_old_chats/**
- Not done, since it was not in scope: the Monitor tab has no listing of archived chats, and `data/history/README.md` still describes the transcripts as living in `chats/` while the actual folder is `_old_chats/`.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**initial_title**
- Not implemented, and why: subagent context isolation needs an agent-level `spawn` tool, not a change to the tool layer (the existing substitutes are `save_to` and `terminal__process` log files, which the docs tools can read); streaming `ToolCallEventStream`/`initial_title` has nothing to render into here; a lazy skill catalog would duplicate what `prompt.json` already carries.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

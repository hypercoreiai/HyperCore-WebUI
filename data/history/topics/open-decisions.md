# Open decisions and deferred work

Accumulated from 14 transcripts, 18 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

- **Anthropic and Responses routes and the turn-tail check.** The check is wired into all three
  routes now; the remaining work is measuring whether it changes answer quality in practice, not
  whether it runs.
- **`--no-turn-tail-check` as a per-model setting.** Done the other way round: the run config is
  already per model, and the missing piece was one switch for a whole install. `serve/server.py`
  `resolve_turn_tail(cli_flag, cfg, env)` decides in order `HYPERCORE_NO_TURN_TAIL_CHECK`, then
  `--turn-tail-check`, then `"turn_tail_check": true`; the server prints when the environment
  overrides a request, and `GET /v1/status` reports the resulting `turn_tail.enabled`.
- **The live server must be restarted for any config change to take effect.** This is the most
  common cause of "I changed it and nothing happened" and there is no hot reload.
- **Fact-checking the 2026 state-of-AI document.** Done as far as it can be from here: the
  figures the history carries (88% / 62% / 23% / under 10%) are from McKinsey's State of AI
  global survey of November 2025, not a 2026 report, and the year was the only thing wrong with
  them. The checked version and the source are in `data/history/notes/general-knowledge.md`.
- **The INTERPRETER grant in sudoers** and the operator rule fallback were left undecided.
- **Pre-existing lint and mypy findings** in `serve/mcp/web_scrape_mcp.py` and `serve/docs_mcp.py`
  are now cleared: the blocking `open()` calls inside async handlers run through
  `asyncio.to_thread`, the optional-Pillow `except Exception: pass` became
  `contextlib.suppress(ImportError, OSError, ValueError)`, the sort keys are typed, and the
  executable bits on files with no shebang were removed. `ruff` and `mypy` report nothing on those
  two files, `serve/mcp/__init__.py` and `serve/winjob.py`.
- **A single turn larger than the context cap.** Solved on the trim path, not on the default
  path. `serve/server.py:_fit_messages` drops the oldest turns and then head/tail-truncates the
  newest turn's own text, so a lone oversized turn (a pasted file, one turn carrying many tool
  results) fits instead of 400ing. Verified on a mock engine: a 200,000-character user turn with
  `--trim-context` returned 200 with "0 oldest turn(s) dropped, 167548 characters clipped from the
  newest; prompt 32696 of 32768 tokens". Without `--trim-context` the server still refuses, which
  is the documented default.


## What is true now

how the thing works, as the chats established it


**.mcp_test_state.json** · 3 chats
- `.mcp_test_state.json` at loop 102, parses cleanly The one open item needs the GPU, which I can't free from here.
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>
- This is the item already recorded as "Not done" in `.mcp_test_state.json`.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-12-05.md</sub>
- `.mcp_test_state.json` updated: loop 2, verified list refreshed with the full run, `next_step` = no test failures outstanding; remaining work is content item [2] and the by-design open items in [21].
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-23-05.md</sub>

**--decode**
- Remaining queue is the three open items: re-run `--decode` with the parser in place, add a `--think-budget` prompt so `thinking_tok_s` is non-zero, and cross-check `disk_bytes_per_s`/`pcie_bytes_per_s` against psutil/NVML.
  <sub>2026-10-16 · strata-chat-2026-10-16-02-29.md · also in strata-chat-2026-10-16-02-29.md</sub>

**--think-end**
- One open item: the default think-end id is the pack's `<|im_start|>` (151667) — a real `build/strata` run needs `--think-end` set to whatever id that pack's tokenizer closes reasoning with, and the GPU was busy so the real-engine smoke run could not boot.
  <sub>2026-10-15 · strata-chat-2026-10-15-19-50.md</sub>

**calls means round1**
- Hmm, calls==3 means: round1 (WORK) → not done → check round 2 (IGNORED) → text empty → `tail_is_done("")` False → then the guard `not (svc.turn_tail_check and checks < ...
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**config/mcp.yaml**
- The one open item is still yours: whether `config/mcp.yaml` should carry `max_tool_calls` 200 (as committed) or 500 (as the recorded target says).
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>

**disk_read_mb**
- NVML `pcie_rx_mb` and psutil `disk_read_mb` are not yet sampled into the trial record as an independent cross-check — that's the natural next step, and `hardware.py` already has the ambient-probe scaffolding for it.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**state file next**
- The state file's next step (rebuild, `./setup.sh --calibrate`, record the measured pcie-frac) is still open, and I updated it to note that the attempt needs the GPU free.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>

**test_recovery_is_the_supervisors_and_is_bounded**
- Reverted it; the residual flakiness in `test_recovery_is_the_supervisors_and_is_bounded` predates this work and is still open.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**.mcp_test_state.json**
- `.mcp_test_state.json` is at loop 25 with the result and the still-open item: the real-engine sweep is blocked by the GPU (344 MiB free, server running).
  <sub>2026-10-15 · strata-chat-2026-10-15-18-43.md</sub>

**strata_deferred_register layer registration**
- `STRATA_DEFERRED_REGISTER=1` (per-layer registration pipelined with the load) exists but is opt-in and Windows-only, because it measured ~10% slower decode.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>

**what still open**
- What is still open, and it is not the IL path. The card is held again (pid 3445307, 24124/24564 MiB), so I could not run the reproduction.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

## Still open

not done, deferred, or waiting on a decision


**--decode**
- Not done: I did not re-run anything — no `--decode` sweep was started, and the work is still uncommitted.
  <sub>2026-10-16 · strata-chat-2026-10-16-02-29.md</sub>

**.git**
- Open items: commit the rename, re-point the docs links, fix the 21 pre-existing `serve/` test failures (all in files that are untracked at HEAD, none rename-related), and decide what to do about the 3 `.git` object-pack hits that would need a history rewrite.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>

**_old_chats/**
- Not done, since it was not in scope: the Monitor tab has no listing of archived chats, and `data/history/README.md` still describes the transcripts as living in `chats/` while the actual folder is `_old_chats/`.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**max_tune.sh**
- Not done, and it cannot be done here: the three hardware items in the queue (vram ladder with coverage active, `--prefill-cap auto` vs `auto:32768`, live read-rate cross-check) need the GPU, which `max_tune.sh` requires at >=12000 MiB free.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**ruff_cache_dir**
- Remaining open items are the sudoers line‑41 direction, the INTERPRETER group decision, and the `RUFF_CACHE_DIR`/`TERMINAL_ALLOW_EXTRA` settings that still need a hub restart to take effect.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>

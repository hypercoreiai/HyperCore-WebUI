# strata-chat-2026-10-06-23-05.md

2026-10-06 · 402 KB · 4 requests · topics: deployment-permissions, issue-log, mcp-tooling, testing-quality, web-ui-monitor

## What was asked
- run all tool tests
- run all additional tests
- write a shell script to perform a zip archive of /home/ai-user0/Strata and save it to archive/strata_<date_time>. Put the shell script in /home/ai-user0/_Notes/archive_strata.sh
- i put an untouched Strata source directory at /home/ai-user0/_Strata Compare the files there to the current project /home/ai-user0/Strata and document all the changes between the 2 directories and log them in /home/ai-user0/_Notes/strata_diff.md

## What came of it
- The line-range, outline and edit-matching primitives those tools are built on are unit-tested against strings, with no server and no workspace, in `serve/test_doc_text.py`.
- The default report is `/home/ai-user0/.terminal/retest_report.json`.
- Nothing here touches the real `/home`, the real downloads folder, or a running chat: each server is started with its root pointed at a temporary directory (`STRATA_DOCS_ROOT`, `STRATA_RAG_ROOT`, `SCRAPE_DIR`, `TERMINAL_ROOT`).
- The web tests find Firefox the same way the server does — `STRATA_FIREFOX`, then `/home/ai-user0/.local/firefox/firefox/firefox`, then `firefox` on PATH — so they run when a browser is present instead of being skipped.
- A test method is tagged so the report can group it by server and tool: Add the tool's name to the module's `ADVERTISED` list and to `EXPECTED` in `test_contracts.py`; the runner then checks that the tool has at least one case and that its `inputSchema` is well formed.
- The restart script and the text primitives behind the document tools are tested separately, with no server and no workspace: The restart script and the text primitives behind the document tools are tested separately: `python3 -m unittest serve.test_restart_script serve.test_doc_text` - 24 cases, no server needed.
- The servers under `serve/mcp/gt-mcp-server/` and `serve/mcp/ps5-control-mcp/` are separate projects with their own test suites and are not part of this run.
- `.mcp_test_state.json` updated: loop 2, verified list refreshed with the full run, `next_step` = no test failures outstanding; remaining work is content item [2] and the by-design open items in [21].
- File re-parsed with `jq` to confirm it is not corrupted.
- 3 × `serve/test_winjob.py` — job objects / power throttling are Windows-only Coverage note: the README names `serve/mcp/gt-mcp-server/` and `serve/mcp/ps5-control-mcp/` as separate projects with their own suites — neither directory exists in this repo, so there is nothing to run there.
- `master_serve/` and `_serve/` are archive snapshots of earlier code and were not run against the live tests.
- `_patch_condenser_test.py` at the repo root is a one-off patch script, not a test.
- Grand total across both runs: 204 harness cases + 597 additional = 801 cases, 0 failures, 0 errors, 8 environment-conditional skips. `.mcp_test_state.json` updated (loop 3) and re-parsed with `jq` to confirm it is valid: completed items, the four resolved environment causes, the remaining legitimate skips, and `next_step` = no test failures outstanding.
- Your edits are correct — diff against git HEAD: An AST scan of `serve/telemetry.py` now finds zero `open()` calls outside a `with`.
- The leak was genuine in the running server — `Telemetry.__init__` starts a daemon `_loop` thread and `_CpuRamFallback.__init__` calls `_times()` immediately, so every tick was leaking a handle.

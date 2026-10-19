# strata-chat-2026-10-04-06-01.md

2026-10-04 · 42 KB · 1 requests · topics: nothing worth keeping

## What was asked
- test all tools

## What came of it
- The default report is `/home/ai-user0/.terminal/retest_report.json`.
- Nothing here touches the real `/home`, the real downloads folder, or a running chat: each server is started with its root pointed at a temporary directory (`STRATA_DOCS_ROOT`, `STRATA_RAG_ROOT`, `SCRAPE_DIR`, `TERMINAL_ROOT`).
- The web tests find Firefox the same way the server does — `STRATA_FIREFOX`, then `/home/ai-user0/.local/firefox/firefox/firefox`, then `firefox` on PATH — so they run when a browser is present instead of being skipped.
- A test method is tagged so the report can group it by server and tool: Add the tool's name to the module's `ADVERTISED` list and to `EXPECTED` in `test_contracts.py`; the runner then checks that the tool has at least one case and that its `inputSchema` is well formed.
- The servers under `serve/mcp/gt-mcp-server/` and `serve/mcp/ps5-control-mcp/` are separate projects with their own test suites and are not part of this run.
- `pytest -q tools/` under `/usr/bin/python3` fails at collection (8 errors) on `import numpy` — `tools/strata_pack.py:30`.
- `pip install numpy` is blocked by PEP 668 (externally-managed environment).
- numpy for the system interpreter, if `tools/` must run under `python3`.

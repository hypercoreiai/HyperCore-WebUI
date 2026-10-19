# strata-chat-2026-10-07-12-05.md

2026-10-07 · 62 KB · 1 requests · topics: nothing worth keeping

## What was asked
- I hit the context ceiling, I thought that you implemented a trucation method to prevent that from happening

## What came of it
- A single turn bigger than the cap cannot be pruned. `pruneToContext` drops whole turns only; `turnEnd(body)` runs out and the loop breaks with `body.length > 1` still true.
- This is the item already recorded as "Not done" in `.mcp_test_state.json`.
- The tool-definition block is fixed overhead — measured ~6,200 tokens of every prompt (42 tools, 8,342 chars of definitions), and no layer prunes it.
- Also worth checking: `DEFAULTS` in `app.js:597` sets `ctx: 132000`, but if your Chat-tab Context cap field is empty or 0, `pruneToContext` returns the payload untouched ("no cap").
- Server-side pre-prefill trim in `serve/server.py`: drop/condense the oldest non-system turns before tokenising, so every client is protected, not just the web UI.
- Hub-level allow-list in `mcp.yaml` using the existing `template_tools(exclude=...)` (`serve/mcp/__init__.py:616`) to shrink the fixed tool block from 42 tools to the 38/36 from the merge audit.

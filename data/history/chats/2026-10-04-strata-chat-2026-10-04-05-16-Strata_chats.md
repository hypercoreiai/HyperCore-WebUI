# strata-chat-2026-10-04-05-16.md

2026-10-04 · 1138 KB · 2 requests · topics: mcp-tooling

## What was asked
- retest all tools and fix any errors
- Group the tests by server and write a series of comprehensive tests: /home/ai-user0/Strata/serve/mcp/tests to reduce the tools calls. When the end user requests retesting the tools this will standardize the success/fail data

## What came of it
- Protocol revision requested: `2025-06-18` (the server's answer is used as given).
- The old SSE transport (`"type": "sse"`) is rejected with an explanatory message.
- Tool naming: every tool is offered to the model as an OpenAI function tool named `<server>__<tool>`.
- Two servers may both define `search`; the prefix keeps them distinct.
- A failed call — the tool's own error, a crash, a timeout — is returned as text beginning `error:`, so the model can react rather than the chat breaking.
- Result capping: content blocks are joined into text and capped at `max_result_chars`, with the overflow stated.
- Hub defaults (overridable in `mcp.yaml` or the run config's `"mcp"` block): `mcp.yaml` wins a shared name over the run config.
- A server entry with `"disabled": true` is left out.
- Current contents: `server.py` resolves the path as: `--mcp-config` if it is a `.yaml`/`.yml`, otherwise `ROOT/mcp.yaml`; a JSON `--mcp-config` is added to the yaml rather than replacing it.
- Firefox is a separate headless process with its own empty profile, started lazily on the first search or browse, driven over the Marionette protocol (a plain socket, length-prefixed JSON packets: `len:` then the JSON).
- Returns `{"firefox": "running" | "not started", "binary": ..., "last_url": ..., "last_error": ...}`.
- DuckDuckGo result links are redirects; `unwrap_ddg()` extracts the real target from the `uddg` query parameter.
- Idempotence and cleanup — `scrape__remove_downloads` reports files and bytes removed; `docs__delete_file` has no equivalent at all, and my test files in `/home/ai-user0/tmp … [universal] /home/ai-user0/strata-chat-2026-10-03-15-09.md … 5.
- `python3 -m pytest -q` in `/home/ai-user0/Strata`: 265 passed, 5 skipped, 149 subtests passed (the 5 skips are genuinely conditional: no pack tokenizer, Windows‑only job objects).
- `ninja` isn't installed; `terminal__build` falls back to `make`, which works.

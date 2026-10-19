# strata-chat-2026-10-04-05-39.md

2026-10-04 · 525 KB · 1 requests · topics: mcp-tooling, testing-quality

## What was asked
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
- One flaky ordering issue remains worth hardening: the `home` index must be built before its read tools can answer.
- `run_all.py` now reads the `@case(server, tool, name)` tag off the test method (not the instance), aggregates per server and per tool, lists any advertised tool with no case, records the slowest call per tool, and diffs against a previous report with `--compare`.
- `test_contracts.py` — the fast pass a retest needs before the deep ones: every server in `mcp.yaml` starts, advertises exactly its documented 35 tools, every `inputSchema` is a well-formed object with typed arguments, unknown tools and bad arguments are answered as text rather than crashing the connection, and a server keeps answering after a bad call.

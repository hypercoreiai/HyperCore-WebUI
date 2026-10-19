# strata-chat-2026-10-05-20-19.md

2026-10-05 · 858 KB · 10 requests · topics: deployment-permissions, issue-log, mcp-tooling, setup-configuration, testing-quality

## What was asked
- This is a new user profile on a ubuntu server machine that was configured with a script. read the docs in /home/ai-user0/_Notes
- Here is the script, you have 30+ tools and some are not working in this new user profile. read the docs first and then we will make everything else work
- read the mcp servers and update paths with $HOME to make the code universal
- run tool tests
- I update the permissions for serve
- retest all tools
- can you create a test python venv in /home/ai-user0/testvenv and write a simple python test script that executes in the venv. also test pip by installing torch and print that torch is working
- In the downloads folder there are 3 sudoers.ai-user0* files. which one is current and which one is desired
- In the downloads folder there are 3 sudoers.ai-user0* files. which one is current and which one is desired
- perform reccomended housekeeping

## What came of it
- The context window is 262,144 tokens (max_context=262144).
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
- truncated: the tool returned 8,186 characters; only the first 6,000 are shown] json "path": "/home/ai-user0/_Notes/strata-chat-2026-10-03-15-09.md" /home/ai-user0/_Notes/strata-chat-2026-10-03-15-09.md
- The `docs__create_file` bug I found earlier is a symptom of this: I had no way to `mkdir` or check writability, so I guessed.

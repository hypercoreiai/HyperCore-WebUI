# strata-chat-2026-10-03-19-51.md

2026-10-03 · 1171 KB · 7 requests · topics: deployment-permissions, engine-internals, mcp-tooling

## What was asked
- check all the mcp servers to make sure they link to /home/ai-user0 The sudo password for ai-agent is Qazwsx1@3
- Ignore the docker directories, but update all other files that reference /home/ai-user0/0
- restart accomplished
- Write a complete MCP_Tools.md documents that would a human developer to rebuild the tools from scratch. Save it in /home/ai-user0/_Notes/MCP_Tools.md
- is there a way create a automated client-side agent wrapper to heartbeat the strata interface and ensure all tasks are completed without interuption
- i want this inside the web app rather than outside it, the natural place is serve/web/ — the same /metrics data already drives the Monitor tab, so a client-side heartbeat there would need no new endpoints.
- prompt (263250 tokens) leaves no room to answer in the context (262144); requests are never truncated

## What came of it
- Config file: `/home/ai-user0/Strata/mcp.yaml` (this is what Strata reads at startup — `serve/mcp.py` defaults to `ROOT/mcp.yaml`).
- `/home/ai-user0/Strata/serve/test_docs_mcp.py` hardcoded `/home/ai-user0/Strata/.venv/bin/python3` and the script path About the sudo password: it wasn't needed — everything was writable as `ai-agent` (the MCP files are owned by `ai-agent`, and `/home/ai-user0/downloads` has an ACL granting `ai-agent` rwx).
- For reference, `/etc/sudoers.d/ai-agent` only allows NOPASSWD for `apt-get`, `pip3 install`, nginx `systemctl`, the ruff_cache mkdir/chmod, and `visudo -c` — `sudo` with that password refuses anything else ("I'm sorry ai-agent.
- All non-Docker files that referenced `/home/ai-user0/0` are now pointed at `/home/ai-user0/t`.
- `git status` in `/home/ai-user0/Strata` works — no "dubious ownership"
- `black --version` runs (26.5.1, CPython 3.12.9) through the new shebang
- A restart of Strata is needed for the new `mcp.yaml` to take effect — say the word and I'll do it.
- Confirmed — the restart took effect and every server is now on `/home/ai-user0/t`.
- Sandboxing — `scrape__status` reports `private_ips_blocked: true` and a 200 MB cap; `docs` is rooted at `/home`.
- `terminal__run` needs its own per-call timeout, or a `sleep 99999` hangs the whole session.
- Output caps — `max_result_chars: 20000` already exists; truncation must be explicit ("showing 4,000 of 21,318 bytes; use `offset` for more"), not silent — the `home__read` truncation I hit earlier was invisible.
- Idempotence and cleanup — `scrape__remove_downloads` reports files and bytes removed; `docs__delete_file` has no equivalent at all, and my test files in `/home/ai-user0/tmp` are still there because of it.
- Added to `serve/terminal_mcp.py` (server now exposes 14 tools: `run, pipe, process, repl, symbols, check, install, status` + `build, compile, tidy, debug, query, compare`).
- One subtle bug fixed: the command guard's name pattern didn't accept `+`, so `g++` was read as an argument and `leak_demo.cpp` was treated as the program.
- Verified against `mcp.yaml` (lists its five top-level keys), `strata-q2_0.json`, `ps5-control/config.toml` (read `playstation.ip` = `192.168.74.3`), and the RAG index directly: Non-SELECT statements are refused (`DELETE` raises).

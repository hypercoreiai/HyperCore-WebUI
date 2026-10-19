# MCP servers and tools

Accumulated from 62 transcripts, 212 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**One hub, several servers.** `serve/mcp/` holds the MCP servers; `serve/mcp/__init__.py` is the
hub that registers them. Servers are stdio JSON-RPC, standard library only — no MCP SDK. The
shipped set is `home` (documents/index), `docs`, `scrape`, `browser`, `terminal`, `assist`, and
the `web` transport. `mcp.yaml` is the registration file; the run config's `"mcp"` block can also
supply servers, and `mcp.yaml` wins a shared name.

**Servers are started at boot, not on demand.** A new or edited server file does nothing until the
hub restarts. This has caused more "the tool is missing" reports than actual bugs.

**Tool tiers.** The terminal server is organised by tier: Tier 1 is the plain shell (`run`, `pipe`),
Tier 2 is structured readers (`repl`, `symbols`, `check`, `install`, `build`, `compile`, `tidy`,
`debug`, `query`, `compare`, `status`), Tier 3 is the heavier native tools (cmake, gdb, valgrind,
clang-tidy), Tier 4 is the environment probes. Binaries are allowlisted; `TERMINAL_ALLOW_EXTRA`
and `TERMINAL_ALLOW_ALL=1` are the escape hatches, and an allowlist refusal names the binary and
the hatch.

**Permissions are ownership-based, not path-based.** `serve/tool_permissions.py` allows writing to a
file this process created or anything under `scratch/`, `tmp/`, `downloads/`; everything else is
refused with a reason and a `force=true` override. Denials come back as error text, so the hub
still marks the call `ok=True` — the reason is in the payload.

**Registry uniqueness is tested.** `McpHub` records namespaced collisions in `hub.collisions`,
reported by `status()`, and a contracts test asserts the shipped config produces none.

**Output is capped and the cap is visible.** `max_result_chars` truncates with an explicit
"showing N of M bytes; use offset for more" rather than silently cutting.

**Two caps, and they are different.** `mcp.max_rounds` (500) counts assistant turns that may
emit calls; `mcp.max_tool_calls` (500) counts the calls that may actually run. A turn can emit
several calls, so the round cap is not a call cap. Calls past the call cap are closed as
"not run", the transcript keeps the results that did run, and the turn ends rather than looping
on calls that can never execute. `max_tool_calls: 0` removes the call cap and leaves the round
cap in force.


## What is true now

how the thing works, as the chats established it


**mcp.yaml** · 16 chats
- A live server is running (pid 2461611, started 1 h 50 m ago) and `mcp.yaml` is read once at startup, so it still uses 100 until restarted.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-09-01.md</sub>
- The remaining 21 are pre-existing drift in untracked files (`test_hub.py` expects six MCP servers while `mcp.yaml` registers seven; `terminal_mcp.py`'s allowlist returns ok for a non-allowlisted binary).
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>
- It isn't registered in `mcp.yaml` (that file has web, home, docs, scrape, terminal, and now browser), which is probably why I didn't reach for it.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>
- To make it available to other apps, `strata_connect_info` gives the base URLs; to add MCP servers to it, `mcp.yaml` beside the config is what `serve/server.py` loads.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>
- `tools/strata_mcp.py` — the built-in server with `strata_status`, `strata_models`, `strata_benchmark`, `strata_logs` — is absent from `mcp.yaml`.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>
- The hub reads `mcp.yaml` once at startup (`serve/server.py:1976`), and a server dropped at startup is not retried.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>
- Any venv interpreter is therefore unreachable through the terminal tool — the `command:` field in `mcp.yaml` is unaffected, since the hub spawns it directly.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>
- `mcp.yaml` → `TERMINAL_ALLOW_EXTRA: sudo,setfacl,getfacl,visudo,apt-get,apt,nproc,timeout,npm,yarn,corepack,snap`, so the sandbox allowlist no longer refuses commands the account is permitted to run (`npm --version` currently fails with "not on the allowlist").
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>
- Hub defaults (overridable in `mcp.yaml` or the run config's `"mcp"` block): `mcp.yaml` wins a shared name over the run config.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-16.md</sub>
- Next step: restart Strata — `mcp.yaml` is read at startup.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>
- One subtlety worth knowing: the shim is `pyenv exec <program>`, and the server's environment has `PYENV_VERSION=system` (set by `mcp.yaml`' `PYENV_DIR`).
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- To make them apply you'd need to start Strata (or just that server) as ai-agent; `mcp.yaml` has no `user:` key, so `command: sudo` is the only in-file option and it conflicts with stdin being the JSON-RPC channel.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- Now the entry point (the house pattern is a thin `serve/mcp/.py` shim over an implementation in `serve/`), the `mcp.yaml` registration, and a stray mis-indented comment in the test.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>
- The server is on disk (`serve/terminal_mcp.py`, 37 KB; `serve/mcp/terminal_mcp.py`, 287 B) but my tool list still has no `terminal__` tools — Strata reads `mcp.yaml` at startup, so the fifth server isn't loaded until it restarts.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>
- Both files are on disk and indexed; `mcp.yaml` registers the server.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>
- A restart of Strata is needed for the new `mcp.yaml` to take effect — say the word and I'll do it.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**serve/mcp.py** · 7 chats
- `serve/mcp.py` is a stale pre-move copy that nothing can import, yet it is tracked in git and still shows up in lint output (`serve/mcp.py:235 PLW1510`, `serve/mcp.py:596 S110`).
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>
- Fix: delete `serve/mcp.py`, and rename the package entry so the docstring stops lying about its own path.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>
- Name shadowing. `serve/mcp.py` (676 lines, tracked) and `serve/mcp/__init__.py` (1343 lines, untracked) both exist; `import serve.mcp` resolves to the package, so the old module is dead code that can silently mislead.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>
- `serve/mcp.py` is now dead code. It's still present and byte-for-byte identical in both trees (676 lines), but the new `serve/mcp/` package shadows it — I confirmed `import serve.mcp` resolves to `serve/mcp/__init__.py`.
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>
- Architecture — the hub (`serve/mcp.py`): both transports, tool naming (`<server>__<tool>`), defaults, restart-on-crash, result capping.
  <sub>2026-10-03 · strata-chat-2026-10-03-19-51.md</sub>
- Stdlib only. Strata's own MCP client (`serve/mcp.py`) is explicitly written with "the standard library only; no MCP SDK is needed," and the two servers that do most of the work follow suit.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>
- The Docker version is untouched. If you'd rather keep the container running, the alternative is a `url:` entry — `url: http://web-scrape-mcp:8766/mcp` — which `serve/mcp.py` already supports.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**/home** · 5 chats
- The `/home` document index is empty — `phase: idle`, `files: 0`, `rows: 0` in `rag.sqlite`.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-12-32.md</sub>
- Evidence base: the tool descriptions in this session, `terminal__status` output (root `/home`, `timeout_s: 30.0`, `max_out_chars: 6000`, 137 allowlisted binaries), and two truncation events I hit directly (`web__browse` → "returned 6,755 characters; only the first 6,000 are shown"; `home__read` of `DETAILS.md` → 8,189 → 6,000).
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>
- Guardrail friction worth knowing: `inside()` resolves symlinks, so `/home/ai-user0/Strata/.venv/bin/python3` is refused by `terminal__run` (it resolves to `/usr/bin/python3`, outside `/home`).
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>
- I also created `/home/.ruff_cache` owned by ai-agent with ACLs for user0, and set `RUFF_CACHE_DIR: /home/ai-user0/.terminal/cache` in `mcp.yaml` — `/home` itself is not writable, which is why ruff failed with "Failed to initialize cache at /home/.ruff_cache".
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- A small tool quirk: `home__structure` rejects relative paths like `user0/tmp` and requires an absolute path inside `/home` — `docs__list_directory` accepts relative ones.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**/home/ai-user0/strata/mcp.yaml** · 4 chats
- Written to `/home/ai-user0/Strata/mcp.yaml` — parses cleanly, and every key is one `serve/mcp.py` actually reads (`mcp.py` lines 629–637).
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>
- The footer also confirms the point from before: the running server still has `max_rounds = 8`, even though `/home/ai-user0/Strata/mcp.yaml` now says 20 — `mcp.yaml` is read once at startup.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>
- Two files updated: `/home/ai-user0/Strata/mcp.yaml` `/home/ai-user0/Strata/serve/mcp/web_scrape_mcp.py` — the fallback default, so it matches rather than diverging if the env var is ever unset: The docstring now states the path explicitly too.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>
- Config file: `/home/ai-user0/Strata/mcp.yaml` (this is what Strata reads at startup — `serve/mcp.py` defaults to `ROOT/mcp.yaml`).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**terminal__run** · 4 chats
- </think> <tool_call> <terminal__run> <parameter=command> cd /home/ai-user0/HyperCore-WebUI/optuna_tune && sed -n '1462,1484p' bayesian_tune.py </parameter> </function> </tool_call> The token timeline is stamped when the main thread processes a queued chunk, not when the reader thread read it, so a burst of queued lines all get the same timestamp.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-14-37.md</sub>
- Your `terminal__run` has `timeout_s` but doesn't tell the model to set it.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>
- Once `terminal__run` exists, the self-test is one call: That closes the loop the toolset was missing — a tool that can verify its own tests.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>
- `terminal__run` needs its own per-call timeout, or a `sleep 99999` hangs the whole session.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**home__read** · 3 chats
- For a coding tool set this is an inconsistent surface, and the `home__read` description ("the other indexed types") doesn't warn that code is excluded.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>
- `home__read` still only covers the types the index classifies — `serve/terminal_mcp.py` returns "that file is not a document type this index reads" (pre-existing `classify()` behaviour, unchanged).
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md · also in strata-chat-2026-10-04-03-57.md</sub>
- Output caps — `max_result_chars: 20000` already exists; truncation must be explicit ("showing 4,000 of 21,318 bytes; use `offset` for more"), not silent — the `home__read` truncation I hit earlier was invisible.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**max_rounds** · 3 chats
- (The `max_rounds: 8 -> 500` line in the same diff was already in the working tree before this session; it is not mine.)
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>
- The project's hard tool-call cap is the MCP hub's `max_rounds` (enforced in `serve/server.py:3035`, `if run_them and rounds >= max_rounds: yield "mcp", {"event": "limit", ...}`).
  <sub>2026-10-13 · hypercore-chat-2026-10-13-09-01.md</sub>
- Keep `max_rounds` at 8 or lower — raising it makes the prompt grow further; the fix is fewer/smaller results, not more rounds.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>

**python invisible_playwright works** · 3 chats
- Passing an interpreter works: `terminal__install(packages=["regex"], python="/home/ai-user0/Strata/.venv/bin/python")` → exit 0.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md</sub>
- Stealth engine `invisible_playwright` 0.27.0 is installed; camoufox and plain playwright are not.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-11-52.md</sub>
- `python -m invisible_playwright` works identically and needs nothing on PATH.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-07-26.md</sub>

**terminal__check** · 3 chats
- `terminal__check` — ruff, mypy, pylint, pytest (whichever are installed)
  <sub>2026-10-14 · hypercore-chat-2026-10-14-16-25.md · also in strata-chat-2026-10-03-15-05.md</sub>
- `terminal__check` — lint/type/test with ruff, mypy, pylint, pytest (whichever are installed)
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md · also in 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>
- `terminal__check`'s ruff stage fails — `Failed to create temporary file, Permission denied /home/.ruff_cache/0.16.10/…`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**terminal__install** · 3 chats
- Minor notes. `terminal__install` with a pyenv path resolves `/home/ai-user0/.pyenv…` to `/home/ai-user0/.pyenv…` (the trees are linked) and installs as a user install — it works, but the reported interpreter isn't the one you asked for.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>
- `terminal__install` — pip into a named interpreter; an absolute pyenv path selects a specific version, which matters here since both 3.12.2 and 3.12.9 are present.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>
- Tier 2 is therefore only partly real; `terminal__install` is the tool that closes it.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**terminal__status** · 3 chats
- `terminal__status` — working root, limits, available interpreters, and which Tier-2/3/4 tools are installed with versions
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md</sub>
- `terminal__status` — what's installed and which versions are available Build & analysis tooling
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>
- `terminal__status` now reports `missing_tools: []` — every Tier-2/3/4 binary in the spec is present.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/home/ai-user0/.venv** · 2 chats
- The two venvs I built for testing (`/home/ai-user0/.venv`, 180M, and `/home/ai-user0/.venv313`, 133M) are still on disk — say the word and I'll remove them; the project venv under `web-scrape-mcp/.venv` is the one that's actually useful.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- The two venvs from the pyenv question (`/home/ai-user0/.venv` 180M, `/home/ai-user0/.venv313` 133M) are still on disk — the project venv at `serve/mcp/web-scrape-mcp/.venv` is the one that's actually useful, since its 18 tests pass through it.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/downloads** · 2 chats
- `/home/ai-user0/downloads` is now empty; the scrape server `mkdir`s its subfolders on demand, so the MCP tools are unaffected.
  <sub>2026-10-09 · strata-chat-2026-10-09-11-32.md</sub>
- `/home/ai-user0/Strata/serve/test_docs_mcp.py` hardcoded `/home/ai-user0/Strata/.venv/bin/python3` and the script path About the sudo password: it wasn't needed — everything was writable as `ai-agent` (the MCP files are owned by `ai-agent`, and `/home/ai-user0/downloads` has an ACL granting `ai-agent` rwx).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-18-59.md</sub>

**config/mcp.yaml** · 2 chats
- The one open item is still yours: whether `config/mcp.yaml` should carry `max_tool_calls` 200 (as committed) or 500 (as the recorded target says).
  <sub>2026-10-18 · 2026-10-18-15-14-continue.md</sub>
- `config/mcp.yaml` was last written at 20:31 today, and the running WebUI process (PID 171982, started 21:04) read it, so it is already running with 500.
  <sub>2026-10-18 · 2026-10-18-10-28-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**defaults** · 2 chats
- `mcp.yaml` sets the thresholds; `DEFAULTS` in `serve/mcp/__init__.py:39-41` are the fallbacks.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>
- Also left alone are test fixtures that pass explicit values to exercise the mechanism rather than define the limit (`serve/test_mcp.py`, `serve/mcp/tests/test_hub.py`, `tools/test_setup_config.py`); `test_hub.py:73` compares against `DEFAULTS`, so it tracks the new value automatically.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-09-01.md</sub>

**docs** · 2 chats
- The running hub (pid 2856630) still holds the old env — `docs`, `scrape` and `terminal__process` only come live after `run-q2_0.sh` is restarted.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>
- `serve/mcp/terminal_mcp.py` (287 B) and `serve/terminal_mcp.py` (39 KB) exist, and `mcp.yaml` lists `terminal` alongside `web`, `home`, `docs`, `scrape` — but `serve/mcp.py` starts servers at startup, so the fifth server is only live after Strata restarts.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**docs/mcp_server.md** · 2 chats
- It's documented in `docs/MCP_SERVER.md` as a server for external assistants (`claude mcp add strata -- python3 .../tools/strata_mcp.py`), and it is not in `mcp.yaml` — that file registers only web, home, docs, scrape, terminal, and now browser.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>
- `docs/MCP_SERVER.md` presents it as a server for external assistants: To make it available in this chat, add it beside the others: Its tests are separate from the retest harness: `python -m unittest tools/test_strata_mcp.py` runs against a fake setup and server, so adding it to `mcp.yaml` wouldn't bring it into the 221-case `run_all.py` count.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**docs__read_file** · 2 chats
- Also made truncation explicit where it was silent: `docs__read_file` no longer refuses large files outright, and `home__read` (the invisible truncation you hit) now appends "showing 8000 of 12345 characters read; use run with head, tail or sed for a specific range".
  <sub>2026-10-03 · strata-chat-2026-10-03-19-51.md</sub>
- Overlapping tool families. `home__read` handles pdf/docx/config; `docs__read_file` is text-only.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md · also in 2026-10-03-strata-chat-2026-10-03-15-05.md</sub>

**enabled** · 2 chats
- Every failure is in `serve/mcp/tests` (`StubServer` has no attribute `enabled`; the terminal allowlist assertion) and `serve/mcp/web-scrape-mcp/tests` (`OSError` on the download folder).
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>
- The remaining 12 failures are pre-existing and unrelated to Assist (`serve/mcp/tests/test_hub.py` `StubServer` has no `enabled` attribute, terminal/docs allowlist assertions).
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**installed engine install** · 2 chats
- I installed the engine on this box — `pip install --break-system-packages invisible-playwright` → wrapper 0.27.0, engine fetched — and the server reached the launch step, which reports: `apt-get install xvfb` timed out against the tool's 90 s cap, so the real-engine path is not yet exercised — the live case skips with that sentence rather than failing.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-07-26.md</sub>
- Camoufox (`pip install camoufox`) and stock Playwright Firefox are the other two paths the adapter will pick up automatically.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-07-26.md</sub>

**serve/docs_mcp.py** · 2 chats
- Pre-existing lint and mypy findings in `serve/mcp/web_scrape_mcp.py` and `serve/docs_mcp.py` are known and not blocking; no test failure is caused by them.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-21-39.md</sub>
- Note: `serve/rag.py`, `serve/terminal_mcp.py`, `serve/docs_mcp.py` and `serve/tool_permissions.py` are currently untracked in git (`??` in `git status`), so these edits won't show up in a diff against HEAD.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**serve/mcp/tests/test_contracts.py** · 2 chats
- `serve/mcp/tests/test_contracts.py` still fails on a missing `Stub.enabled` — untracked directory, untouched by this work.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- Also, I should check `serve/mcp/tests/test_contracts.py` to see what contracts are tested — good to note gaps.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-00-55.md</sub>

**terminal__pipe** · 2 chats
- `terminal__pipe` — pipelines are how you actually navigate a large codebase (`rg class | wc -l`, `find … | xargs grep …`).
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-07.md</sub>
- `terminal__pipe` — takes stages as a list rather than one string.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**terminal__process** · 2 chats
- Raise it to 180 for terminal work, or document the background-job workaround (`terminal__process`), which is how the install eventually succeeded.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>
- The suite only completed by running it through `terminal__process`, which is exactly what that tool is for.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**terminal__repl** · 2 chats
- `terminal__repl` — persistent Python or Bash session that keeps state between calls
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>
- `terminal__repl` — a persistent Python session is the fastest route to understanding and iterating on OO code: instantiate a class, inspect it, subclass it, test it, in one round trip.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-07.md</sub>

**--break-system-packages**
- `terminal__install` cannot install into the system interpreter — no `--user`/`--break-system-packages` flag, so PEP 668 rejects it (`error: externally-managed-environment`), and the project venv interpreter is refused by the root guard (`/home/ai-user0/Strata/.venv/bin/python` resolves to `/usr/bin/python3`, outside `/home`).
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**.bb_cmake_configure.txt**
- The genuine repo dirt is separate and not removable by `git clean`, because it's tracked: 27 scratch files at the root — `.review_.txt` (13), `.mcp_run_all.txt`, `.bb_cmake_configure.txt`, `_kimi_ab.py`, `mcp.yaml.orig`, `mcp.yaml.orig2`, `ni_hypercore-q2_0.json.bak`, `.full_suite_after.txt`.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**.cpp**
- Source code is invisible to the `home` tools but visible to the `docs` tools `serve/rag.py:63 classify()` has no code extensions, so `home__read` on `serve/server.py` returns "that file is not a document type this index reads" and `home__structure` lists no `.py`/`.cpp` files.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**.gitignore**
- `.gitignore` still ignores `strata-` but not `hypercore-`, and `serve/mcp/web-scrape-mcp/.venv` (139 MB) is not ignored.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**.json**
- `terminal__query` with `action: toml` on a `.json` correctly refuses rather than guessing (`BUILD.json is not valid TOML`).
  <sub>2026-10-08 · 2026-10-08-strata-chat-2026-10-08-16-41.md</sub>

**.mcp_test_state.json**
- Two adjacent instances I did not touch (outside what you asked for, say the word and I'll do them): the report header in `serve/mcp/tests/run_all.py` that prints `Strata MCP tool retest`, and the `.mcp_test_state.json` session name `strata-upgrades-3-per-chat-distillation`.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**.pyenv**
- As ai-agent, `HOME=/home/ai-user0/t`, which has no `.pyenv`, so `terminal__status` reported only: With `PYENV_ROOT=/home/ai-user0/.pyenv` set, it correctly returns 3.12.2, 3.12.9 and 3.13.13.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**.ssh/authorized_keys**
- `pytest serve/test_docs_mcp.py serve/test_server.py serve/test_responses.py tools/test_hypercore_agent.py` → 246 passed, 1 failed — the single failure is the pre-existing `.ssh/authorized_keys` guard issue, not caused by the copy.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**.ssh/id_rsa**
- `serve/mcp/tests/test_docs.py::test_docs__create_secret` asserted `.ssh/id_rsa` can be created, while `serve/test_docs_mcp.py` asserts a credential is not a document.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**.venv**
- 16 errors are `serve/mcp/web-scrape-mcp/tests/`, which need that sub-project's own `.venv`.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**/home/ai-user0/.terminal/jobs**
- `terminal__process` writes job logs to `/home/ai-user0/.terminal/jobs` and `terminal__repl` writes `/home/ai-user0/.terminal/repl_driver.py`; the bash REPL session needs a real shell.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/0**
- Read all five (`serve/mcp/web_mcp.py`, `serve/mcp/rag_mcp.py`, `serve/mcp/web_scrape_mcp.py`, `serve/terminal_mcp.py`, `serve/docs_mcp.py`) — the code is already `$HOME`-universal; every user-specific path is derived from `Path.home()` when the env var is unset: The non-universal part was `mcp.yaml`, which hard-coded `/home/ai-user0/0` and then `/home/ai-user0/0`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/home/ai-user0/index.html**
- It doesn't turn up — a search for "Current State of AI October 2026" returns unrelated hits (`/home/ai-user0/index.html`, llama.cpp docs, Cursor project files), and `home__status` reports the crawl is idle with 0 files indexed at the root, despite 80,425 rows in the database.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-14-03.md</sub>

**/home/ai-user0/scratch/zed-review**
- Scratch copy of the Zed source is still at `/home/ai-user0/scratch/zed-review` — say the word and I'll delete it, or wire the same `doc_text` primitives into `terminal__run`'s output cap.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**/home/ai-user0/strata**
- One correction to the target path: there is no `/home/ai-user0/Strata` — the framework lives at `/home/ai-user0/Strata` (that's the path `mcp.yaml` resolves `serve/mcp/.py` against, and `ai-agent` already has write access there — `serve/assist_fast_models.json` and `serve/assist_latency_report.txt` are owned by `ai-agent`).
  <sub>2026-10-09 · strata-chat-2026-10-09-11-32.md</sub>

**/home/ai-user0/strata/_check_servers.py**
- Verified by running the hub's own loader and spawning every server with that config (`/home/ai-user0/Strata/_check_servers.py`): `~` works because `web_scrape_mcp.py:38` and `terminal_mcp.py:61` call `.expanduser()`; confirmed directly: `TERMINAL_STATE_DIR -> /home/ai-user0/.terminal`, `SCRAPE_DIR -> /home/ai-user0/downloads`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/home/ai-user0/strata/serve/mcp**
- The README's setup instructions say `cd /home/ai-user0/web-scrape-mcp`, so that copy is the one actually deployed; the `/home/ai-user0/Strata/serve/mcp` path looks like the source tree under version control.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-14-03.md</sub>

**/home/ai-user0/web-scrape-mcp**
- There is a second, byte-identical copy at `/home/ai-user0/web-scrape-mcp` — same file sizes throughout.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-14-03.md</sub>

**/mcp**
- Next step: make the empty-tool case visible (`loadMcp()` reports "no tools offered" on a non-ok `/mcp` and retries after the key is set), add `serve/mcp/__main__.py`, and decide whether non-web clients should be able to opt in.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md</sub>

**/tmp**
- If you want the broad version, it's a one-line change to `mcp.yaml` — I tested it and it works (`/usr/bin/gcc`, `cd /usr`, `/tmp` writes all succeed), but it removes the path jail entirely.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**172.17.0.1**
- In the AnythingLLM storage `plugins/anythingllm_mcp_servers.json`: `172.17.0.1` is the Docker bridge to the host on Linux.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-06-37.md</sub>

**192.168.74.3**
- Verified against `mcp.yaml` (lists its five top-level keys), `strata-q2_0.json`, `ps5-control/config.toml` (read `playstation.ip` = `192.168.74.3`), and the RAG index directly: Non-SELECT statements are refused (`DELETE` raises).
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-19-51.md</sub>

**82f46a8c**
- `mcp.yaml` exists on disk but is not in HEAD, and the tool reports "git has no HEAD for mcp.yaml" — misleading, since HEAD exists (`82f46a8c`) and the same call on tracked `CMakeLists.txt` returned `identical: true`.
  <sub>2026-10-08 · 2026-10-08-strata-chat-2026-10-08-16-41.md</sub>

**_abs_under**
- `mcp.yaml` argument rewriting is surprising `_abs_under` (`serve/mcp/__init__.py:831, 855`) rewrites any arg that names an existing path relative to the yaml file.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**_check_server**
- `$HOME` written literally in `mcp.yaml` does not work yet — `load_mcp_yaml()`/`_check_server` pass `env` verbatim to `subprocess.Popen`, with no variable expansion.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**_continuation_hint**
- Items 7–10 from the review (hub thread-safety under free-threading, the hard-coded `_continuation_hint` names, the retry-on-substring check in `web_scrape_mcp.py`, and the per-call restart throttle) are design changes rather than bugs, so I left them untouched — say the word and I'll take them in that order.
  <sub>2026-10-06 · strata-chat-2026-10-06-00-55.md</sub>

**_wait**
- HTTP-transport calls cannot be interrupted `serve/mcp/__init__.py:345-360`: the POST runs on a daemon thread and `_wait` raises `McpTimeout`/`McpCancelled` while that thread keeps running (with a hardcoded `timeout + 5` socket timeout).
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**apply_edits**
- The transports are hand-written stdio JSON-RPC with correct cancellation, restart-after-crash, paging and result caps; path guards resolve symlinks and reject escapes from the root; the scrape server checks every redirect hop and blocks private addresses; `apply_edits` refuses to write if any edit fails and handles non-UTF-8 files by matching bytes.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**assist__status**
- This is exactly what Strata's `serve/assist.py` already calls (`assist__status` reports `model_count: 80`).
  <sub>2026-10-09 · strata-chat-2026-10-09-09-50.md</sub>

**ast**
- `terminal__symbols` — reads a file or package with `ast` and returns classes, bases, members, and whether each member is public.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-05.md</sub>

**browser**
- Sync Async The `browser` object is a `playwright.sync_api.Browser` / `playwright.async_api.Browser` - every Playwright method works as-is.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-07-26.md</sub>

**build_dir**
- `terminal__tidy` without a `build_dir` prints "Could not auto-detect compilation database… Running without flags." It still runs and reports counts, but to resolve project includes it needs the `build_dir` pointing at a `compile_commands.json`.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md</sub>

**bytetokenizer**
- Against the real `serve/chat_template.jinja` and the seven servers in `mcp.yaml`, using the `ByteTokenizer` from the test harness (one id per byte, so these are character counts; the model's BPE gives roughly a quarter): The number moves with the surface, which is the point.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**calls**
- Accounting. `hub.usage` tracks `calls`, `chars`, `dropped_chars`, `truncated`, `errors`, `condensed_results`, `condensed_chars`; the answer's meta line appends `N tool calls` (`app.js:1424-1425`), and any tool left in `writing`/`running` when the stream ends is marked `skipped`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**calls/chars/truncated/dropped_chars/errors**
- Token accounting — `serve/mcp/__init__.py` Every call now returns `tokens_est` (`chars // 4`) and the hub accumulates `calls/chars/truncated/dropped_chars/errors`, exposed in `status()`.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**camoufox**
- If you want those, I'd need a browser engine — installable here via `pip install playwright && python -m playwright install firefox` (or `camoufox`/`invisible-playwright`), which the tooling says takes ~260 MB one time.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-40.md</sub>

**check**
- `terminal__install` closed the gap I flagged earlier — pytest 9.1.1 and ruff 0.16.10 are now installed in pyenv 3.12.9, and `check` uses them: `symbols` gave a real read of the MCP hub: `StdioTransport`, `HttpTransport`, `McpServer`, `McpHub`, and the `McpError → McpTimeout` hierarchy.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-09.md</sub>

**code live strata**
- The new code is live. Strata restarted at 03:44 (PID 2512290) and `GET /mcp` now reports `condense_at: 0.55, condense_after: 2, condense_chars: 1200`.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**command sudo terminal**
- `command: sudo` in the terminal entry — awkward, because `sudo -S` consumes stdin, and stdin is the JSON-RPC channel.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**compact_at**
- `condense_at`/`compact_at` default to `0` (off) in code and are enabled only by `mcp.yaml`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**condense_after**
- `DEFAULTS` gains `condense_after: 0` (off) and `condense_chars: 600`; `settings_from` validates both (`condense_after` accepts 0, rejects negatives), `load_mcp_yaml` reads them, and `McpHub.status()` reports them so `GET /mcp` shows what's active.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**conftest.py**
- The vendored servers under `serve/mcp/gt-mcp-server/` and `serve/mcp/ps5-control-mcp/` keep their own suites and are excluded from this run — `conftest.py` already ignores them.
  <sub>2026-10-04 · strata-chat-2026-10-04-05-39.md · also in 2026-10-04-strata-chat-2026-10-04-06-01.md</sub>

**core**
- `terminal__debug` `action: core` requires the `core` parameter; passing only a binary returns "is not a core file".
  <sub>2026-10-08 · 2026-10-08-strata-chat-2026-10-08-16-41.md</sub>

**core_pattern**
- `terminal__debug action=core` untested — Ubuntu's `core_pattern` pipes cores to apport, so no core file is produced to read.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**count**
- `terminal__query`'s `limit` argument does nothing `serve/terminal_mcp.py:1244` computes `count` only to put `"add LIMIT {count}"` in a note; the SQL is passed to `sqlite3` unmodified.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**cwd**
- `terminal__status` reports `"root": "/home"` — every `cwd` I pass to the terminal tools must stay inside it, and the docs tools (`docs__`, `home__`) are scoped to it too.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**doc_text.read_with_ranges**
- `home__read` silently truncates at 80,000 characters and reports a wrong file length `serve/rag.py:413` reads only `doc_text.MAX_RANGE_CHARS 4` (80,000 chars) before line-range slicing, so `total` in `doc_text.read_with_ranges` is the window's line count, not the file's.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**docker**
- `serve/mcp/web-scrape-mcp/tests` fails collection with `ModuleNotFoundError: No module named 'webscrape'` — that's the containerised HTTP scraper, not installed (and `docker` is still absent).
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**edit_file**
- If you do three: `offset` on the read tools, an `edit_file` tool with the line-number convention, and an outline fallback in `home__read` for files over ~16 KB.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**env**
- So the shim resolves against the system interpreter: If you want the newer pyenv-installed linters to be the ones actually used, set `PYENV_VERSION: 3.12.9` in the terminal server's `env` block in `mcp.yaml`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**externally-managed-environment**
- `terminal__install` against system `python3` returns pip's PEP 668 `externally-managed-environment` error (exit 1).
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-19-47.md</sub>

**gt-mcp-server**
- The vendored servers (`gt-mcp-server`, `ps5-control-mcp`, `web-scrape-mcp`) have their own venvs that are missing or broken here, so their tests can't run from this account; they're excluded from the root run rather than silently "passing".
  <sub>2026-10-04 · strata-chat-2026-10-04-05-16.md</sub>

**gt-telemetry/mcp_requests.jsonl**
- MCP tool calls are appended to `gt-telemetry/mcp_requests.jsonl`.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-06-37.md</sub>

**head**
- I checked the diff of every failing test file against `HEAD` — they are all untracked-at-HEAD files (`test_restart_script.py`, `test_assist_paths.py`, `test_hub.py`, `test_terminal.py`, `test_docs_mcp.py`, `test_terminal_mcp.py`, `test_contracts.py`) and none of their content changes are rename-related.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**home__reindex**
- `home__reindex` is asynchronous — `home__status` shows `walking` until it finishes.
  <sub>2026-10-08 · 2026-10-08-strata-chat-2026-10-08-16-41.md</sub>

**home__search**
- Identifying this as the Strata trace UI is inference from the tool names (`terminal__run`, `home__search`), which match the tools in this session; the screenshot itself carries no product label.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-36.md</sub>

**hypercore_**
- `serve/docs_mcp.py:93` reads `STRATA_DOCS_ROOT` while everything else is `HYPERCORE_`.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-03-30.md</sub>

**hypercore_rag_root**
- Environment-variable names are split. `serve/rag.py:23` reads `HYPERCORE_RAG_ROOT`, but `serve/mcp/tests/test_home.py` and `test_contracts.py` still set `STRATA_RAG_ROOT`.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md · also in 2026-10-10-strata-chat-2026-10-10-03-30.md</sub>

**inputschema**
- `test_contracts.py` — the fast pass a retest needs before the deep ones: every server in `mcp.yaml` starts, advertises exactly its documented 35 tools, every `inputSchema` is a well-formed object with typed arguments, unknown tools and bad arguments are answered as text rather than crashing the connection, and a server keeps answering after a bad call.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-39.md</sub>

**interpreter**
- sudoers/mcp.yaml: open items are the operator-rule fallback and the `INTERPRETER` grant.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**invisible-playwright**
- The installed command is `invisible-playwright`, with a hyphen.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-07-26.md</sub>

**lmnope**
- A real allowlist bypass in the terminal MCP. `serve/mcp/tests/test_terminal.py` fails: `` echo `lmnope` `` is accepted although the inner program is not on the allowlist.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**made test assert**
- I made the test assert the shipped values plus the invariant `max_tool_calls <= max_rounds` so the suite is green, but the number itself is your call: the recorded target says raise to 500, the working-tree edit lowered it to 200.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>

**make**
- `ninja` isn't installed; `terminal__build` falls back to `make`, which works.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-16.md</sub>

**max_context**
- Restart after editing `mcp.yaml`; `GET /mcp` reports the live `max_rounds`/`max_result_chars` and `GET /props` reports `max_context`, so you can confirm what the server is actually using.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>

**mcp.max_rounds**
- The rows with `not run` next to a "Not run" badge are read directly; why they were not run is inferred from the footer's stated cause — the 8-round limit (`mcp.max_rounds`) — not from anything else in the image.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-36.md</sub>

**mcp.py**
- One thing to know before it takes effect: the running server (PID 2491454) still reports the old settings — `GET /mcp` returns `{'max_result_chars': 6000, 'max_rounds': 20}` with no condense keys, because it loaded `mcp.py` before the patch.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**mcp_servers**
- Either drop servers the chat doesn't use (all six report zero calls in this instance), or set `mcp_servers` per profile so a coding chat doesn't carry the vision and scrape schemas.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**mcphub.usage**
- `McpHub.usage` is incremented from tool calls that run on their own threads (`serve/server.py:1208-1214`) with no lock, and `routes()` is recomputed on every call (`McpHub.call:593`, `server.py:1201`).
  <sub>2026-10-06 · strata-chat-2026-10-06-00-55.md</sub>

**missing_tools**
- `missing_tools` is now `[]` from `terminal__check`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**pyenv**
- Two of the three are cheap: `python3` and `pyenv` are already installed, and the MCP servers in `serve/mcp/` are Python — the plumbing for a fifth `terminal` server is the same pattern as the existing four.
  <sub>2026-10-05 · 2026-10-05-strata-chat-2026-10-05-23-07.md</sub>

**read_with_ranges**
- `read_with_ranges` returns the slice plus a pointer to the next one: Observed (`serve/mcp/tests/test_docs.py` case, passing): reading lines 3–4 of a 120-line file returns ` 3\tl3` and `continue with start_line=5`.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**reindex**
- `test_home.py` — search limit clamping, empty query, a second `reindex` while one is running, depth clamping, `structure` on a file, and `status` naming the root and db it is reading.
  <sub>2026-10-04 · strata-chat-2026-10-04-05-39.md</sub>

**round assistant turn**
- A round is one assistant turn that emits calls, and a turn can emit several calls in one round (`serve/server.py:3102`, `if run_them and rounds >= max_rounds`).
  <sub>2026-10-18 · 2026-10-18-10-28-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**run_all.py**
- The suite under `serve/mcp/tests/` (10 files, `run_all.py`) is outside that scope: So the whole MCP hub/browser/scrape contract surface is silently excluded from every routine run.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**run_with_mcp**
- `serve/server.py` — `run_with_mcp` calls it after each round's results are appended, re-prepares the prompt, and emits `("mcp", {"event": "condensed", ...})` so the client can see it.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-18-57.md</sub>

**sed**
- Fix: count lines from the full file, or state the window in the note ("only the first 80k characters are indexed by this call, use `terminal__run` with `sed`").
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**serve web_scrape_mcp retry**
- `serve/mcp/web_scrape_mcp.py:483` — the retry test is `if ("429" in str(e) or "503" in str(e))`.
  <sub>2026-10-06 · strata-chat-2026-10-06-00-55.md</sub>

**serve/**
- Fixing that means editing `serve/mcp/__init__.py`, and `serve/` is `root:root 644` (`stat -c '%a %U:%G' serve/mcp/__init__.py` → `644 root:root`), while the sudoers rules here allow only `sudo apt-get install -y nodejs npm git ripgrep jq`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**serve/mcp/__init__.py**
- Python resolves the package first: `serve/mcp/__init__.py` (1,411 lines) carries the same docstring header ("serve/mcp.py - tools from MCP...") and the same `if __name__ == "__main__"` block.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**serve/mcp/assist_mcp.py**
- Done and observable: `record_call()` (assist.py:340) appends JSONL, prints `[strata] assist …` to stderr (stdout is the MCP protocol), and both the HTTP server and `serve/mcp/assist_mcp.py` go through it.
  <sub>2026-10-09 · strata-chat-2026-10-09-15-05.md</sub>

**serve/mcp/browser_mcp.py**
- The design is deliberate — from the docstring of `serve/mcp/browser_mcp.py`: "Nothing browser-related is imported at start-up: `status` answers on a box with no engine at all and says which one is missing and how to get it." Auto-fetching a ~260 MB browser inside a tool call would be a multi-minute surprise, and the terminal default timeout is 30 s.
  <sub>2026-10-09 · strata-chat-2026-10-09-09-50.md</sub>

**serve/mcp/gt-mcp-server/**
- 3 × `serve/test_winjob.py` — job objects / power throttling are Windows-only Coverage note: the README names `serve/mcp/gt-mcp-server/` and `serve/mcp/ps5-control-mcp/` as separate projects with their own suites — neither directory exists in this repo, so there is nothing to run there.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-23-05.md</sub>

**serve/mcp/tests/readme.md**
- Also updated `serve/mcp/tests/README.md`: the coverage table was stale (191/190 vs the actual 204) and the telemetry suite is now listed in the no-server section.
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>

**serve/mcp/tests/run_all.py**
- And it runs: `serve/mcp/tests/run_all.py` from the fresh copy → 235/235 OK.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**syntaxerror**
- The live MCP server still holds the pre-patch module. Strata starts servers at boot, so `terminal__repl` through the hub still returns the old driver's `SyntaxError`, while every test run against the file on disk is correct.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-09.md</sub>

**terminal__build**
- `terminal__build` reuses a broken build directory. llama.cpp's configure failed earlier (`add_subdirectory given source "ui" which is not an existing directory`), and the second call skipped configure and failed on `gmake: Makefile: No such file or directory`.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**three entries unreadable**
- Three entries were unreadable, not failed: `serve/mcp/web-scrape-mcp/.venv/bin/python{,3,3.12}` are permission-denied symlinks.
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>

**tools/call**
- Playwright's sync API is thread-affine, but `stdio()` dispatches each `tools/call` on its own thread (inherited from `web_mcp.py`, where that's harmless because Firefox/Marionette is just a socket).
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-09-31.md · also in 2026-10-07-strata-chat-2026-10-07-09-31.md</sub>

**web-scrape-mcp**
- The existing `web-scrape-mcp` package is a well-built but structurally different animal from the servers Strata actually runs: Six tools, all preserved under closer-to-home names:
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**xvfb**
- Run `STRATA_TEST_LIVE=1 python3 serve/mcp/tests/run_all.py --server browser` once `xvfb` is present to close that gap.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-07-26.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**mcp.yaml** · 4 chats
- Not caused by the rename, but worth flagging — 11 pre-existing failures surfaced by the suites: `mcp.yaml` now registers seven servers while `test_hub__shipped_yaml` asserts six; the hub merge API moved (AttributeError in 8 merge cases); the terminal allowlist doesn't catch backtick substitution; and `docs_mcp.create_file(".ssh/authorized_keys")` is accepted where the guard should refuse it.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>
- Hub-level allow-list in `mcp.yaml` using the existing `template_tools(exclude=...)` (`serve/mcp/__init__.py:616`) to shrink the fixed tool block from 42 tools to the 38/36 from the merge audit.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-12-05.md</sub>
- Two env additions the running server needs regardless: I already added `TERMINAL_ALLOW_EXTRA` and `RUFF_CACHE_DIR` to `mcp.yaml`, but pid 2351071 was started with the old environment, so `terminal__run` still rejects `sudo` ("'sudo' is not on the allowlist") — as you saw in my test above.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>
- `mcp.yaml`'s `timeout_s: 90` caps calls; long suites must go through `terminal__process`, which is what I did throughout The suite runs in 3.8 s, versus 153 s before the REPL fix — the timeout was the dominant cost, and the fix removed it.
  <sub>2026-10-03 · strata-chat-2026-10-03-15-09.md</sub>

**--mcp-config** · 2 chats
- `serve/server.py` (5,015 → 5,037) — `GET /prompts` endpoint, the context condenser wired into the tool loop (`hub.needs_condensing(...)` / `condense_messages(...)`, triggered by prompt size not round count), and `mcp.yaml` support added to `--mcp-config`.
  <sub>2026-10-06 · strata-chat-2026-10-06-23-05.md</sub>
- Current contents: `server.py` resolves the path as: `--mcp-config` if it is a `.yaml`/`.yml`, otherwise `ROOT/mcp.yaml`; a JSON `--mcp-config` is added to the yaml rather than replacing it.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-05-16.md</sub>

**/home/ai-user0/.terminal**
- I added `user0` so the agent can write to `/home/ai-user0/.terminal`, which `mcp.yaml` sets as `TERMINAL_STATE_DIR`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/.terminal/cache**
- Fixed by re-adding `RUFF_CACHE_DIR: $HOME/.terminal/cache` to `mcp.yaml` (validated: `load_mcp_yaml` expands it to `/home/ai-user0/.terminal/cache`, and `ruff check` with that env returns "All checks passed!").
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**/home/ai-user0/s**
- Also note `path` is resolved against `ROOT`, not `cwd` — `terminal__check(path="tests", cwd=".../web-scrape-mcp")` looked for `/home/ai-user0/s`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**/home/ai-user0/strata/mcp.yaml**
- `sudo` is not on the terminal allowlist. I added `TERMINAL_ALLOW_EXTRA: sudo,setfacl,getfacl,visudo,apt-get,nproc` to the `terminal` server env in `/home/ai-user0/Strata/mcp.yaml`, but the running `terminal_mcp.py` process (pid 2351071) was started with the old environment, so it still rejects `sudo`.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md · also in strata-chat-2026-10-03-14-03.md</sub>

**/proc/2400540/environ**
- The `mcp.yaml` env I added is picked up (`PYENV_ROOT`, `RUFF_CACHE_DIR`, `TERMINAL_ALLOW_EXTRA` all present in `/proc/2400540/environ`), so `sudo`, `setfacl`, `getfacl`, `nproc` are now on the allowlist and `sudo -n apt-get update` succeeds without a password.
  <sub>2026-10-03 · strata-chat-2026-10-03-18-25.md</sub>

**__init__.py.new**
- `__init__.py.new`, `test_docs_mcp.py.new` — the full patched files None can be applied from this profile: `serve/` is `root:root 755/644` and sudo here allows only `apt-get install -y nodejs npm git ripgrep jq`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**_check_server**
- `serve/mcp/__init__.py` (838 → 862 lines, matching the 24 added lines): `_check_server` now returns `_expand_paths(cfg)`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**args**
- Only `args` is replaced; `sampling`, `reasoning_close_retry`, `mcp_servers` etc.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>

**batch_download**
- Their names and descriptions match `web_scrape_mcp.py` exactly — `extract_media`, `download_file`, `download_page_media`, `batch_download`, `file_info`, `list_downloads`, plus `status` — which suggests the server I added to `mcp.yaml` is now being served to me.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**camoufox**
- Every tool, driven against the real patched Firefox: Two honest caveats: `webgl.vendor`/`renderer` come back `null` in headless without a display, so the probe reports what's actually there rather than assuming a value; and Camoufox isn't installed here, so `invisible_playwright` (wrapper 0.27.0) is the engine the adapter selects — the `camoufox` and `playwright` branches stay as fallbacks.
  <sub>2026-10-07 · 2026-10-07-strata-chat-2026-10-07-09-31.md</sub>

**condense**
- `git diff --stat`: `serve/mcp.py +172`, `serve/server.py +73`, `serve/test_mcp.py +68`, `docs/DETAILS.md +64`; 54 added lines mention `condense`.
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

**cors_origins**
- And per `docs/INSTALL.md:175`, re-running setup rewrites the keys it owns and keeps ones you added (`sampling`, `mcp_servers`, `cors_origins`…), saving the previous file as `strata-<model>.json.bak`.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**defaults**
- `max_result_chars` is 20000 in `DEFAULTS` (`serve/mcp/__init__.py:39`); the 6000 value comes from `mcp.yaml:25`, applied at `serve/mcp/__init__.py:1034`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**feder-cr/firefox_antidetect_patch**
- The anti-detect engine is invisible-playwright's own C++-patched Firefox (from `feder-cr/firefox_antidetect_patch` / `r00tb00t/firefox_antidetect_patch`), MPL-2.0 — the fingerprint is set in the engine, not injected by JS.
  <sub>2026-10-09 · strata-chat-2026-10-09-09-50.md</sub>

**fixed declaring attribute**
- Fixed by declaring the attribute on the class (`srv: McpServer` at `serve/mcp/tests/test_docs.py:23`).
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**head**
- `terminal__compare git_ref=HEAD` can't work for the hub files — `git ls-files serve/mcp` returns 0: the MCP servers were added outside the repo's tracked set, so `HEAD` has no version of them.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**home__read**
- `ruff check` and `mypy` clean on the four changed files One caveat: the MCP server processes already running in this session are the pre-edit code, so `home__read` and `terminal__query` here still behave the old way until those servers restart.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**method**
- `tools/list_changed` is ignored `serve/mcp/__init__.py:172` returns on any message with a `method`, so `notifications/tools/list_changed` is dropped.
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-14-10.md</sub>

**patches/test-docs-mcp-portable.patch**
- The single failure is exactly the portability problem from the last turn, not a tool bug: Fixed in `patches/test-docs-mcp-portable.patch` (derive the repo root from the already-imported package, use the running interpreter): Verified: `pytest _test_docs_mcp_patched.py` → 7 passed.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**serve/mcp/__init__.py**
- `serve/mcp/__init__.py`, three edits, matching the template's own rule: `git diff HEAD` on that file: 7 added, 6 removed, exactly these three hunks.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md</sub>

**serve/mcp/tests/test_hub.py**
- Added to `serve/mcp/tests/test_hub.py` in the existing `@case` style, one case per concern: Run before the patch: `Ran 54 tests ...
  <sub>2026-10-16 · hypercore-chat-2026-10-16-17-22.md · also in strata-chat-2026-10-06-14-10.md</sub>

**serve/mcp/web-scrape-mcp/tests**
- Fixed with a `SUITE_PREFIXES = ("tests", "serve/mcp/tests")` constant; the scope test now also asserts the vendored `serve/mcp/web-scrape-mcp/tests` stays out.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**terminal__process**
- `terminal__process` stays broken until the restart, because its env is fixed at spawn.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**footer verbatim tokens**
- Footer, verbatim: `5,027 tokens · 86.5 tok/s · projection on · 43 tool calls · stopped at the limit of 8 tool rounds (mcp.max_rounds)` Each row has a wrench icon on the left and a chevron (expand) on the right; the status badges are amber for "Not run", gray for "Done".
  <sub>2026-10-05 · strata-chat-2026-10-05-23-36.md</sub>

## Gotchas

things that bit once and will bite again


**ai-agent**
- The breakage is that `mcp.yaml` was never repointed from `ai-agent` to `ai-user0`.
  <sub>2026-10-05 · strata-chat-2026-10-05-20-19.md</sub>

**delete_file**
- `edit_file` bypasses the ownership guard that `write_file`/`delete_file` enforce `serve/docs_mcp.py:255` and `:276` call `tool_permissions.decide(...)`; `edit_file` (`:206`) does not, and then calls `note_created(target)`, so editing a file the session never created both succeeds and grants the session write/delete rights over it.
  <sub>2026-10-06 · strata-chat-2026-10-06-14-10.md</sub>

**exception**
- Six sites catch `Exception` and do nothing: `serve/server.py` (2), `serve/telemetry.py`, `serve/mcp/__init__.py`, `serve/mcp.py`, `serve/attached_engine.py`, `serve/mcp/tests/harness.py`.
  <sub>2026-10-19 · 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md</sub>

**home__read**
- Outline-before-content for large files — `home__search`/`home__read` on a 300 KB file should return a symbol outline or a section index, and the tool description should say "this is a successful response; do not retry without line ranges." 5.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**httptransport**
- If you want the 54-era leftovers prevented structurally rather than by a per-class `terminate`, the durable fix is for `HttpTransport` to own its server process the way `StdioTransport` does — worth doing only if you plan to run HTTP-transport servers from `mcp.yaml` in production too.
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**mcp.yaml**
- Known gaps — the guard is not a shell parser, gdb needs `-g` for useful output, `ptrace_scope` limits breakpoints, Docker volumes intentionally still reference user0, and `mcp.yaml` requires a restart.
  <sub>2026-10-03 · strata-chat-2026-10-03-19-51.md</sub>

**serve/test_mcp.py**
- Tests — `serve/test_mcp.py` now has `test_results_condense_under_context_pressure` (with an 8,192-token mock context, rounds 1–2 are untouched, round 3 crosses 0.4 and condenses, including the current round when still over the mark) and `test_condensing_off_keeps_every_result` (a 5-round loop with `condense_at: 0` never condenses).
  <sub>2026-10-04 · strata-chat-2026-10-04-03-57.md</sub>

## Still open

not done, deferred, or waiting on a decision


**condense_at**
- Remaining open items are the sudoers line-41 direction, the INTERPRETER group decision, and the unstarted Medium item — token accounting/compaction in the tool layer, which would sit next to the existing `condense_at` / `condense_chars` settings in `mcp.yaml`.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

**initial_title**
- Not implemented, and why: subagent context isolation needs an agent-level `spawn` tool, not a change to the tool layer (the existing substitutes are `save_to` and `terminal__process` log files, which the docs tools can read); streaming `ToolCallEventStream`/`initial_title` has nothing to render into here; a lazy skill catalog would duplicate what `prompt.json` already carries.
  <sub>2026-10-05 · strata-chat-2026-10-05-23-07.md</sub>

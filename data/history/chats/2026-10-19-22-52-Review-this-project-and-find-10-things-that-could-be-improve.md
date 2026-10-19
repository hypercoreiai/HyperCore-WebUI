# 2026-10-19-22-52-Review-this-project-and-find-10-things-that-could-be-improve.md

2026-10-19 · 709 KB · 2 requests · topics: deployment-permissions, issue-log, mcp-tooling, testing-quality, web-ui-monitor

## What was asked
- Review this project and find 10 things that could be improved: /home/ai-user0/HyperCore-WebUI
- execute the first 4 suggestions

## What came of it
- Baseline: `pytest -q` gives 448 passed in 77.16 s, `.mcp_test_state.json` parses and is current (loop 66, `remaining_queue: []`, 1 recorded error).
- Python resolves the package first: `serve/mcp/__init__.py` (1,411 lines) carries the same docstring header ("serve/mcp.py - tools from MCP...") and the same `if __name__ == "__main__"` block.
- `serve/mcp.py` is a stale pre-move copy that nothing can import, yet it is tracked in git and still shows up in lint output (`serve/mcp.py:235 PLW1510`, `serve/mcp.py:596 S110`).
- Fix: delete `serve/mcp.py`, and rename the package entry so the docstring stops lying about its own path.
- Also dedupe `McpError`, `McpTimeout`, `McpCancelled`, `_Slot`, `_wait`, `result_text`, which are defined in both files.
- `pytest.ini` sets `testpaths = tests` and `tools/run_tests.sh` runs `pytest tests/ -q`.
- The suite under `serve/mcp/tests/` (10 files, `run_all.py`) is outside that scope: So the whole MCP hub/browser/scrape contract surface is silently excluded from every routine run.
- There is no `.github/`, no `pyproject.toml`, no `Makefile`, no pre-commit.
- The consequence is that the quality findings just sit there: 39 of the ruff findings are auto-fixable (`ruff check --fix`).
- 51 files in `serve/` and `tools/` are executable with no shebang (EXE002), which is a `chmod` away from clean.
- Fix: add a minimal CI that runs `tools/run_tests.sh`, `ruff check`, and `mypy`, and add a `pyproject.toml` with the ruff/mypy config so the gate is reproducible rather than ad hoc.
- Largest units measured with `ast`: The project already has the right seams (`quant_store.py`, `build_api.py`, `engine_supervisor.py`, `offline_engine.py`, `strata_hook.py`), so the split is a matter of moving the OpenAI lane, the Anthropic lane, the vision path, and the MCP tool loop out of `server.py` into modules that already have their own tests.
- `mypy` reports 58 `union-attr` and 52 `assignment` errors, concentrated in the deferred-engine block: One variable is reused for an engine, a request handler, and `None`.
- `CHANGELOG.md`, `README.md`, `serve/quant_store.py`, `serve/server.py`, `serve/web/app.js`, and 4 new test files (`conftest.py`, `docs/strata-0.2.0-adjustments.md`, `tests/test_build_off_flags.js`, `tests/test_build_source_choices.js`) are untracked or modified.
- `pytest`, `ruff`, `mypy`, and `pyright` are used by the project but are not declared anywhere.

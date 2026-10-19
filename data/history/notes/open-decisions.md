# Things decided but not yet done

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

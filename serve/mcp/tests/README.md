# MCP tool retest — one command, one report

Every MCP server HyperCore ships has its tests here, grouped by server and by tool. When the
chat is asked to "retest the tools", this is the run: it exercises every tool each server
advertises, plus the guardrails around it, and prints one table with the same columns every
time, so a retest can be compared with the last one.

```
serve/mcp/tests/
  harness.py         the stdio client, the fixtures, the @case tag
  run_all.py         runs every module and writes the report
  test_contracts.py  every server starts; every tool has a schema; bad arguments are answered
  test_terminal.py   terminal: run, pipe, process, repl, symbols, check, install, build,
                     compile, tidy, debug, query, compare, status
  test_scrape.py     scrape: extract_media, download_file, download_page_media,
                     batch_download, file_info, list_downloads, remove_downloads
  test_docs.py       docs: create_file, read_file, edit_file, write_file, delete_file, list_directory
  test_home.py       home: status, search, structure, read, reindex
  test_web.py        web: status, search, browse
  test_browser.py    browser: status, fetch, links, probe, screenshot, close
  test_hub.py        the hub itself: mcp.yaml, tool naming, the result cap, timeouts,
                     cancellation, restart after a crash, condensing, the HTTP transport,
                     and the Tier 1 merges (browse, status, ls)
```

The line-range, outline and edit-matching primitives those tools are built on are unit-tested
against strings, with no server and no workspace, in `serve/test_doc_text.py`.

## Running it

```bash
python3 serve/mcp/tests/run_all.py                          # every server, live cases included
python3 serve/mcp/tests/run_all.py --no-live                # everything that needs no network or browser
python3 serve/mcp/tests/run_all.py --server terminal        # one server
python3 serve/mcp/tests/run_all.py --verbose                # every case, not only the failures
python3 serve/mcp/tests/run_all.py --compare previous.json  # what changed since the last run
python3 serve/mcp/tests/run_all.py --json /tmp/report.json  # where to write the report
```

The default report is `/home/ai-agent/.terminal/retest_report.json`. Exit code is 0 only
when nothing failed or errored; a skipped case is not a failure.

Nothing here touches the real `/home`, the real downloads folder, or a running chat: each
server is started with its root pointed at a temporary directory (`HYPERCORE_DOCS_ROOT`,
`HYPERCORE_RAG_ROOT`, `SCRAPE_DIR`, `TERMINAL_ROOT`). The hub tests use `tests/mcp_fake_server.py`,
a dependency-free MCP server, and the scrape tests fetch from a fixture server on localhost.

`HYPERCORE_TEST_LIVE=0` (or `--no-live`) skips the cases that need the internet or Firefox.
The web tests find Firefox the same way the server does — `HYPERCORE_FIREFOX`, then
`~/.local/firefox/firefox/firefox`, then `firefox` on PATH — so they run when a browser is
present instead of being skipped.

## The report shape

The columns never change:

```
server        tools  cases   pass  fail  error  skip  status
------------------------------------------------------------
contracts         1      6      6     0      0     0  OK
docs              6     26     26     0      0      0  OK
...
TOTAL                  167    167     0      0      0  OK
```

The JSON behind it:

```json
{
  "generated": "2026-10-04T05:26:46+00:00",
  "python": "3.14.4",
  "repo": "/home/ai-agent/HyperCore",
  "live": true,
  "servers": {
    "terminal": {
      "advertised": ["build", "check", ...],
      "cases": 49, "pass": 49, "fail": 0, "error": 0, "skip": 0, "elapsed_ms": 6452,
      "tools": {"run": {"cases": 12, "pass": 12, "fail": 0, "error": 0, "skip": 0, "slowest_ms": 1003}},
      "tools_tested": ["build", "check", ...],
      "ok": true,
      "failures": [{"tool": "run", "case": "...", "note": "the last line of the traceback"}]
    }
  },
  "uncovered": [],
  "totals": {"cases": 167, "pass": 167, "fail": 0, "error": 0, "skip": 0, "ok": true, "elapsed_ms": 13412},
  "rows": [{"server": "docs", "tool": "create_file", "case": "...", "test": "Docs.test_docs__create",
            "outcome": "pass", "note": "", "ms": 1}],
  "diff": ["tool web.search: pass 2->3"]
}
```

`uncovered` lists any tool a server advertises that has no case — the report says so rather
than quietly reporting a green run.

## Covering a new tool

A test method is tagged so the report can group it by server and tool:

```python
@case("terminal", "run", "long output is clipped and says how much is missing")
def test_terminal__run_clipped(self): ...
```

Add the tool's name to the module's `ADVERTISED` list and to `EXPECTED` in
`test_contracts.py`; the runner then checks that the tool has at least one case and that its
`inputSchema` is well formed.

## What is covered

| server   | tools | cases |
|----------|-------|-------|
| contracts| 6 servers | 8 |
| terminal |  14 | 53 |
| docs     |   6 | 46 |
| home     |   5 | 22 |
| scrape   |   7 | 25 |
| web      |   3 | 11 |
| browser  |   6 | 17 |
| hub      |   — | 46 |
| **total**| **41 tools** | **229** |

Four servers advertise 41 tools between them; the hub folds six of them into three (`browse`,
`status`, `ls`), so the model is offered 38. A merge only applies when every tool it folds up
is present, so a server that failed to start still offers its own tool.

The restart script, the text primitives behind the document tools, and the /proc readers in
`serve/telemetry.py` are tested separately, with no server and no workspace:

```
python3 -m unittest serve.test_doc_text serve.test_restart_script serve.test_telemetry   # 40 cases
```

`serve.test_telemetry` feeds fixture /proc text through a stand-in for `open()` and asserts every
handle is closed, then hammers the real /proc files 400 times and checks the descriptor count does
not grow - the leak that made the background connections lock up.

The servers under `serve/mcp/gt-mcp-server/` and `serve/mcp/ps5-control-mcp/` are separate
projects with their own test suites and are not part of this run.

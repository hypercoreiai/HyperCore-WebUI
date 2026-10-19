# The tool hub as it now works

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

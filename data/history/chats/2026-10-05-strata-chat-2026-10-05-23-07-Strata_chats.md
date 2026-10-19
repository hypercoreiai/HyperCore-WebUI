# strata-chat-2026-10-05-23-07.md

2026-10-05 · 1021 KB · 6 requests · topics: deployment-permissions, hardware-environment, issue-log, mcp-tooling, setup-configuration, testing-quality, web-ui-monitor

## What was asked
- review this project: https://github.com/zed-industries/zed.git for ideas on structuring coding tool calls and managing context.
- grade each change on a scale of high medium low as to how it would improve the current tool sets
- prototype the high changes
- update the tools tests as needed, clean up the zed scratch if it no longer useful
- implement the medium and low priority changes and up date the tools tests. If you need a restart during the updates just let me know
- what if you generate a restart script that stops the current /home/ai-user0/Strata/ni_run-q2_0.sh (no image) or /home/ai-user0/Strata/yi_run-q2_0.sh (yes image to be added) .

## What came of it
- The context window is 262,144 tokens (max_context=262144).
- The `docs__create_file` bug I found earlier is a symptom of this: I had no way to `mkdir` or check writability, so I guessed.
- Sandboxing — `scrape__status` reports `private_ips_blocked: true` and a 200 MB cap; `docs` is rooted at `/home`.
- `terminal__run` needs its own per-call timeout, or a `sleep 99999` hangs the whole session.
- Output caps — `max_result_chars: 20000` already exists; truncation must be explicit ("showing 4,000 of 21,318 bytes; use `offset` for more"), not silent — the `home__read` truncation I hit earlier was invisible.
- Idempotence and cleanup — `scrape__remove_downloads` reports files and bytes removed; `docs__delete_file` has no equivalent at all, and my test files in `/home/ai-user0/tmp` are still there because of it.
- `terminal__repl` — a persistent Python session is the fastest route to understanding and iterating on OO code: instantiate a class, inspect it, subclass it, test it, in one round trip.
- `terminal__pipe` — pipelines are how you actually navigate a large codebase (`rg class | wc -l`, `find … | xargs grep …`).
- Two of the three are cheap: `python3` and `pyenv` are already installed, and the MCP servers in `serve/mcp/` are Python — the plumbing for a fifth `terminal` server is the same pattern as the existing four.
- Repo shape: ~24 built‑in tool modules (`_tool.rs`), a compile‑time registry, permission layer, sandboxing, skills, subagents, and a compaction subsystem.
- Execution streams through `ToolInput` / `ToolCallEventStream`, so progress and result are separate events.
- Usage rules live in the schema, not the prompt. `GrepToolInput`'s doc comment carries the pagination contract ("Results are paginated with 20 matches per page.
- Invariants checked at compile time. The `tools!` macro in `tools.rs` builds `ALL_TOOL_NAMES` from `<$tool>::NAME` and a `const _: () = { const fn str_eq(...) }` block that fails the build on duplicate names.
- Tools are tested with golden diffs. `crates/agent/src/tools/evals/` contains per‑task fixtures (`before.rs`, `after.rs`, `possible-01.diff` … `possible-07.diff`) behind a `unit-eval` feature flag.
- Permissions are a separate layer from execution. `tool_permissions.rs` returns `ToolPermissionDecision::{Allow, Confirm, Deny}` from: hardcoded security rules → per-tool global default (`ToolPermissionMode`) → path rules (`decide_permission_for_paths`) → composed with `most_restrictive`.

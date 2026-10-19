# unknown-context.md

unknown · 3 KB · 0 requests · topics: engine-internals, memory-vram, prompting-behaviour, web-ui-monitor

## What was asked

## What came of it
- Diff against the backup — exactly the four changes, nothing else: Plus the profile alignment: `strata-q2_0.shared-settings.json` copied to `ni_strata-q2_0.shared-settings.json`, so the Chat-tab defaults (temperature 0.7, top_p 0.95, top_k 20, reasoning_effort high) now belong to the profile that's actually running.
- Before this, `GET /settings` returned `{"shared": false, "defaults": {}}` — the settings existed under a name no process read.
- And the two browser-server fixes: `WORKER_TIMEOUT_S` 120 → 80 (under the hub's 90 s ceiling, so a slow page returns an error rather than a killed call), `STRATA_BROWSER_MAX_CHARS` 12000 → 6000 (matching `max_result_chars`, so a page isn't truncated twice).
- `svc.config_path` and the engine `args` are read at startup, and `ensure_loaded()` reuses that args list — so a `/v1/unload` + `/v1/load` cycle will not pick up `--vram-reserve-mib`.
- `effort_position: "end"` needs engine ≥ 0.1.39.
- You're on 0.1.40, so it will apply; on startup it prints `effort_position end: a request's non-default reasoning effort goes right before the answer, so changing it keeps the cached conversation` and adds `--tail-role-token` to the args.
- `--max-context 262144` — the largest prompt in 5,773 requests was 221,987 and the typical one is 135k; `--kv-grow` isn't available in resident mode, so this is a real trade-off rather than a clean fix.
- Deleting `strata-q2_0.json` / `yi_strata-q2_0.json` — the duplicates are still there.
- `strata-q2_0.json` is the name setup writes, so removing it would just be recreated on the next setup run; I'd rather you decide which profile name you want to keep.
- `--tail-role-token 8678` is the server resolving `effort_position: "end"` into the engine flag — token 8678 is `system`, so the effort turn is checkpointed in front of it.
- (previously `{"shared": false, "defaults": {}}`)
- `max_tokens: 400000` on a short prompt now returns a completion instead of a 400 — `fit_max_tokens` is doing its job.
- Also, while checking I noticed the config passes `--spec 4` but `/metrics` reports `"spec": 6`; the engine is apparently deriving the verify window from the MTP draft head rather than the flag.
- Switching agent profiles, adding an MCP server (I added `browser` this session — the tool catalog is part of the system prompt), or changing reasoning effort when it's rendered at the top of the prompt.
- This is exactly what `effort_position: "end"` fixes: `serve/server.py:1784` adds `--tail-role-token 8678` so the engine checkpoints in front of the effort turn, and "changing it keeps the cached conversation".

# clean_add_timing_strata-chat-2026-10-16-02-09.md

2026-10-16 · 508 KB · 4 requests · topics: deployment-permissions, engine-internals, hardware-environment, issue-log, memory-vram, open-decisions, performance-benchmarks, prompting-behaviour, testing-quality, web-ui-monitor

## What was asked
- Make a report about how /home/ai-user0/HyperCore-WebUI derives the token counts per second for decode, thinking, prefill, bus (from ssd, from ram, from cpu) and the rate for any token traffic not listed here.
- what are the actual core code phases of the system?
- Which of these have transfer rates that can be monitored to to identify the impact of the engines flags and associated values?
- How can this be transferred into /home/ai-user0/opyuna_tune to ensure when the best possible performance -> system resource balance is reached? Done — the transfer telemetry is now wired into the tuner. Here's how it works and what changed.

## What came of it
- Short answer: there are exactly two clocks in the system — the engine's C++ `steady_clock` and the server's Python `time.time()` — and every "tok/s" number is a division of one of those.
- Decode and prefill are real rates; thinking has no rate of its own; and the SSD/RAM/CPU "bus" figures are per-token counts and per-token milliseconds, not rates — anything that reads like a bus rate is that count multiplied by the decode rate.
- `serve/server.py:94` is explicit about why the live number is a window rather than a mean: a mean reads `1/elapsed` at the first token (the Monitor showed five-digit numbers) and undershoots for the first second.
- `serve/test_server.py:844` (`LiveRate`) is the test that pins that behaviour.
- Note the multi-GPU/pipelined path counts tokens per engine idle period (`multi_n`), so its `tok_s` is an aggregate across concurrent requests, while each request's own `DONE` still carries its `produced` and `decode_ms`.
- There is no thinking rate. `in_think` is a state flag (`generate.cpp:1716`, `:2003`); reasoning tokens are counted in the same `produced`/`generated` counter as answer tokens.
- The thinking budget is enforced in tokens (`produced >= req_think_budget` → force `</think>`), and the loop guard uses different windows while thinking (`STRATA_GLM_LOOP_THINK` 256 vs `_ANSWER` 1024).
- The only thinking-specific output is the phase label — `_note()` sets `s["phase"] = "thinking"` for `ev.kind == "reasoning"` — so the dashboard shows "thinking" with the decode tok/s attached.
- Per MoE route of `K` experts, `glm_fast_path.cu:3700–3776` partitions every routed expert into exactly one bucket: So the reported quantities are counts per token (`ram_fetch`, `disk`, `promo`, `cpu_experts` — the dashboard's unit is literally `/tok`, `app.js:263-277`) and milliseconds per token (`disk_ms/tok`, `cpu_ms/tok`).
- `tools/glm_cache_sim.py` and `tools/glm_tier_replay.py` simulate the same accounting (`reads/tok`, `MB/tok`, `vram%`, `ram%`, `off-card ms/tok`).
- The actual bus bandwidth is measured separately (`stream_bandwidth()`, and `load_main.cpp:84` — "the bus is the ceiling, so measure it at the granularity the engine actually uses"), because a 1.38 MB copy does not reach the bus's rated throughput.
- Everything else is the complement of the listed counters, and its rate is always `decode_rate × (per-token count)`:
- `tok_s` (live) and `decode_tok_s` (per request) can disagree: one is a 2 s window, the other a whole-request mean.
- The engine's `STAT` lines are emitted every 8 tokens; the server keeps only the last 120 in `stat_history` for sparklines.
- The CPU lane is not a byte stream: `pcie_share` decides how many of those experts are copied over PCIe versus computed on the CPU, so `cpu_experts/tok` and `ram_fetch/tok` trade against each other.

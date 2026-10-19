# Performance and benchmarks

Accumulated from 27 transcripts, 40 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**Decode.** On the 4090 with the Q2_0 pack, decode runs ~140–150 tok/s on a cold prompt and
higher with the conversation cache warm. Speculative drafts are accepted at 79/82, 9/10 — the
MTP layer is doing real work.

**Prefill.** A 116k-token prompt read from scratch takes ~31 s (≈3.7k tok/s on the first read);
the same window on a subsequent turn reads 29 tokens in 166 ms. Prefill rates reported in the
Monitor range from ~595 to ~905 tok/s depending on how much of the prompt was already cached.

**The worker's own throughput.** Aggregate tokens reaching the host, 4000 tokens per stream,
median of 3: at one stream the worker is slower than the engine — 124k vs 215k tok/s. That gap is
the IPC, not the model.

**Three knobs move tok/s.** `pcie_frac`, `spec_min_p` and `pool_workers` are the ones calibrated;
the other ~357 environment variables are A/B switches used by the parity and bench harness, not
user-facing tuning.

**`STRATA_DEFERRED_REGISTER=1` is opt-in and Windows-only** because it measured about 10% slower
decode.

**A number in a topic file is only as good as the machine it was measured on.** Anything from
another box is marked as such in the entry's provenance.


## What is true now

how the thing works, as the chats established it


**active research frontier** · 2 chats
- Once mlockall is active and memory limits are lifted, execute a fast stress run using your local benchmarking tools: python3 tools/backend_parity.py --repeat 3 1.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>
- The active research frontier is durable memory — hypergraph-based systems such as HGMEM that reason over relationships rather than retrieving passages — and inference-efficient long-context methods such as TTT-E2E, which maintains constant latency at 128k tokens.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-12-03.md</sub>

**stub ttft_ms prefill_ms** · 2 chats
- In the stub, `ttft_ms == prefill_ms` exactly — 45 and 45 in the wire_test output — and `ttft - prefill` is always 0.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- Because queue_hold_ms = ttft_ms - prefill_ms is a simple subtraction, computing it inside ipc_parser.py is straightforward.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**--gen**
- What is in place Usage `--gen` maximises generation tok/s, `--prefill` maximises prompt tok/s, `--latency` minimises `prompt_ms + decode_ms`; all three are mapped to one maximisation score so a failed boot lands at the bottom of the range.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-14-31.md</sub>

**.mcp_test_state.json**
- `.mcp_test_state.json` updated to loop 93, timestamped, checkpoint entry appended to `done`, `next_step` set to the three remaining hardware items (NATIVE_EXPERTS parity with Strata 1.43, loan/watchdog bounce side-stream, ple-io drop-cache-on-exit).
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**/home/ai-user0/hypercore-webui**
- Done. The calibrate/benchmark option from Strata_1.43 is now in `/home/ai-user0/HyperCore-WebUI`, tested, and the state file is updated (loop 89).
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**/home/ai-user0/hypercore-webui/setup.py**
- Both symptoms trace to the supervisor refactor, not to a missing feature. `grep -cE "calibrat"` → 24 in both `/home/ai-user0/Strata/setup.py` and `/home/ai-user0/HyperCore-WebUI/setup.py`, and the offer block is identical apart from the name: The prompt is emitted after Step 4 (engine + image encoder).
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**/home/ai-user0/lm-eval-runs/peer_qwen3_limit10.json**
- To make it an apples-to-apples comparison I then re-ran `Qwen/Qwen3-0.6B` on the peer's 1060 with `--limit 10`, so both of us saw the identical 60 items (100.4 s eval, mirrored to `/home/ai-user0/lm-eval-runs/peer_qwen3_limit10.json`).
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**aggregate tokens reaching**
- Aggregate tokens/s reaching the host, 4000 tokens per stream, median of 3: At one stream the worker is slower — 124k vs 215k.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**backend_parity**
- Fix the benchmark, not the engine — `backend_parity` reports total tokens / total time, which is dominated by the ramp.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

**boot_ms**
- With `--repeat 3`, `gen/prefill/latency` are medians but `prompt_ms`, `decode_ms`, `wall_ms`, `boot_ms`, `disk_read_mb` come from the single best-scoring boot (`bayesian_tune.py:330-333`), so the transfer columns describe a different boot than the median throughput.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**memory hypergraph based**
- Memory (hypergraph-based systems like HGMEM), long-context efficiency (TTT-E2E, which keeps latency constant at 128k tokens) and self-assessment are the current research frontier.
  <sub>2026-10-03 · 2026-10-03-strata-chat-2026-10-03-12-03.md</sub>

**pcie_frac**
- The "tune" surface is deliberately tiny. Three knobs (`pcie_frac`, `spec_min_p`, `pool_workers`) are calibrated because they are the three that actually move tok/s; the 357 env vars are mostly A/B switches used by the parity and bench harness, not user-facing tuning.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-03.md</sub>

**src/interface/worker_main.cpp**
- `src/interface/worker_main.cpp` — the failure is now self-diagnosing: `tools/backend_parity.py` / `tools/bench_flood.py` — pid-namespaced paths, matching the pattern `tools/calibrate.py` already used.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-16-40.md</sub>

**warm engine before**
- Warm the engine before the benchmark gate. `backend_parity.py --repeat 3` measures run 1 as a real datapoint; it should discard or pre-warm it, otherwise every CI number is a cold-start number.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-16-40.md</sub>

**what does branch**
- What does the V1 branch report for TTFT? It sets `ttft_ms = 0`, which for a v1 worker that did emit tokens reads as "instantaneous" rather than "not measured".
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**--builds**
- `--builds` would delete `build-shared/hypercore-worker` and `build/libhypercore_api.so`, which `tools/bench_flood.py`, `tools/backend_parity.py`, `tools/native_probe.py` and the calibration I just fixed all read by default.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**bench.sh**
- The calibrate/benchmark work is in `setup.py`, not `setup.sh`, so the option had to be added to `black_box`'s `setup.py` and given a `bench.sh` entry point next to `setup.sh`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**decode_ms**
- Same measurement source. Both take tok/s from the engine's own `decode_ms`/`generated` counters rather than wall-clock guessing, and both run greedy (calibrate pins `temperature=0`; the tuner relies on the engine's greedy default with a fixed prompt and fixed `max_new`).
  <sub>2026-10-15 · strata-chat-2026-10-15-19-50.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**derived figures median** · 2 chats
- The standard error of a median-of-3 is 2.43 tok/s (exact `sqrt(0.374)·σ`; 2.65 by 20000-sample simulation), not 2.9.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md · also in hypercore-chat-2026-10-11-15-51.md</sub>
- My log-derived figures (median 155 tok/s decode across 5,773 requests, 142 tok/s on a live 645-token generation) are steady-state numbers from a warmed expert cache and heavy prefix reuse.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**ranking puts fully** · 2 chats
- That is also why the ranking is so noisy: grouping the 75 logs by config, the median max/min spread within a single config is 1.31x, worst 3.78x (266.0 vs 1005.1 tok/s for the same config, three boots).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- Ranking puts a fully resident pack above one that streams its experts from the SSD, which is the distinction setup.py itself documents (UD-Q4_K_XL measures 7–8.5 tokens/s on a 64 GB PC).
  <sub>2026-10-10 · strata-chat-2026-10-10-08-05.md</sub>

**setup calibrate works** · 2 chats
- `MIN_GAIN = 0.03` (`tools/calibrate.py:39`) is 4.3 tok/s at 142, which is only 1.5 standard errors.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>
- `./setup.sh --calibrate` works — the repo's own state record shows a real run on this box (`--pcie-frac 0.75, --spec-min-p 0.30, --pool-workers 12, 2362 tok/s`).
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**--resident-experts**
- Right as they are: `--pcie-frac 0.00` (correct — zero file reads), `--expert-cache auto` (15.05 GiB free → 11,022 slots), `--resident-experts`, `--spec-min-p 0.70` (calibrated at 141.6 tok/s).
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**/home/ai-user0/.config/hypercore/settings.json**
- `/home/ai-user0/.config/hypercore/settings.json` already holds a calibration for this exact PC and model (`RTX 4090|24GB|i9-14900KF|31GB|qwen3.8-flash-next-q2_0|262144|text`, 2026-10-05, 141.6 tok/s), so `cal` is not `None` and setup applies the saved settings instead of asking to re-measure.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**0.0**
- The server stores that as `prefill_tok_s_mean` and reports it only while `first_token is None` (`_prefill_tok_s_mean()` returns `0.0` the moment generation starts) — that's the `test_prefill_rate_excludes_cached_tokens` case: 12,000 prompt tokens, 8,000 cached, 2,000 read in 2,000 ms → 1000 tok/s, not 6000.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**calibrate_glm.py**
- `knee()` finds where more resource stops buying throughput (same 3% / 70% rule as `calibrate_glm.py`).
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md · also in clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**caveat these production**
- One caveat: these are production numbers from a server that is in active use — another client was running 130k-token prompts while I measured, so treat 142 tok/s as "in real use with contention" and the 155 median as the steady-state figure.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**check halves dense**
- (The `CHECK=1` halves dense throughput — 78 tok/s in your run is expected, not a regression.) Three outcomes, three fixes: Paste the trace and the report line and I'll go straight to the offending call site.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**config problem three**
- Your config isn't the problem. Three separate things are being conflated: Your "averaged 150 tok/s" matches the Oct-12 median exactly.
  <sub>2026-10-14 · strata-chat-2026-10-14-17-09.md</sub>

**decode_ms**
- Now: Tested, not asserted: `test_prompt_timing_does_not_move_with_the_draft_cycle` runs the same request with and without a 150 ms draft cycle and requires `prefill_ms` to stay put while `decode_ms` and `ttft_ms` move.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**edge device inference**
- Edge/on-device inference is now production-viable — 7B–8B models run on consumer hardware with sub-50 ms latency and zero per-inference cost, which matters for privacy, latency and sovereignty.
  <sub>2026-10-03 · strata-chat-2026-10-03-12-03.md</sub>

**internal arithmetic self**
- Internal arithmetic is self-consistent: 1000/8.90 = 112.4 ms/tok, 112.4−62.2 = 50.2 ms → 19.92 tok/s compute, matches the logged 19.93.
  <sub>2026-10-16 · strata-chat-2026-10-16-02-29.md</sub>

**real finding noise**
- The real finding is the noise. Nine configurations appear in both runs, and the same config measured twice differs by 1.6–21.1% in tok/s (median 7.2%) and 1.6–16.5 points in bus share (median 2.2).
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>

**results 2026 13_142947**
- The run log `results/run-2026-10-13_142947-kv,topology,worker-0.log` is internally consistent with it (chunk PP lines 2159.3, 2549.3, 2390.8, 2292.7 tok/s; `prompt_ms 8932.3` for 20480 tokens).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-05-57.md</sub>

**telemetry internally consistent**
- Telemetry is internally consistent for the winner: 1000/9.54 = 104.8 ms/tok, 60.72 ms off-card = 57.94%, card-alone 22.69 tok/s, and 16.52 reads × 3.652 ms = 60.3 ms of that 60.7 ms.
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>

**timeout fixed timeouts**
- `--run-timeout 300` fixed the timeouts: 10 evaluated, 10 measured, 0 failed. The think columns are arithmetically exact — 65 tokens / 16883 ms = 3.85, 447 / 123727 ms = 3.61, 512 / 140634 ms = 3.64, 10240 / 66207 ms = 154.7, and the parts sum to the reported latency.
  <sub>2026-10-16 · strata-chat-2026-10-16-04-56.md</sub>

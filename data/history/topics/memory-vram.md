# Memory, VRAM and RAM

Accumulated from 55 transcripts, 163 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**Three tiers for experts.** VRAM first, then pinned RAM, then the GGUF file. `--resident-experts`
puts a chosen amount of experts in RAM; `--pcie-frac` is the share of the experts missing from
VRAM that are copied to the GPU rather than computed on the CPU — a fast PCIe link and a slower
CPU want more, a laptop's narrower link less. `--adapt-every` keeps the RAM copy following the
conversation: a swap copies the evicted expert back into the RAM place of the one replacing it.

**Pinned memory is a real mechanism, not a hint.** `PinnedArena` and `HYPERCORE_ARENA_PIN_GIB`
pin a bounded region; the engine manages residency per layer with `MADV_WILLNEED` /
`MADV_DONTNEED`. `STRATA_RESIDENT_PIN=0` (or `--pcie-frac 0`) turns the pinned path off.

**KV is the other half of the budget.** Above 64K context, setup keeps the KV cache in RAM and
only the part attention reads in VRAM (`--kv-resident 32768`), which is what lets a larger model
fit. KV precision is chosen by `--kv` (int8 default, q4_0 half the memory, k8v4 hybrid).

**`--vram-reserve-mib` is an engine argument, not a config key.** The web Settings tab writes it
into the `args` list; there is no top-level `vram_reserve_mib`. It is the only setting in the
editable spec that maps to a flag rather than a key.

**Read the numbers, not the assumption.** A request that reports 88,235 of 88,264 tokens reused is
using the conversation cache; one that reads 116,235 of 116,240 is not. The difference is prompt
time, and it is the first thing to check when a turn is slow.

**The memory guard is measured against the machine, not against the card.** A worker started from
the build on this box refused before it was ready: limit 30.5 GiB (source: meminfo), available
4.7 GiB, floor 2560 MiB. Nothing was wrong with the GPU - the other server running here holds the
machine's memory, so a second engine cannot be loaded until that one releases it. This is the
state the web app must stay usable in, and the reason a worker's exit note is the only useful
thing it can report.


## What is true now

how the thing works, as the chats established it


**--pcie-frac** · 4 chats
- The knobs that do control free VRAM and RAM One indirect effect worth knowing. With `--pcie-frac` unset on a single-GPU CUDA serve, `PcieAuto` (`generate.cpp:1582`) measures the pool's milliseconds per missed expert and rebalances copy-over-PCIe versus CPU compute three times over the first decode windows.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>
- RAM is MemTotal in GiB (31 here), PCIe is the display device's link (16.0 GT/s = gen 4, x16), not the fastest link on the board, which is what actually decides whether a `--pcie-frac` result transfers.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>
- With a page-locked copy the GPU also takes its usual share of the misses over PCIe (`--pcie-frac`), as with enough RAM; `--pcie-frac 0` (or `STRATA_RESIDENT_PIN=0`) gives the mapp [showing 8000 of 8001 characters read; the rest is not included.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md · also in strata-chat-2026-10-03-14-03.md</sub>
- the share of the experts missing from VRAM that are copied to the GPU instead of computed by the CPU (`--pcie-frac`: a fast PCIe link and a slower CPU want more, a laptop's narrower link less);
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**--vram-reserve-mib** · 4 chats
- `svc.config_path` and the engine `args` are read at startup, and `ensure_loaded()` reuses that args list — so a `/v1/unload` + `/v1/load` cycle will not pick up `--vram-reserve-mib`.
  <sub>unknown · unknown-context.md</sub>
- The table grouped by `--vram-reserve-mib` (min 12.29 at 512, median 12.83) does not exist in the data.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>
- Test asserts the emitted launcher's `--vram-reserve-mib` is within `--vram-max 256` after a run whose history contains 1280/1536 points.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-55.md</sub>
- The only bridge is `("arg", "--flag")` in the `EDITABLE` spec — currently just `--vram-reserve-mib`.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-03.md</sub>

**vram** · 4 chats
- So the coverage line will not fire for `vram` under the current priors, which is why the balance sweep you pasted printed no `coverage:` line.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>
- `--vram-min 512`: not because 256 "always fails", but because 49 of the 62 rows at `vram=256` hit `NO_VRAM_FOR_MTP: the draft head does not fit` (7 PASS).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-05-57.md</sub>
- For `vram` engine only >=0, but Param low is user-specified min; not necessarily mismatch.
  <sub>2026-10-15 · strata-chat-2026-10-15-16-58.md</sub>
- `VRAM` is not optional sugar — the server uses it live, so an API that can't express it can't host the server.
  <sub>2026-10-09 · strata-chat-2026-10-09-16-17.md</sub>

**streaming engine more** · 3 chats
- Emitted: `glm kv: KV streaming: %.2f%% of %llu block reads hit VRAM, %.1f MiB read from RAM [ - OVERFLOW]` (`generate.cpp:1951`), from `Glm5Model::kv_stream_stats()`.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>
- When the engine exited at boot (your trial 1: `vram=512, kv=fp16, pcie=0.33` at 131072 context is a plausible MTP/VRAM boot failure), Python flushed that buffer during GC and printed `Exception ignored while finalizing file <_io.TextIOWrapper name=5>`.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-14-31.md</sub>
- KV streaming (engine 0.1.5): at 64K and more, setup keeps the context's KV cache in RAM and only the part the attention reads in VRAM (`--kv-resident 32768`), so more experts fit on the GPU.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>

**--adapt-swaps** · 2 chats
- `--pcie-frac 0.00` + `--resident-experts` + `--adapt-swaps` — docs DETAILS.md line ~95-100 explicitly: "Through the server, two more things carry over from one request to the next (#410): the adaptive tier moves experts between RAM and VRAM (the GPU and the CPU round an expert differently), and the prompt cache resumes a repeated prompt and reads only its tail through the decode path.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- For the requests that produced token soup: `--prompt-cache` is at its default 6 (`src/program/generate.cpp:592`) and `--adapt-swaps` at 96 (`:588`).
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**--dry-run** · 2 chats
- The grid-coverage feature is verified against the stub engine and a `--dry-run` only; the hardware vram A/B is still unmeasured.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- `hardware.py` gains `free_vram_mib()`; `search_space.py` gains the pack's floor, read from the header the pack generator writes: `bayesian_tune.preflight()` runs before the first trial and is skipped for `--dry-run`, a `.py` stand-in engine, or `--no-preflight`: exit 1.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-55.md</sub>

**break** · 2 chats
- If capped (`STRATA_ARENA_PIN_GIB=N`, multi-GPU under WDDM, remote experts) or refused: `for (i = 0; i + 1 < bounds.size(); ++i)` — arena order, `break` at the first refusal.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>
- `src/core/pinned.cu:356–483` — registration loop walks the explicit heat order first, then the remaining slices in arena order; cap overflow `continue`s, driver refusal `break`s; tail lock is per-slice when an order is supplied.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-17-15.md</sub>

**hypercore_arena_pin_gib** · 2 chats
- This is a deliberate design decision, stated in the source: The engine manages residency per layer with `MADV_WILLNEED` / `MADV_DONTNEED`, and there is already a selective pinning mechanism (`PinnedArena`, `HYPERCORE_ARENA_PIN_GIB`).
  <sub>2026-10-10 · hypercore-chat-2026-10-10-16-40.md</sub>
- Pin the hot slices, not the address space. Use the existing `PinnedArena` / `HYPERCORE_ARENA_PIN_GIB` path, sized to the working set, rather than `mlockall`.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-16-40.md</sub>

**--adapt-every**
- The cache still follows the conversation (`--adapt-every`): a swap copies the evicted expert back from VRAM into the RAM place of the one that replaces it, so the RAM copy keeps holding exactly what the GPU does not.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>

**--conversation-cache-mib**
- If the goal is more free VRAM, raise `--vram-reserve-mib`; if it is more free RAM, lower `--resident-budget-gib` or `--conversation-cache-mib`.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**--kv-resident**
- KV streaming is not active here (`--kv-resident` is absent; `kv_resident` defaults to 0, all KV in VRAM), and at these sizes it wouldn't matter.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

**--min-save**
- For every file it copies a keyframe-exact segment (`-c copy`, so the original bits are preserved), then re-encodes that segment with libx265 — the same encoder HandBrake uses — starting at RF 26 and stepping up to RF 30 until the measured byte saving reaches `--min-save`.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-07-56.md</sub>

**--mmap-experts**
- Then a parity run on the arena path (no `--mmap-experts`) — the `FileExpertSource` path on this node is already fully page-locked, so it won't show a delta; the gain is only measurable in the default arena configuration.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-17-15.md</sub>

**--no-load**
- Then the A/B that the first sweep could not do, same grid with vram pinned: `--no-load` skips the 499 DRY priors.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-09-35.md</sub>

**--tail-role-token**
- Compared with Strata's running args, `_hypercore-q2_0.json` is missing `--vram-reserve-mib 512`, has `--pcie-frac 0.75` (the default) instead of the measured `0.00`, `--spec-min-p 0.80` instead of `0.90`, and no `--tail-role-token`.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**--vram**
- One thing your run left behind. It was interrupted (`interrupted: true`, 0 trials evaluated), and the deliverable it wrote at `/home/ai-user0/project-maya/maya-maya-s24.json` carries: `best` is taken from the highest-scoring history prior regardless of the current grid, so a config outside your `--vram` bounds and using a mode the GLM pack ignores ended up in `run-maya-maya-s24.sh`.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**.calibrate_progress.log**
- `.calibrate_progress.log` shows: The calibrator starts its own worker while the live server (pid 998337, started 14:04, `--port 8081 --turn-tail-check`) already holds the model, so the second engine cannot get VRAM.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-14-18.md</sub>

**/home/ai-user0/.config/hypercore/settings.json**
- `/home/ai-user0/.config/hypercore/settings.json` (1498 B, modified 14:17:52) — the actual calibration table, keyed by `GPU|VRAM|CPU|RAM|model|context|mode`: The 2026-10-11 entry is the one that matters for your setup.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>

**/home/ai-user0/hypercore-webui/.mcp_reproduction_state.json**
- The state file `/home/ai-user0/HyperCore-WebUI/.mcp_reproduction_state.json` is written and parses cleanly; the remaining queue is the live GPU confirmation and the decision on whether the Strata config should keep `--vram-reserve-mib 768` now that the K/V tier, not the reserve, is the dominant consumer.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**/home/ai-user0/maya-data/models/maya-s24/pack**
- What still drops is legitimate: 43 priors outside the `--vram-min 512 --vram-max 1024` you asked for, and 35 carrying `kv=k8v4`, which the GLM runner on `/home/ai-user0/Maya-data/models/Maya-S24/pack` does not implement (those trials measured an fp16 run — they could be re-keyed to the fp16 point if you want them used).
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**/tmp/oc_grid/.csv**
- Then confirm it: I validated the flag syntax against the stub engine: it reports `frozen: pcie=..., ple_io=direct, kv=..., tail=1` and the grid does include `vram=0` (`/tmp/oc_grid/.csv` row 2).
  <sub>2026-10-17 · hypercore-chat-2026-10-17-00-38.md</sub>

**512**
- `--vram-reserve-mib 768` vs `512` costs 195 slots up front.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**_hypercore_run-q2_0.sh**
- Next step needs your go-ahead: stop the Strata instance (pid 178995) so the 4090 is free, then re-run `_hypercore_run-q2_0.sh` to see the full READY handshake — or, if you'd rather keep Strata up, I can run the calibration path first so the config gets a VRAM reserve.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**args**
- The server reads the config at start, so the tab's own note says it plainly: "Saved to the run config; used from the next start of the model." Some keys have guards — `lazy_load` is refused if the config has a `vision` block — and `vram_reserve_mib` is written into the `args` list as `--vram-reserve-mib <n>`, not as a top-level key.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**bench/results/exchange-rotation-token-ids.json**
- What the four runs measured (same winner config, `--vram-reserve-mib 512`, 13605 resident slots): Why it is the prompt, and not the size. `search_space.py:343` points the bench at `bench/results/exchange-rotation-token-ids.json`, which carries exactly 1024 ids.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**calibrate_glm.py**
- Preflight. Before the first trial, check free VRAM against the pack's minimum and refuse to start with the installer's wording — `calibrate_glm.py` already does this: "a model server still running?
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**caveat signal claim**
- One caveat to add to the signal claim. The knob groups are confounded at n=10: the winner is the only `layer=joint` trial, and it is simultaneously the vram=512 and pcie_mode=kernel point.
  <sub>2026-10-16 · strata-chat-2026-10-16-02-29.md</sub>

**cnt**
- For each token, count `cnt` = how many of its 10 routed experts are not in the VRAM tier (`host_res[...] < 0`), not on a peer GPU, and not held by a helper/remote GPU.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-15-32.md</sub>

**collapse twice trials**
- This is the fix for the collapse you saw twice (all 25 trials at vram=1280 despite a `{768,1024,1280}` grid).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**correction state file**
- One correction to the state file, now closed: the queued item claiming Maya's `hardware.ambient_state()` lacks RAM/swap capture is wrong.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**cudaeventquery**
- Drain demotion events (`cudaEventQuery` on `F->draining`); a demoted expert becomes `kRHold` in RAM and its VRAM slot is freed only now.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**data/expert-profile.bin**
- For Q2_0 the decode-side balance is degenerate. Every measured run reports `ram_blobs=0 file_blobs=0 offloaded=0` for the decode, because all 13,061 expert slots are pre-filled from `data/expert-profile.bin`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**docs/details.md**
- Check the RAM headroom first — at 262K the context is already the memory limit for the larger quants (`docs/DETAILS.md`, speed tables).
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>

**everything else sweep**
- Everything else the sweep sends (`vram 256–1536`, `pcie 0.0–1.0`, `pool-workers 0…cores−1`, `prompt-cache 0–6`, `spec-min-p 0.001–1.0`, `ple-io direct|mmap`, `pcie-mode auto|dma|kernel|direct`) is inside the engine's accepted range.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**fast_boundary**
- Emitted: `glm stat` (`src/program/generate.cpp:1928`), per request, to stderr: Every field is derived from counters incremented inside `fast_moe` (`glm_fast_path.cu:3700-3776`) and `fast_boundary` (`:4140`): Derived rates: `bytes/s from SSD = disk(tok⁻¹) × blob_bytes × tok/s`; `PCIe bytes/s = (ram_fetch + promo) × blob_bytes × tok/s`.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**free_ram_mb**
- `/home/ai-user0/HyperCore-WebUI/optuna_tune/hardware.py:224` defines `memory_state()` (`free_ram_mb`, `total_ram_mb`, `swap_used_mb`, block reads) and `hardware.py:290` merges it into `ambient_state()`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**home_of**
- sycl #2014 `resident_stage_swaps`' `home_of` argument (`sycl/src/program/generate.cpp:255` has no such parameter) - needs an Arc build.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-08-16.md</sub>

**hot_layer_order**
- Need maybe maybe include "The C++ test covers `hot_layer_order` and, when a CUDA device is present, `PinnedArena` honors order." Good.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>

**hypercore_bridge.cpp**
- So the correct place to act is not `hypercore_bridge.cpp` with `mlockall`, but the arena registration path in `PinnedArena`.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>

**hypercorerequesthandler**
- Which HTTP entry is production? `serve/server.py` (5,479 lines, `Service`, `HyperCoreRequestHandler` adapter, MCP, VRAM admission) and `serve/hypercore_server.py` (316 lines, clean, 4/4 passing) both exist and both speak the same wire.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**k8v4**
- Against `results/maya-Maya-S24.record.json` (40 trials, 36 `q4_0` + 2 `k8v4`): All 40 now carry a valid mode; the 36 refusals are purely `vram=1280/1536` outside the bounds asked for — which is correct.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-55.md</sub>

**knee**
- The point it makes is the one the black_box sweep is missing: a raw tok/s ranking rewards a configuration that is fast because it is thrashing the bus, and `knee` says when adding more VRAM stops paying.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**mb/tok**
- `tools/glm_cache_sim.py` and `tools/glm_tier_replay.py` simulate the same accounting (`reads/tok`, `MB/tok`, `vram%`, `ram%`, `off-card ms/tok`).
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**no_vram**
- The same pattern applies to identical failure reasons: five consecutive `NO_VRAM` is not a search result, it's an environment problem.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**no_vram_for_mtp**
- Without engine changes, the only transfer proxy black_box has is `pcie_frac` and the `NO_VRAM_FOR_MTP` failure mode, which is what the existing sweeps already measure indirectly.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**objectives converge same**
- The two objectives converge on the same configuration. All 14 knob values are identical: `vram 512, pcie .55, kernel, mmap, inflight 128, fp16, kvstream off, no host worker, worker 10, tail 2, draft floor .445, suffix draft 2, mtp 4, parallel 1`.
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>

**off-card**
- `off-card` is the share of the decode's routed experts the GPU did not get from its own VRAM: DONE position 15 (`offloaded`, #588) against the cache lookups, exactly the number the engine prints beside its hit rate (`src/program/generate.cpp:10435-10439`).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md</sub>

**pin_order**
- Potential issue: `PinnedArena` constructor with `register_order` may be called with `pin_order` from profile but if profile is flat, empty.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-54.md</sub>

**pinnedarena**
- The `PinnedArena` side of #1 is already in place — I just re-confirmed it:
  <sub>2026-10-10 · hypercore-chat-2026-10-10-17-15.md</sub>

**populated counters decode**
- The populated counters are the decode cache misses: `lookups - hits` are the routed experts the VRAM cache did not hold (admitted from the pack over PCIe, or refused and computed by the CPU pool).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md</sub>

**prompt_read**
- The worker's terminal frame now carries the expert-tier accounting it already computed (`prompt_read`, cache hits/lookups, PCIe offloads, RAM blobs, file blobs) and the caps reply carries the VRAM tier (slots, slots full, cache MiB, cache full MiB, free VRAM, how the host experts are held, prompt chunk).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**rank_learned_profile**
- Heat is used only on the GPU side: `read_expert_profile` / `rank_learned_profile` fill the VRAM cache tiers.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

**reused**
- The request table's `Reused`, `Tok/s` and `VRAM hit rate` columns are populated.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**serve/server.py**
- `serve/server.py` — wire the narrator into the one lifecycle entry the server uses: The trigger strings are still valid for this engine: `src/program/generate.cpp:3777` prints `expert arena:`, `:3778` prints `loaded %.2f GiB at %.2f GiB/s`, `:4073` prints `expert cache N slots,`.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-20-47.md</sub>

**test_boundary_payloads_agree_between_engines**
- `test_boundary_payloads_agree_between_engines` runs the transcribed formula over empty profile, zero free VRAM, reserve > free, overflow, and one-byte-short-of-a-slot.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**transfer.py**
- The counters are already there, in the DONE line. `src/program/generate.cpp:10354` documents the format, and a real log line is: Positions 9 (hits), 10 (lookups), 11 (RAM blobs), 12 (file blobs), 13 (file MB), 15 (offloaded) are exactly the fields `transfer.py` needs.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**transfer_share**
- Where only counts are available, times are attributed with those measured unit costs; `transfer_share` is clamped to 1 and is the larger of that and `1 - vram_hit`, which the engine measures directly.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**--batch**
- It is not applied, and no line is printed, when every expert is already in VRAM, with `--batch` slots, or with a peer/helper GPU.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**changed pack tokenizer**
- Changed so the pack's tokenizer is the prompt source, not the fallback: Verified against the real model: `./max_tune.sh --model Maya-S24 --gen --dry-run --trials 2 --vram-min 512 --vram-max 1024` now prints no vocabulary note.
  <sub>2026-10-15 · strata-chat-2026-10-15-18-12.md</sub>

**src/core/pinned.cu**
- I widened the magic field and the struct member to 20 bytes in both `src/core/pinned.cu` and `sycl/src/core/pinned.dp.cpp`, keeping the header 4-byte aligned inside the fixed 4096-byte arena header.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**test_grid_coverage_probes_a_dimension_the_priors_dominate**
- Tests: added `test_grid_coverage_probes_a_dimension_the_priors_dominate` (5/7 priors at 1280 → coverage queues 1024 and 768, CSV samples more than one vram).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**vram collapse root**
- The vram collapse has a root cause, and it is fixed. Both 25-trial sweeps put every trial at `vram=1280` despite `--vram-min 768 --vram-max 1280 --vram-step 256`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**vram model runtime** · 8 chats
- 244 MiB less VRAM free at the sizing moment (the Strata run had the MTP draft layer at 834 MiB versus 818 MiB, plus other allocations) costs 187 slots.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>
- The chosen config (vram 512, pcie 0.3347, kv int8, host_core last, worker 22) is not one of the 10 trials; it is a prior recorded on 2026-10-15T03:13 at 202.38 tok/s.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-23-49.md</sub>
- Bug: the vram ladder was reporting a knee from noise. `knee()` was fed raw `(vram, prefill)` rows, so a sweep that sampled only 1280 produced 25 "rungs" and printed "extra reserve stops paying at 1280 MiB".
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- The 13:27 failure is a VRAM contention failure ("cannot pin 260 MiB, nor place it in VRAM"), consistent with the card being held by another instance at that time.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>
- 3 GB VRAM is tight. `--batch_size 16` OOMs ("tried to allocate 1.15 GiB").
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>
- Its head takes ~180 MiB of VRAM, which the expert cache leaves free for it (0.1.28).
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>
- Your GPU has 24 GB of VRAM, and the model/runtime is using: > 23.8 GB That means the model is using almost all available GPU memory.
  <sub>2026-10-02 · strata-chat-2026-10-02-22-43.md</sub>
- Combined with other model/cache requirements, it explains why VRAM is near 24 GB.
  <sub>2026-10-02 · strata-chat-2026-10-02-22-43.md</sub>

**mode engine chosen** · 6 chats
- The handshake now succeeds; the engine then exits on its own memory guard: There is already a Strata server resident on this box — pid 113114, `/home/ai-user0/Strata/serve/server.py --engine strata --config /home/ai-user0/Strata/strata-q2_0.json --port 8080`, holding 24.8 GB of RAM, with the 4090 at 23.9/24.5 GiB.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>
- The engine itself reports `471 MiB of VRAM free with everything loaded`, so the card is fine; the lookup tier above it is not.
  <sub>2026-10-16 · strata-chat-2026-10-16-04-56.md</sub>
- An engine run needs 1.48 GiB for the weight arena, so I could not run the three arms against the fixed binary — the garbling the A/B matrix measured was the pre-guard binary.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-53.md</sub>
- The engine pins 19.34 GiB of experts page-locked in host RAM on a 30 GB box, and takes a 15.57 GiB expert cache on a 24 GB card.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- But the resident mode's own headroom is 4 GiB, and there is essentially none left for the web server, the six MCP processes, and the patched Firefox.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>
- Low-RAM mode (engine 0.1.26, chosen by setup): normally all of a model's experts are copied into RAM (23-50 GB, pinned) and the GPU holds a copy of the most-used ones.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**measured estimated runs** · 5 chats
- The measured floor is what it is: 13.14% is the lowest off-card ever recorded here, and it came from `vram=512, pcie=0.3347, kv=int8, hostcore=last, worker=22`.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-00-38.md</sub>
- Verdict on the two runs you pasted: the `--prefill-cap auto:16384` probe is a clean, controlled A/B, and it lost. Same configuration in both, `vram=1280 pcie=0.0917 worker=16 tail=1 draft_floor=0.296`, three boots each: `-19.4%`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>
- The deployed config is now measured, not inferred. Your pinned run (`vram=1024 pcie=0.0368 worker=17 draft-floor=0.8`, repeat 3) gives 944.63 tok/s prefill, 166.49 gen, 23378 ms, bus 76%, 23964 MB read, 1322 MB/s.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- Peak measured 2784 / 2998 MiB — it runs, but there is no headroom.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>
- Not measured - estimated from the runs above (same CPU and 64 GB RAM): the GPU part scaled by memory bandwidth, the CPU part by how many more experts the card's VRAM holds.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**cudamalloc** · 4 chats
- I tried twice and both failed at startup: the Strata engine on port 8081 holds 24.1 of 24.5 GiB, and it reloads itself within seconds of being unloaded — even a 13 MB `cudaMalloc` returns out of memory.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-53.md</sub>
- No model was loaded, no GPU was touched — the 4090 is holding 24,074 of 24,564 MiB and a 4 KB `cudaMalloc` from a second process returns `out of memory`, so throughput is still unmeasured here.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>
- It produced no numbers: the engine is resident on the 4090 (24,074 of 24,564 MiB, 93% utilisation), and a 4 KB `cudaMalloc` from a second process returned `out of memory`.
  <sub>2026-10-09 · strata-chat-2026-10-09-16-17.md</sub>
- A refactor of an 11k-line driver is exactly the kind of change that would need that same paired-measurement discipline before it's adopted — and I can't run it here: the 4090 is holding 24,074 of 24,564 MiB and a 4 KB `cudaMalloc` from a second process returns `out of memory`.
  <sub>2026-10-09 · strata-chat-2026-10-09-16-17.md</sub>

**--vram-reserve-mib** · 3 chats
- `--vram-reserve-mib` does move the floor: 512 MiB → 12.3% min, 1280 MiB → 14.6% median.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>
- What the data does show Grouped by `--vram-reserve-mib` across the sweep CSVs: So the answer to the question as asked: no prompt in `max_tune.sh`'s ladder produces an off-card value under 10%.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>
- Set `--vram-reserve-mib`. It's unset, so the engine default of 700 MiB applies, and only 377 MiB is actually free.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**experts real strata** · 3 chats
- The mechanism is system RAM, not the GPU. The live `strata` process has `VmRSS 26.3 GB` and `VmSwap 1.75 GB` on a 31 GB host with `MemAvailable 8.6 GB`; the engine pins a 20 GB RAM tier.
  <sub>2026-10-16 · strata-chat-2026-10-16-04-56.md</sub>
- So the real mechanism: the config sets `--vram-reserve-mib 512` but the engine warns it needs 831/1024 MiB.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- 12,630 experts / 16.26 GiB is real: Strata log lines 20-21, `expert cache 12630 slots, 16.26 GiB of VRAM`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>

**start here setup** · 3 chats
- Arm 2 — the sanitizer is not trustworthy here. `--expert-cache 4096` did free enough VRAM (9353 MiB free, so the cuBLAS OOM is gone), and memcheck reached the prompt path.
  <sub>2026-10-14 · strata-chat-2026-10-14-15-52.md</sub>
- Any black_box engine start dies before it can answer: I reproduced that with the control arm `HYPERCORE_MMVQ_IL=0 ./debug_mmvq_il.sh` — it fails the same way ("native embedding: cannot pin 260 MiB, nor place it in VRAM").
  <sub>2026-10-14 · strata-chat-2026-10-14-13-13.md</sub>
- `START-HERE.bat --setup --draft-vocab en` keeps the English/code subset from before (40,525 ids, ~110 MiB less VRAM, English answers 1-2% faster; CJK answers get almost no drafts).
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**--resident-experts** · 2 chats
- And on this node it is moot twice over: the running config uses `--resident-experts` → `FileExpertSource`, whose 19.15 GiB is already fully page-locked and registered (96–98% hit, 1–5% PCIe); and I checked the profile itself — `data/expert-profile.bin` (48×512, all 24,576 pairs ranked) has a layer-heat spread of only 32.5%, and the best heat-ordered prefix beats the arena prefix by 1.99% at k=18.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>
- The Q2_0 arena is 34.0 GB on a 32 GB PC, so setup chose `--resident-experts` — and it's working: the log reports "0 blob reads from the file" and an 87–92% expert hit rate.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**/home/ai-user0/strata/engine/strata** · 2 chats
- I started the probe: it got past argument parsing and reached engine init, then stopped at `native embedding: cannot pin 260 MiB, nor place it in VRAM` — the card is held by `/home/ai-user0/Strata/engine/strata` at 24050 of 24564 MiB.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>
- I could not run it myself: your `/home/ai-user0/Strata/engine/strata` is holding 24076 MiB of the 4090 with 30 MiB free — even the parity test OOMs.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**adaptive swap tier** · 2 chats
- The KV tier takes slots from the expert tier as context grows: At 131072 the KV hands back roughly 0.56 GiB compared with 262144, which is about 430 more resident experts.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-00-38.md</sub>
- The adaptive swap tier moved ~320 MiB of experts into RAM to compensate, so nothing is being read from the SSD.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**hybrid k8v4 cache** · 2 chats
- With `--expert-cache auto` filling 15.94 GiB and resident RAM mode, the VRAM is nearly full → the driver pages GPU memory to system RAM → decode degrades → the model emits garbage/CJK spam/empty.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- Hybrid K8V4 KV cache (engine 0.1.25, optional, PR #120): `--kv k8v4` (`START-HERE.bat --setup --kv k8v4`) keeps the keys at 8 bits and stores the values as rotated 4-bit: 23% less KV memory than 8-bit, so more experts fit in VRAM.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

**--kv-grow**
- With `--kv-grow`, the K/V tier is elastic and takes its room out of the expert cache: 3.35 GiB of K/V versus 1.68 GiB.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**--max-new**
- The lowest reachable point is the 1024 rung at exactly 10.0%, from the winner config on disk (`max_run_q2_0.json`, `--vram-reserve-mib 512`, `--prefill auto:32768`): `max_tune.sh:113` is `exec "$PY" "$HERE/winner_bench.py" "$@"`, and `winner_bench.py:566` (`--max-new`, default 512) and `:570` (`--prompt-ladder`, default the 7-size ladder) confirm those flags are forwarded.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**--selftest**
- The full `--selftest` could not be run here: serve (pid 1321629) holds 24,068 MiB and the test aborts at `history regression allocation: out of memory`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-22-55.md</sub>

**--vram-elastic**
- `--vram-elastic` exists for the GPU side; there are only 377 MiB free on the 4090.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**./build/mmvq_il_parity**
- I could not run the isolation myself: `./build/mmvq_il_parity` fails `CUDA: malloc w: out of memory`, and `debug_mmvq_il.sh` exits at `native embedding: cannot pin 260 MiB, nor place it in VRAM` — one card, 24106/24564 MiB used.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**also tuning failed**
- This is also why `Tuning: FAILED` said "the draft head does not fit: needs 178 MiB of VRAM and 170 MiB is free" — the MTP head competes for the same held-back pool.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**closing needs roughly**
- Closing that needs roughly 450-500 more resident slots, about 620 MB, and the card has no headroom at 512 MiB reserve.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**cudahostalloc**
- `src/core/native_head.cpp:150` fails when both `cudaHostAlloc` (mapped pinned — which also needs device mapping) and the `cudaMalloc` fallback cannot get 260 MiB, and that error is fatal to init (`worker_main.cpp:277` → `-5`).
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**cudahostregister**
- The test asserts the real profile is rejected: The registration half of the test exits 77 (skipped) because the live server holds 23.9 GiB of the 24.5 GiB GPU — `cudaHostRegister` of even 1 MiB is refused.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

**dimension marginals best**
- Dimension marginals (best measured prefill per value) are unambiguous: Winner (max measured prefill): `vram=1024 pcie=0.0368 ple-io=direct kv=k8v4 kv-grow=1 host-core=first affinity=auto pool-workers=17 prompt-cache-tail spec-min-p=0.8` at `--max-context 262144` → 2292.80 tok/s prefill (runner-up 2023.56 tok/s).
  <sub>2026-10-17 · strata-chat-2026-10-17-14-18.md</sub>

**done deliverable verification**
- Done for the deliverable; verification is blocked — the winner re-run (`max_tune.sh --prefill --repeat 2`, all dimensions pinned) refused to boot: "the GPU has 350 MiB free.
  <sub>2026-10-17 · strata-chat-2026-10-17-14-18.md</sub>

**effects move decode**
- The two effects that do move it are decode length (a 1-token decode at the 512 rung is 61% because the tier is cold; a 512-token decode at the 2048 rung is 10.2%) and the reserve, which trades slots directly: 1 GiB more reserve is ~775 fewer resident slots out of 13605.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>

**empty reply 2065**
- The empty reply is the #2065 stall: 45 MiB free against a 512 MiB reserve, and `MMVQ_IL_CHECK=1` doubles the dense work (`verify.cpp:715`, "halves the dense throughput, so it is a diagnostic, not a mode").
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md · also in hypercore-chat-2026-10-14-12-55.md</sub>

**experts.bin**
- `experts.bin` is 33,973,862,400 bytes (31.6 GiB) and the card has 24,564 MiB total, 18.2 GiB free after the weights.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-00-38.md</sub>

**group means confirmed**
- Group means confirmed: kernel 16.65/52.9%, kvstream off 16.70, vram 512 8.53 vs 1024 8.21.
  <sub>2026-10-16 · strata-chat-2026-10-16-02-29.md</sub>

**hypercore webui spawns**
- HyperCore-WebUI spawns its own engine process, so there is no room for a second one on this 30 GiB box.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**hypercore-q2_0.json**
- `hypercore-q2_0.json` was written with `--vram-reserve-mib 700`, from: The BF16 mmproj alone is 907,524,736 B = 865 MiB of tensors, plus the encoder's own CUDA context and its work buffers.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**hypercore-q2_0.log**
- So the engine is doing the work — `hypercore-q2_0.log` has `FileExpertSource: allocating 19.15 GiB page-locked cache complement … 12241 in the GPU cache` — the window just says nothing for those minutes.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**include/strata/plan/plan.hpp**
- Memory plan — `include/strata/plan/plan.hpp`, CLI `src/plan/plan_main.cpp`: "the engine has 33.97 GB of expert weights … a fixed VRAM budget that has to hold the dense weights, the KV cache, the indexer keys, the recurrent state and as many expert-cache slots as remain." Computed from measured free VRAM, not configured.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**max_tune.sh**
- Run these yourself, since `max_tune.sh` needs the engine and >=12000 MiB free VRAM: Paste the output and I will close the last two queue entries.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**mlock**
- `src/platform/memory_test.cpp` failed because `RLIMIT_MEMLOCK` here is 8 MiB and the test demanded a 256 MiB `mlock`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-00-06.md</sub>

**ncu**
- Nsight Compute cannot profile while the engine is running. I connected `ncu` to a test binary and it attached fine, but the kernel launch failed with `out of memory`: `nvidia-smi` shows pid 3609440 holding 24,072 of 24,564 MiB.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-04-36.md</sub>

**nvidia-smi**
- `nvidia-smi` shows no processes because that engine keeps 20.7 GiB of experts resident in system RAM; the box has 30 GiB total, so the second engine died on `cannot pin 260 MiB: out of memory`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**page locked expert**
- RAM, not GPU. 20.7 GiB is page-locked for the expert arena and the process RSS is 24.5 GB of 32 GB.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**prompts must repeating**
- Prompts must be new. Repeating three prompts raised VRAM hits to 99.2% and left 2.6 experts on the CPU (Maya-L, 9 GPUs) — the CPU lane was measured with essentially nothing on it.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**py_compile**
- `py_compile` clean on all seven edited files, `sh -n` clean on both scripts To see it live, the process needs to reload the source: I didn't do that myself — it stops the running chat and reloads the model (minutes of weight loading, ~24 GB of VRAM).
  <sub>2026-10-10 · strata-chat-2026-10-10-19-47.md</sub>

**qwen/qwen2.5-1.5b-instruct**
- Constraint found: the GPU is fully held by the Strata engine (PID 71501, 23.5 / 24.5 GiB), so `Qwen/Qwen2.5-1.5B-Instruct` OOM'd on CUDA → the benchmark ran on CPU.
  <sub>2026-10-07 · strata-chat-2026-10-07-17-37.md</sub>

**same headroom failure**
- That is the same headroom failure that killed the calibration worker ("cannot pin 260 MiB, nor place it in VRAM").
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**serve**
- It compiled (`cmake --build build --target ple_parity`, exit 0), but running it directly hits `history regression allocation: out of memory` at `src/kernels/ple_parity.cpp:195` while `serve` holds 24084 of 24564 MiB.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-07-50.md</sub>

**speed_lines**
- `maya.py:2424` (`speed_lines`) adds the guard for issue #8: a request that only read a prompt has no decode, so a `glm stat` line with `decode < 1 ms/tok` or `vram hit 0.00%` is rewritten to `glm stat (prompt only): prompt …` rather than reporting a 96,000 tok/s decode speed.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**still garbles there**
- If it still garbles, there's a second path I haven't found and I'll bisect the kernel itself with `build/mmvq_il_parity --bench --emit-table` — which needs the GPU free; right now it fails with `malloc w: out of memory` because the Strata server holds 24,114 of 24,564 MiB.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**strata_models**
- Consider the model setup.py recommends. `strata_models` says Coder IQ1_M at 128K is the fastest that fits here (23.4 GB arena → fully resident), while Q2_0 "fits in the low-RAM mode (slower: the GPU holds part of the experts)".
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**timeout correct behaviour**
- The TIMEOUT row is correct behaviour, not a bug: that trial (vram 1024, pcie 1.0, worker 0) reached READY and decoded at 1.19 tok/s, so 512 tokens needed ~430 s and the deadline cut it off.
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>

**what ramp made**
- What the ramp is made of, from the log: the VRAM expert tier fills across the session (38,435 → 51,309 exchanges with the tier), and draft acceptance rises from 86–94% on short requests to 97% on long ones.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

## Gotchas

things that bit once and will bite again


**docs details already** · 2 chats
- Rewrote `docs/TROUBLESHOOTING.md:85` to name the warning, the `--vram-reserve-mib 1024` fix, and the fact that the check is a diagnostic.
  <sub>2026-10-14 · hypercore-chat-2026-10-14-12-55.md</sub>
- docs/DETAILS.md already says "~1.4 GB of VRAM is kept free for it" — setup just never reserved that much.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**vram 1024 point** · 2 chats
- And "193 MiB of VRAM free with everything loaded - LOW: requests may stall" and "WARNING: only 51 MiB of VRAM free at the start of this request (below 512 MiB): when the card is this full the driver can page GPU memory to system RAM, decode falls to a few tok/s and a window may never finish (#2065)".
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- a `vram=1024` point when you run `--vram-max 512`) is skipped and counted, never clamped.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-17-13.md</sub>

**--conversation-cache-mib**
- Don't add `--conversation-cache-mib` yet. It defaults to 0, and `--conversation-cache-min-free-mib` (default 2560) would refuse to park anything with 4.4 GiB available.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**--gen**
- Also worth porting directly, because they fix the specific defects found in the black_box data: `other_objective_history()` (the deployed config came from a `--gen` sweep and was never re-scored under `--prefill`), `FAIL_STREAK` (49 `NO_VRAM_FOR_MTP` timeouts are being re-booted), and the digest in `run_log_name`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**balance sweep only**
- The balance sweep only sampled 1280 MiB, so the reserve dimension is untested.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md · also in hypercore-chat-2026-10-16-12-00.md</sub>

**disk**
- `vram_hit` is a share of routes, `ram_fetch`/`disk`/`promo` are counts per token — they are different denominators and must not be added.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**expert cache rate**
- Expert cache hit rate 87–92% on a 512-expert MoE: 11,022 slots (14.5 GiB) in VRAM plus 20.72 GiB of experts page-locked in RAM, and the log says "0 blob reads from the file" — the SSD is never touched during decode.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**rank_learned_profile**
- The one missing link is the call site. `src/core/expert_source.cpp:3287-3288` still constructs `PinnedArena(want + blob, bounds, max_pinned_bytes, ...)` with no order argument, so the heat ranking from `read_expert_profile` / `rank_learned_profile` in `src/core/expert_cache.cpp` never reaches the arena.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-17-15.md</sub>

**strata_resident_headroom_gib**
- The engine leaves 4 GB of the RAM it finds free (`STRATA_RESIDENT_HEADROOM_GIB`); when even the experts the GPU does not hold do not fit, it says so and runs the plain mapped mode.
  <sub>2026-10-03 · strata-chat-2026-10-03-14-03.md</sub>

## Still open

not done, deferred, or waiting on a decision


**max_tune.sh**
- Not done, and it cannot be done here: the three hardware items in the queue (vram ladder with coverage active, `--prefill-cap auto` vs `auto:32768`, live read-rate cross-check) need the GPU, which `max_tune.sh` requires at >=12000 MiB free.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

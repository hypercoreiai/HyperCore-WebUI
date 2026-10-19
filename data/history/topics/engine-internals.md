# Engine internals

Accumulated from 56 transcripts, 178 entries. Built 2026-10-19. Newer chats append here; within a section the entries are grouped by the thing they are about, newest statement first.


## Current state

Maintained by hand; the builder keeps it in place.

**The engine is HyperCore, not llama.cpp.** `engine/hypercore` is built from this repo's `src/`
with CMake; llama.cpp is only a reference for quant formats and rope types. The server talks to a
`hypercore-worker` process over a Unix socket using a small frame protocol (`CMD_SUBMIT`,
`CMD_CANCEL`, token frames, one terminal frame per request), and the worker in turn drives the
engine process. `serve/hypercore_supervisor.py` owns that lifecycle; `serve/ipc_parser.py` decodes
the wire; `serve/server.py`'s `HyperCoreRequestHandler` is the adapter the `Service` uses.

**The ABI contract is a build step, not a hope.** `include/hypercore/hypercore_api.h` defines the
request and summary structs; `serve/ipc_parser.py` mirrors their sizes and field offsets, and
`tools/test_abi_contract.py` checks the two agree. The worker's summary carries `prompt_reused`,
`produced_tokens`, `prefill_ms`, `decode_ms`, `ttft_ms`, drafts accepted/offered and the stop
reason. When the worker does not report a counter, the Python side derives it rather than leaving
the Monitor blank (`derive_monitor_field`).

**Expert residency is the main memory mechanism.** Experts live in three tiers — VRAM, pinned RAM,
and the GGUF file — with `--resident-experts`, `--pcie-frac` and `--adapt-every` controlling what
stays on the GPU and how the cache follows the conversation. Hit rates are reported per request
("expert cache 90.0% hit"). KV streaming at 64K+ keeps most of the context's KV in RAM and only
the part attention reads in VRAM.

**Speculative decoding is on by default.** `--spec 4` plus the MTP draft layer (`--mtp <path>`)
accepts a large share of drafts; the terminal frame reports `drafts accepted/offered`, which is
what the Monitor's "Reused" and draft columns show.

**Prefill and decode are measured separately.** `prefill_ms` is the engine's own time to read the
prompt; `decode_ms` is the emit window. The Monitor's prefill card is computed from those two and
from `prompt_read` - the tokens actually read - not from the whole prompt, so a cached 88k-token
prefix does not make the rate look wrong.

**The 0.1.42 decode path.** `HYPERCORE_MMVQ_IL` (the interleaved small-batch matvec) and `HYPERCORE_NAN_GUARD`
(the per-window head-logits check) are on unless set to `0`. `HYPERCORE_ADAPT_OVERLAP` stays off unless set to `1`.
A reused interleaved copy is only valid for the `(n_in, ncols)` it was built for; `Q81IlCopy` rebuilds it on a
shape change and on every write to the activations. `HYPERCORE_MMVQ_IL_CHECK` is a diagnostic: it doubles dense
work and is not a serving mode.

**The empty-response cause was VRAM, not the flags.** The A/B ran on 2026-10-14 on an RTX 4090 with 45 MiB free:
`MMVQ_IL=1 MMVQ_IL_CHECK=1` returned no text, `MMVQ_IL=1` alone answered slowly, `MMVQ_IL=0` answered normally.
`grep -c "MMVQ_IL CHECK FAILED" hypercore-q2_0.log` is 0, so the interleaved kernel matched the plain one bit for bit
and the stale-copy guard is not what was producing the empty replies. `MMVQ_IL_CHECK` doubles the dense work by design
(`verify.cpp:715`), and on a card already under its reserve that is the #2065 stall. The fix is `--vram-reserve-mib
1024`, not the switches.


## What is true now

how the thing works, as the chats established it


**prefill_ms** · 5 chats
- For "tracks token latency drops and stalls" to be a real alert, either the summary needs a `prefill_ms` field, or the supervisor computes total from ack→terminal wall clock.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- Your decomposition says `prefill_ms` isolates compute, `ttft_ms` is hold + prefill, and a congested node spikes TTFT while prefill stays normal.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- In the code as it stands, that second half is false: `prefill_ms` is the hold.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- In the stub, `ttft_ms == prefill_ms` exactly — 45 and 45 in the wire_test output — and `ttft - prefill` is always 0.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- That costs a field, and byte 36 is now TTFT, so it means either a 44-byte summary (which the prefix rule already tolerates) or redefining `prefill_ms` to start at the scheduling point.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**hypercore-worker** · 3 chats
- One finding that affects the design: `hypercore-worker` does not exist in upstream Strata.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>
- CMake gating — `hypercore_abi_alignment_check` is now a dependency of `hypercore-worker` as well as `hypercore_api`.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>
- Is `hypercore-worker` part of the shipped artifact? Right now the documented entry point is `libhypercore_api.so`, but the out-of-process worker is the one that survives an engine crash.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**strata** · 3 chats
- 6 upstream llama.cpp vocab GGUF files — `strata` there is a token in the model vocabulary (`…Wilson ▁strata brainstorm…`), vendored third-party model data.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>
- The other `strata` content hits are upstream llama.cpp GGUF vocab files where "strata" is a real vocabulary token, and my own state notes.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>
- Upstream `third_party/llama.cpp/models/ggml-vocab-.gguf` — `strata` there is a vocabulary token inside llama.cpp's own model files, not our naming.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**--batch-mtp** · 2 chats
- The two paths `include/strata/program/batch_groups.hpp` is the whole decision, and it is pure (no GPU, no env): Why they conflict. With a layer split, `--batch-mtp`'s slot drafters live on the last stage's GPU, and the windows are two rows per slot pushed through every stage.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>
- The 0.1.43 behaviour your changelog describes, where `--batch-mtp` actually runs on a layer split, needs the slot-drafter support from Strata commit `48f2c47d` (`include/strata/core/batch_rows.hpp` plus the `verify.cpp` `stage_batch` layout rules) ported first.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**because queue_hold_ms ttft_ms** · 2 chats
- `--vram-min 512`: not because 256 "always fails", but because 49 of the 62 rows at `vram=256` hit `NO_VRAM_FOR_MTP: the draft head does not fit` (7 PASS).
  <sub>2026-10-16 · hypercore-chat-2026-10-16-05-57.md</sub>
- Because queue_hold_ms = ttft_ms - prefill_ms is a simple subtraction, computing it inside ipc_parser.py is straightforward.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**decode_ms** · 2 chats
- After the split, the slot hold is `prefill_ms` and `decode_ms` is only the emit window.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- If suffix-draft verification is charged to `decode_ms`, then `ttft - prefill` is the gate plus the first draft attempt — not purely the gate.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**hypercore_** · 2 chats
- Assumption I made: I kept `STRATA_BATCH_MTP_SERIAL` as a fallback so an existing setup does not silently change behaviour, with `HYPERCORE_` taking precedence.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>
- If you want me to proceed without deciding 1–3, the safe subset I can do immediately is: add `HYPERCORE_` env fallbacks alongside `STRATA_`, restore the `enabled = True` lines, fix the MCP test harness env scrubbing, and add `tests/CMakeLists.txt` — that clears the 15 rename-caused failures without touching any naming convention.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**pcie_frac** · 2 chats
- Does the request config carry `pcie_frac` / `spec_min_p`? Without them the pcie sweep becomes five engine re-inits instead of one — five model loads.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>
- The "tune" surface is deliberately tiny. Three knobs (`pcie_frac`, `spec_min_p`, `pool_workers`) are calibrated because they are the three that actually move tok/s; the 357 env vars are mostly A/B switches used by the parity and bench harness, not user-facing tuning.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-03.md</sub>

**pool_workers** · 2 chats
- What the expert cache actually sizes from (`src/program/generate.cpp:4031-4049`): `pool_workers` does not appear anywhere in that expression.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>
- Also: `strata_caps_t` no longer reports `pool_workers`, so `STRATA_POOL_WORKERS` is parsed by the bridge and then unobservable — a setting you can set but not read.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**test hard codes** · 2 chats
- The test intent is excellent: ABI alignment as a build dependency (`add_dependencies(hypercore-worker hypercore_abi_alignment_check)`) is a strong idea — a build that succeeds has already proven the header, the Python mirrors and the frame writer agree.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>
- A test that hard-codes a default fails the moment the environment layer is exercised. The first run asserted `workers == -1` for the defaults case; run with `STRATA_POOL_WORKERS=7`, the env layer correctly overrode it and the test failed.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**worker builds frame** · 2 chats
- The `socket /tmp/hypercore-worker-8081.sock could not be cleared` line is benign — the supervisor falls back to a process-private path.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>
- The worker builds the frame in a buffer and issues one `write()`; the test runs four concurrent streams and checks every stream gets all its tokens and exactly one terminal frame.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**--context**
- If you genuinely need a bigger window, re-run setup with a larger `--context`; 262,144 is the model's trained ceiling, so anything above it needs rope scaling, which setup derives for you (`START-HERE.bat --setup --context 393216` picks yarn and the factor).
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>

**--cross-objective**
- The shared surface is exactly the four functions that change how a result is scored and compared: The applicability mechanism is `--cross-objective` (`bayesian_tune.py:1000`): it pulls configurations measured under `gen`/`latency`/`prefill` and re-scores them for the current objective without re-booting.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**--gen**
- What is in place Usage `--gen` maximises generation tok/s, `--prefill` maximises prompt tok/s, `--latency` minimises `prompt_ms + decode_ms`; all three are mapped to one maximisation score so a failed boot lands at the bottom of the range.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-14-31.md</sub>

**--pool-workers**
- No. `--pool-workers` (config key `pool_workers`, also accepted as `workers` or `--threads`) is a CPU thread count for the expert pool.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**.bak-**
- `engine/` also holds `hypercore-vision`, `hypercore-worker`, and a set of `.bak-` files from earlier swaps.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-44.md</sub>

**/config**
- They are the same codebase — identical run-config format (`exe`, `args`, `cwd`, `mmproj`, `model`), identical `--engine strata --config <json>` invocation, identical `/config` settings view, identical engine IPC.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/health**
- Live: `python3 -m serve.hypercore_server --port 8125 --worker build-shared/hypercore-worker --socket /tmp/hc3.sock --config '{"batch":2}'` → `/health` 200, a 20-token completion streams and logs `[MONITOR] … decode_ms=39 tokens=20 drafts=20/100`, `/slots` returns the populated monitor, a dropped client issues `CMD_CANCEL` and `in_flight` returns to 0.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**/home/ai-user0/hypercore-webui**
- In `/home/ai-user0/HyperCore-WebUI` (HyperCore), 12 files: In `/home/ai-user0/Strata`: the flag does not exist under that name. `/home/ai-user0/Strata` is the 0.1.40 tree (`CMakeLists.txt:11 project(strata VERSION 0.1.40)`) and its `src/` has no tail skip.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-15-32.md · also in hypercore-chat-2026-10-13-08-16.md</sub>

**/home/ai-user0/hypercore-webui/build**
- Verified: it now resolves to `/home/ai-user0/HyperCore-WebUI/build/hypercore-worker` (RUNPATH `/home/ai-user0/HyperCore-WebUI/build`, i.e.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**/home/ai-user0/hypercore-webui/hypercore-q2_0.log**
- b) The 12:58 run you saw is in the engine's own log (`/home/ai-user0/HyperCore-WebUI/hypercore-q2_0.log`, which the supervisor captures from the worker's stderr — that's why nothing appeared on the terminal): The engine loaded completely, then the worker hit `src/prefill/kernels.cu:72` (`check()` → `std::exit(1)`) during prefill and exited.
  <sub>2026-10-14 · strata-chat-2026-10-14-13-13.md</sub>

**/home/ai-user0/hypercore-webui/tune**
- `/home/ai-user0/HyperCore-WebUI/tune` holds `lib.sh` (the shared FIFO + DONE-parsing plumbing), three single-dimension sweeps (`tune_affinity.sh`, `tune_host_core.sh`, `tune_pool_workers.sh`), two production launchers, and `results/` with four CSVs and 20 engine logs.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-11-07.md</sub>

**/home/ai-user0/strata**
- Nothing else in the live system was touched: the server in `/home/ai-user0/Strata` (PID 1552343, port 8081, HTTP 200) is still running, and I left `_Strata_/engine/strata` alone.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**/home/ai-user0/strata-data/packs/q2_0**
- `/home/ai-user0/Strata-data/packs/q2_0` has `index.txt`, `conversions.json`, `experts.bin.src.json` (the shard names and sizes), `native_experts.txt`; `mtp/` has `mtp-manifest.json` and `mtp-inventory.json`; and `data/expert-profile.bin` is the expert profile referenced in the config.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**/metrics**
- Also, while checking I noticed the config passes `--spec 4` but `/metrics` reports `"spec": 6`; the engine is apparently deriving the verify window from the MTP draft head rather than the flag.
  <sub>unknown · unknown-context.md</sub>

**_request_last**
- `prompt_read` / `prefill_ms` are parsed from the terminal summary in `serve/ipc_parser.py` (`derive_monitor_field`), carried through `_request_last` into the request record, and rendered in `serve/web/app.js`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**_shared-settings.json**
- The real configs are `ni_strata-q2_0.json`, `yi_strata-q2_0.json`, `yarnkv_strata-q2_0.json`, plus `_shared-settings.json`.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**_strata_**
- The C++ test failure is the missing `tests/CMakeLists.txt` (the archive has one; `_Strata_` doesn't).
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**active_pool_workers**
- What does `active_pool_workers` actually mean? Right now it reports the pool's configured topology count, not the number of requests in flight.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**artifact**
- `artifact` and `plan` are leaf modules with no GPU dependency, which is deliberate: `strata_artifact` and `strata_plan` are `INTERFACE` libraries with no CUDA link (`CMakeLists.txt:227, 258`).
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**backup-before-identity-rewrite**
- backup branches kept: `backup-before-identity-rewrite`, `backup-before-rebase` Deliberately not rewritten: 11 commit messages that mention Niko1221 in prose (`Rebased onto 0.1.31 (Niko1221)`, `Niko1221/Strata#653`), plus older messages naming `STRATA_RING_BYTES` / `STRATA_PREFILL_HELP` env vars that are now `HYPERCORE_`.
  <sub>2026-10-10 · strata-chat-2026-10-10-08-34.md</sub>

**batch**
- Where does instance-level config live? `batch`, `max_context`, `pool_workers` are set at `init` via argv; there is no frame to change them at runtime.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**batch_groups_test**
- `build/` was configured with `STRATA_BUILD_TESTS` off, so the `batch_groups_test` CMake target is not in `build/build.ninja`; the test was compiled standalone instead.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**boot_ms**
- With `--repeat 3`, `gen/prefill/latency` are medians but `prompt_ms`, `decode_ms`, `wall_ms`, `boot_ms`, `disk_read_mb` come from the single best-scoring boot (`bayesian_tune.py:330-333`), so the transfer columns describe a different boot than the median throughput.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**bos**
- Usable grammar with explicit ids (the README's `<[1000]>` form): One thing worth flagging, unrelated to the think ids: `tokenizer.json` maps `tokenizer.ggml.eot_token_id: 154829`, which resolves to `<|observation|>`, while `eos`/`padding`/`unknown` are all 154820 (`<|endoftext|>`) and `bos` is 154822 (`[gMASK]`).
  <sub>2026-10-15 · strata-chat-2026-10-15-21-07.md</sub>

**boundary.yml**
- In CI the second half is still correctly gated on `build-cuda/hypercore-worker` existing — a GPU-less runner can't claim it, and `boundary.yml` says so rather than faking it.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**build-cuda/hypercore-worker**
- This box has an RTX 4090 and `build-cuda/hypercore-worker` is built, so I ran the cross-backend half directly: That is the determinism half and the cross-backend half, both green.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>

**build-cuda/libhypercore_api.so.0.1.40**
- truncated: the page has more text; the first 12,000 characters are shown] abi busy precedence summary pool cancel resolve worker_mode → 8/8 PASS ``` `build-cuda/libhypercore_api.so.0.1.40` links `libcublas.so.13`, `libcudart.so.13`, `libcublasLt.so.13`; glibc floor still 2.38.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-11-52.md</sub>

**build-shared/hypercore-worker**
- `build-shared/libhypercore_api.so.0.1.40` (2.74 MB) + `build-shared/hypercore-worker` (17.9 KB), both built Oct 10 10:04.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-11-52.md</sub>

**build/cmakecache.txt**
- Note this build has `STRATA_BUILD_TESTS:BOOL=OFF` in `build/CMakeCache.txt`, so the test is not registered in ctest here; I ran it standalone against the real header.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**build/hypercore**
- `ninja -C build hypercore` fails at `CMake Error at third_party/llama.cpp/ggml/src/CMakeLists.txt:216 (configure_file): Operation not permitted`, so `build/hypercore` is still the 12:21 binary and the tail skip / look-ahead stats cannot be run on this box.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-14-18.md · also in hypercore-chat-2026-10-11-13-20.md</sub>

**build/hypercore-worker**
- `tools/calibrate.py hypercore-q2_0.json` exits with `WorkerUnreachable: worker exited during startup (1)`, and running `build/hypercore-worker` standalone gives `engine init failed (-5)` because the live server (pid 834276, port 8081) holds the GPU.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-13-20.md</sub>

**caps_size.c**
- The 12 C boundary tests compiled and ran from the copy: abi, busy, cancel, caps_size, config, pool, precedence, resolve, summary, worker_mode green (`caps_size.c` prints sizes only — it reports 28/28/40 and is not a pass/fail test; `probe.c` and `dbg_mode.c` are helpers).
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**cli_gateway.cpp**
- The worker is HyperCore's own process boundary (`src/interface/worker_main.cpp`, `engine_process.cpp`, `hypercore_bridge.cpp`, `cli_gateway.cpp`, `ipc_frame.h`, `glibc_compat.c` + `include/hypercore/hypercore_api.h`).
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**cmake**
- The Strata engine is not "another project", so `engine-internals` gained the keywords `strata`, `hypercore-worker`, `engine-src`, `engine/strata`, `cmake`: 84 entries to 156.
  <sub>2026-10-17 · 2026-10-17-21-51-This-is-your-new-HyperCore-WebUi-environment.-Add-a-button-t.md</sub>

**cmakelists.txt**
- What I deliberately did not do is `mv _Strata_ /home/ai-user0/HyperCore-WebUI`, for three reasons the numbers make plain: Moving the build trees is pure noise: they're regenerable from `CMakeLists.txt` and they make "which build am I testing?" ambiguous.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**computed exactly place**
- It is computed in exactly one place (`serve/ipc_parser.py::derive_monitor_field`); emitting it from the worker would grow the frozen 40-byte summary and add a third number that has to agree with two measured ones.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

**config/run-.json**
- `config/run-.json` is gitignored (absolute paths on one box), so a fresh clone had no config at all and `run.sh` fell back to a bare `engine/strata`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**configuring dhypercore_build_tests fails**
- Configuring with `-DHYPERCORE_BUILD_TESTS=ON` fails (`gguf_split_test.cpp:4: fatal error: strata/artifact/gguf_reader.hpp`).
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-03-30.md</sub>

**cpu_threads**
- Seven of the fourteen dimensions (`pcie`, `draft_floor`, `worker`, `suffix_draft`, `mtp_max_t`, `parallel`, `spec`) are boot-required, but `pcie_frac`, `draft_floor`, `cpu_threads`-adjacent and `spec` are not.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**decode_tok_s**
- Monitor fields. `drafts_offered`, `drafts_accepted`, `decode_tok_s`, `panic` are read at `serve/server.py:2421-2514` and by `serve/engine_bridge.py` — load-bearing, so `MONITOR_FIELDS` in `serve/ipc_parser.py` stays as the single mapping.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**docs/**
- Nothing is committed. `git status` shows 27 modified files and two new ones (`docs/`, `engine-src/interface/strata_bridge.cpp`) against the single initial commit.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**done**
- Where is the summary? `DONE` at `generate.cpp:9945` carries produced count, decode ms, stop reason, drafts accepted/offered, RAM reads, offloaded, window count.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**draft layer accepted**
- MTP draft layer accepted 298/332 drafts in my request; `--spec 6` with suffix drafts is doing real work.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**draft_floor**
- Similarly `spec_min_p` request clamps 0..1; Param `draft_floor` low 0.001 high 1.0, but engine allows 0.0.
  <sub>2026-10-15 · strata-chat-2026-10-15-16-58.md</sub>

**engine-src**
- The `engine-src` injection approach for the worker — is that acceptable, or do you want the worker built from a second clone of `hypercoreiai/HyperCore-CCP` instead?
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**engine-src/**
- My resolution: The Build tab copies `engine-src/` into `tmp/strata-<version>/` before configuring, so one cmake invocation produces all three targets: Fallback: if the injected interface fails to compile against a given Strata version, the build produces engine-only and the server runs in direct stdin/stdout mode — the UI still works, Monitor reports "worker: not built".
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**engine/build.json**
- 4.3 graceful compilation fallback. `BuildJob.build_targets()` attempts all three targets, and if the worker target fails, retries with `strata` and `strata-vision` only, logs `worker: not built - the engine runs in direct mode`, and records `"worker": false` in `engine/BUILD.json`.
  <sub>2026-10-18 · 2026-10-18-12-19-continue.md</sub>

**engine/strata**
- The only native artefact at runtime is `engine/strata`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**engine_dead**
- The cwd requirement is gone. The old `worker_binary()` ended with `return candidates[0];` — it returned `"./hypercore-worker"` even when nothing was usable, so a stray cwd turned a missing binary into an `execl` failure reported as `ENGINE_DEAD` instead of a clear error.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**eos**
- So `STRATA_STOP_REASON_CANCEL` can only be produced by the engine deciding to stop early — which is indistinguishable from `EOS` in practice.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**example consumer sends**
- For example, if a consumer sends top_k = 128, it returns STRATA_STATUS_INVALID_ARG immediately rather than bubbling down to src/kernels/sampler.cu and causing an abrupt std::exit().
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-18-55.md</sub>

**floor ggml built**
- CPU ISA. With no ISA floor, ggml-cpu is built with `GGML_NATIVE=ON` (`CMakeLists.txt:1228`), i.e.
  <sub>2026-10-15 · hypercore-chat-2026-10-15-05-16.md</sub>

**ggml-base**
- Link is only `ggml-cpu` + `ggml-base` (`CMakeLists.txt:1158`).
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**ggml-common.h**
- Actual ggml surface used is tiny and specific: 16 source/header files include `ggml.h`, `ggml-cpu.h`, or `ggml-common.h`, concentrated in `src/kernels/cpu/{iq_avx2,iq_avx512,kq_avx2,native_expert}.cpp`, `src/kernels/cuda/iq_kernels.cu`, `src/prefill/moe_fused_iq.cu`, and `src/core/mtp.cpp`.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**hypercore_batch_mtp_serial**
- `HYPERCORE_BATCH_MTP_SERIAL` is the name, `STRATA_BATCH_MTP_SERIAL` stays as the Strata-side alias, and the alias is read through the same code path so it cannot change what the engine does.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**hypercore_caps_t**
- `hypercore_caps_t` is now 28 bytes with `instance_pcie_frac` / `instance_spec_min_p`.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**hypercore_engine_init**
- Should the worker's config be per-instance or per-request? `hypercore_engine_init` takes the JSON config; per-request `pcie_frac`/`spec_min_p` already travel in the request struct.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**hypercorerequesthandler**
- `tools/calibrate.py` now builds the engine the server actually runs — the IPC worker — via a new `worker_engine()` that mirrors `serve/server.py`'s construction (supervisor + `HyperCoreRequestHandler`), with `transport=` injectable so the tests need no GPU or model pack.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**hypercorerequesthandler.info**
- `serve/server.py`'s `HyperCoreRequestHandler.info` now reports the instance's `pcie_frac` / `spec_min_p` / `pool_workers` from the worker's CAPS reply, so the sweep tunes against the baseline it is really running rather than the product defaults.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**include/hypercore**
- `CMakeLists.txt:1623` installs headers to `include/strata` while the tree is `include/hypercore`.
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**ipc_parser**
- Is `queue_hold_ms` derived in Python or emitted by the worker? Derived keeps the wire at 40 bytes but puts the arithmetic in the dashboard layer, where a reader that doesn't use `ipc_parser` can't see it.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**max_batch_slots**
- Does the boundary report pool size, or does `STRATA_POOL_WORKERS` go away? I'd rather report it than drop it — the adapter needs `max_batch_slots` to size its concurrency, and right now it can't ask.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**max_result_chars**
- And the two browser-server fixes: `WORKER_TIMEOUT_S` 120 → 80 (under the hub's 90 s ceiling, so a slow page returns an error rather than a killed call), `STRATA_BROWSER_MAX_CHARS` 12000 → 6000 (matching `max_result_chars`, so a page isn't truncated twice).
  <sub>unknown · unknown-context.md</sub>

**monitor_fields**
- Is `prefill_ms` the Monitor's `prompt_ms`? `MONITOR_FIELDS` currently maps `engine.prompt_ms → prompt_ms`.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**mtp_max_t**
- Root cause: the `suffix_draft` / `mtp_max_t` arms were built with `step=2`, so the grid was `{0,2,4,6,8}`.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**ngram**
- It is organised as eleven modules that mirror eleven CMake libraries, and the dependency graph is acyclic and layered: `artifact → plan → core → kernels → engine → prefill → program`, with `spec` and `ngram` as side branches consumed only by the driver.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**ni_strata-q2_0.shared-settings.json**
- Diff against the backup — exactly the four changes, nothing else: Plus the profile alignment: `strata-q2_0.shared-settings.json` copied to `ni_strata-q2_0.shared-settings.json`, so the Chat-tab defaults (temperature 0.7, top_p 0.95, top_k 20, reasoning_effort high) now belong to the profile that's actually running.
  <sub>unknown · unknown-context.md</sub>

**no_vram_for_mtp**
- Without engine changes, the only transfer proxy black_box has is `pcie_frac` and the `NO_VRAM_FOR_MTP` failure mode, which is what the existing sweeps already measure indirectly.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**objectives converge same**
- The two objectives converge on the same configuration. All 14 knob values are identical: `vram 512, pcie .55, kernel, mmap, inflight 128, fp16, kvstream off, no host worker, worker 10, tail 2, draft floor .445, suffix draft 2, mtp 4, parallel 1`.
  <sub>2026-10-16 · resume_strata-chat-2026-10-16-03-08.md</sub>

**prompt_read**
- The worker's terminal frame now carries the expert-tier accounting it already computed (`prompt_read`, cache hits/lookups, PCIe offloads, RAM blobs, file blobs) and the caps reply carries the VRAM tier (slots, slots full, cache MiB, cache full MiB, free VRAM, how the host experts are held, prompt chunk).
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**real finding prefill**
- The real finding: prefill is not knob-driven. In that 25-trial sweep, `corr(prefill, worker)=0.136`, `corr(prefill, pcie)=-0.108`, `corr(prefill, draft_floor)=-0.013`, while `corr(prefill, achieved MB/s)=0.811` and `corr(prefill, io_share)=0.728`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**reused**
- Native build check: Result: passed, including the ABI alignment check: So the Monitor tab’s `Reused`, `Tok/s`, and `VRAM Hit Rate` columns are no longer empty placeholders; they are fed from the worker terminal frame and rendered by the web app.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-19-47.md</sub>

**sent probe requests**
- I sent probe requests to `127.0.0.1:8081` and got clean output — but `ps` shows port 8081 is held by Strata, not black_box: No black_box worker is running.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**serve/test_abi_contract.py**
- `serve/test_abi_contract.py` proves the lock isn't vacuous — it edits the real header (insert above `top_k`, delete `top_p`, widen `top_k` to `uint64_t`) and asserts the checker fails.
  <sub>2026-10-10 · strata-chat-2026-10-10-00-02.md</sub>

**silence wall supervisor**
- The silence wall is the supervisor's own (`"engine_silence_s"`, 300 s by default; `0` waits as long as the request needs), measured from the last frame, so a six-figure prompt that takes 31 s to read is not cut off mid-prefill.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-08-01.md</sub>

**spec spec speculative**
- The `--spec 4` / `--spec-min-p 0.90` speculative decoding: the log shows "drafts accepted 0 of 0" on the degenerate turns and the MTP draft vocab includes all CJK tokens (106,299 ids) — the garbling into CJK characters ("该该该", "亚马逊pyramid", " scopes.otify") is exactly the kind of thing the draft layer proposes when the verified model's logits collapse.
  <sub>2026-10-13 · strata-chat-2026-10-13-21-31.md</sub>

**src/include/tests**
- 22 files under `src/include/tests` still carry the string `strata` (mostly GGUF wire keys like `strata.ple.format` and comments — those are intentional; a few are stale doc paths).
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**src/spec/controller.cpp**
- The choice of whether to draft at all is a separate component: `src/spec/controller.cpp`'s `CostModel::step_ms(k, mtp)` and `Controller::choose()` compare `tokens_per_ms` for `Source::None` / `Source::Mtp` / suffix drafts, and `observe()` feeds accepted counts back.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**strata-data**
- Model files — does HyperCore-WebUI download/prepare the GGUF packs, or assume an existing `Strata-data` from a prior install?
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**strata.ple.format**
- One thing I did deliberately: the GGUF metadata keys `strata.ple.format` / `strata.ple.scale` and the `# strata native experts vN` header tag are data-format contracts — model packs already on disk carry them, and a reader can't rewrite a GGUF file.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**strata_batch_mtp_serial**
- One thing to flag up front: `STRATA_BATCH_MTP_SERIAL` does not exist in this checkout, so this was an implementation of the #2038 behaviour, not a rename.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**strata_benchmark**
- MCP annotations are set properly: `strata_status`, `strata_models`, `strata_logs`, `strata_benchmark`, `strata_connect_info` are `readOnlyHint: true`; `strata_stop` is `destructiveHint: true`; `strata_install` and `strata_start` are `idempotentHint: true`.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**strata_build_conversation_tests**
- They cluster by module exactly as the code does: Note the collision: `STRATA_BUILD_CONVERSATION_TESTS` and `STRATA_ENABLE_HIP` are both CMake options and identifiers in the source — the build-time and runtime namespaces overlap, which is a real footgun when mapping this surface.
  <sub>2026-10-09 · strata-chat-2026-10-09-08-03.md</sub>

**strata_engine**
- The real cross-machine test needs `strata_engine` behind it, and a Daytona sandbox has no NVIDIA card — so it can validate link-time ABI stability (struct layout, symbol visibility, calling convention), not behaviour.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**strata_mmq_kquants**
- This is a cross-cutting layer, not a directory: Quant-format capability is likewise compile-gated: `STRATA_MMQ_KQUANTS` (Q4_K/Q5_K/Q5_1/Q6_K), `STRATA_Q6K_EXPERTS`, `STRATA_ORCA_Q4KS_MMQ`, `STRATA_NATIVE_EXPERTS`.
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-08-03.md</sub>

**strata_shared**
- If the boundary becomes the supported entry point, the executable should link `strata_shared` rather than the internals — otherwise you maintain two paths to the same engine and the disagreement between `serve/server.py:849` (clamps `top_k`) and the boundary (refuses it) is the first place it shows.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**streaming engine more**
- KV streaming (engine 0.1.5): at 64K and more, setup keeps the context's KV cache in RAM and only the part the attention reads in VRAM (`--kv-resident 32768`), so more experts fit on the GPU.
  <sub>2026-10-04 · 2026-10-04-strata-chat-2026-10-04-03-57.md</sub>

**text protocol integration**
- The text-protocol integration tests (test_slots.py, test_control_cancel.py, test_fatal_recovery.py) must be rewritten using ipc_parser and binary frame fixtures rather than retired.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**tools/daytona_bridge_test.py**
- Cross-toolchain stage: now green. `tools/daytona_bridge_test.py` ran end-to-end: 8/8 C tests (abi, busy, precedence, summary, pool, cancel, resolve, worker_mode) compiled and ran on a foreign glibc 2.41 host against the artifact built here.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md · also in 2026-10-10-hypercore-chat-2026-10-10-11-52.md</sub>

**tools/test_strata_agent.py**
- 10 stub tests pass (`tools/test_strata_agent.py`): clean stop, length-continuation, bounded continuations, engine-death retry, user-stop detection, silence-is-a-stall, tool-activity-counts-as-progress, long-prefill-is-not-a-stall, unreachable server.
  <sub>2026-10-03 · strata-chat-2026-10-03-19-51.md</sub>

**verified grep mtp_serial**
- Verified: `grep -rn "MTP_SERIAL" src include docs serve tools tests` returns nothing, and `git log --all -S "STRATA_BATCH_MTP_SERIAL"` has no commits.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**vision_start_reason**
- `log.tell()` on a handle opened in append mode is 0 until it is written to, so `vision_start_reason` read the whole log and returned its last line — the engine's `hypercore serve: mtp: the draft head does not fit` from setup's tuning run.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**yi_strata-q2_0.shared-settings.json**
- The values in `yi_strata-q2_0.shared-settings.json` (temperature 0.7, top_p 0.95, top_k 20, reasoning_effort high) are simply dead.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md · also in strata-chat-2026-10-07-09-31.md</sub>

## Decisions taken

what was chosen, and why, oldest first


**--batch-mtp**
- What was added Verified in this session One thing you should know before using it `black_box` at 0.1.42 still refuses `--batch-mtp` on any layer split (`generate.cpp:3601`, "it is for one GPU (no layer split or helper) for now").
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>

**--builds**
- `--builds` would delete `build-shared/hypercore-worker` and `build/libhypercore_api.so`, which `tools/bench_flood.py`, `tools/backend_parity.py`, `tools/native_probe.py` and the calibration I just fixed all read by default.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**./hypercore-worker**
- `tools/daytona_bridge_test.py` re-run against the patched source: `remote_c_fail=0`, 8/8 C tests on glibc 2.41 — the harness uploads `hypercore-worker` next to the test, so the `./hypercore-worker` branch still resolves there.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**.bak**
- The configs on disk are `ni_strata-q2_0.json` (+ `.bak`, and the `_`-prefixed copy), `yarnkv_strata-q2_0.json`, `yi_strata-q2_0.json`.
  <sub>2026-10-10 · strata-chat-2026-10-10-03-30.md</sub>

**abi_test.c**
- Files are in `bench/results/2026-10-09-blackbox-api/`: `strata_bridge_review.cpp` (as written), `strata_bridge_fixed.cpp` (compiles, 5 exported entry points), `abi_test.c` and `busy_test.c` (both pass).
  <sub>2026-10-09 · 2026-10-09-strata-chat-2026-10-09-18-55.md</sub>

**config/run-q2_0.json**
- Housekeeping — `tools/hypercore_tokenizer.py` was missing (the server imports it for the pack tokenizer); the test stub config moved to `tests/`; `config/strata-q2_0.json` renamed to `config/run-q2_0.json` (the plan's convention, and it's gitignored); `engine/strata` + `engine/strata-vision` + `engine/BUILD.json` adopted from your existing build so the project is self-contained.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**decode_ms**
- Decision 1 — the premise was already violated in code, so I fixed the code, not the doc. `include/hypercore/hypercore_api.h` claimed `decode_ms` "includes draft verification latency", but `src/interface/hypercore_bridge.cpp` measured it from the first token callback.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**engine/hypercore**
- Frozen evidence — confirm the list. Specifically `engine/strata`: is that binary evidence to preserve, or should it be rebuilt as `engine/hypercore` from the renamed target?
  <sub>2026-10-10 · strata-chat-2026-10-10-05-20.md</sub>

**ipc_parser.py**
- Is the terminal frame a memcpy of the C struct, or a separately declared packed layout? I'd make it explicit and keep `struct_size` as the version tag — but that means `worker_main.cpp` and `ipc_parser.py` share one table that must be edited together.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**niko1221/strata**
- It is not a toy project — it's a full handoff mirror of "HyperCore" (the renamed Strata engine, GitHub `Niko1221/Strata`), rsync'd on 2026‑10‑10 to freeze the boundary surface (public C API + worker + IPC framing + supervisor) for review.
  <sub>2026-10-10 · 2026-10-10-strata-chat-2026-10-10-07-17.md</sub>

**prefill_fused_iq_test**
- Fixed in `CMakeLists.txt:1316` (mmq target now carries `CUDA::cuda_driver` and the blas target); that also fixed `prefill_fused_moe_test` and `prefill_fused_iq_test`.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>

**prefill_ms**
- Is TTFT meant to include the hold? As implemented it starts at request acceptance, so it overlaps `prefill_ms` completely.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**prompt_reused**
- The Monitor path is now connected end to end: The populated fields include: I also found and fixed a real bridge bug: the terminal frame was discarding `prompt_reused`.
  <sub>2026-10-10 · 2026-10-10-hypercore-chat-2026-10-10-19-47.md</sub>

**resp_caps**
- The bridge fills them from the already-resolved Level-1 options; the worker's `RESP_CAPS` payload is 28 bytes; `ipc_parser.decode_caps()` returns them; the supervisor mirrors them on the instance so a handler doesn't re-query.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**static_assert**
- CI decision. The layout check won, and it is wired in: `add_custom_target(hypercore_abi_alignment_check ALL ...)` at `CMakeLists.txt:1492` → `tools/check_abi_alignment.py`, which parses the `static_assert` sizes out of the header and diffs them against the Python `struct.Struct` mirrors.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

## Measured on this machine

numbers actually measured, not estimated


**/home/ai-user0/strata/engine/strata** · 7 chats
- Current state: the card is held again — pid 3441354 `/home/ai-user0/Strata/engine/strata`, 24104/24564 MiB.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>
- Two separate causes, both verified: a) Right now the card is full. `nvidia-smi` shows 24122 / 24564 MiB used by pid 3431164, `/home/ai-user0/Strata/engine/strata` (started 13:00).
  <sub>2026-10-14 · strata-chat-2026-10-14-13-13.md</sub>
- Blocked, and it is the next step: the card is held by `/home/ai-user0/Strata/engine/strata` (pid 3421829, 23614 MiB of 24564).
  <sub>2026-10-14 · strata-chat-2026-10-14-12-53.md</sub>
- What's blocking the live debug run right now: the card is busy — `/home/ai-user0/Strata/engine/strata` (pid 3336849) holds 24 068 MiB, 38 MiB free.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>
- I started the probe: it got past argument parsing and reached engine init, then stopped at `native embedding: cannot pin 260 MiB, nor place it in VRAM` — the card is held by `/home/ai-user0/Strata/engine/strata` at 24050 of 24564 MiB.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>
- I could not run it myself: your `/home/ai-user0/Strata/engine/strata` is holding 24076 MiB of the 4090 with 30 MiB free — even the parity test OOMs.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-06.md</sub>
- So the first line the server saw was `[New LWP 187521]`, and the actual reason went to stderr, into the log, unseen: Same underlying condition as the engine failure: the 4090 is full — pid 178997 (`/home/ai-user0/Strata/engine/strata`) holds 24070 of 24564 MiB.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**card free 3679801** · 2 chats
- 244 MiB less VRAM free at the sizing moment (the Strata run had the MTP draft layer at 834 MiB versus 818 MiB, plus other allocations) costs 187 slots.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-12-04.md</sub>
- The card is not free. PID 3679801 (`/home/ai-user0/Strata/engine/strata --serve --pack …/q2_0`) holds 23 804 MiB of 24 564 MiB.
  <sub>2026-10-15 · strata-chat-2026-10-15-09-32.md</sub>

**decode_ms** · 2 chats
- That confirms both of your decisions empirically: `prefill_ms` is the slot hold (45 ms = `40 + max_new/2`, and `resolve_test` shows it collapsing to 0 at `pcie_frac=1.0`), and `decode_ms` really is 0 in the stub.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>
- Now: Tested, not asserted: `test_prompt_timing_does_not_move_with_the_draft_cycle` runs the same request with and without a 150 ms draft cycle and requires `prefill_ms` to stay put while `decode_ms` and `ttft_ms` move.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**empirical test results** · 2 chats
- Engine + boundary + test harness: ~90–95% complete and green — it builds, the ABI contract locks, the supervisor and IPC framing suites pass, and the docs are thorough.
  <sub>2026-10-10 · strata-chat-2026-10-10-07-17.md</sub>
- Your empirical test results (45 ms == 45 ms) expose a serious design flaw: as long as prefill_ms captures the queue sleep, it is tracking gate delays, not core hardware computation.
  <sub>2026-10-09 · strata-chat-2026-10-09-22-41.md</sub>

**experts real strata** · 2 chats
- The guard. If 90% or more of the experts are already in the GPU cache (60% on a HIP layer split), the default is not set and you get the opposite message: `STRATA_ROUTE_TAIL_SKIP is off by default: X% of the experts are in the GPU cache, so few are missed and skipping costs more draft acceptance than it saves`.
  <sub>2026-10-13 · hypercore-chat-2026-10-13-03-22.md</sub>
- 12,630 experts / 16.26 GiB is real: Strata log lines 20-21, `expert cache 12630 slots, 16.26 GiB of VRAM`.
  <sub>2026-10-11 · hypercore-chat-2026-10-11-15-51.md</sub>

**reason nothing measured** · 2 chats
- The deployed config is now measured, not inferred. Your pinned run (`vram=1024 pcie=0.0368 worker=17 draft-floor=0.8`, repeat 3) gives 944.63 tok/s prefill, 166.49 gen, 23378 ms, bus 76%, 23964 MB read, 1322 MB/s.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>
- And the reason nothing measured: the GPU is at 23802 MiB of 25334 MiB, 360 MiB free — `/home/ai-user0/Strata/engine/strata --serve --pack .../q2_0` is holding it.
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**.env**
- The project is 8.6 GB, and 8.0 GB of it is regenerable: `git clean -ndx` would sweep 429 items — but it would also take things you probably want to keep: `engine/` (the 120 MB engine binary), `third_party/llama.cpp/` (167 MB, needed by `tools/iq_fixture.py` and `tools/_paths.py` for `gguf-py`), `.env`, and the transcript `strata-chat-2026-10-10-00-02.md`.
  <sub>2026-10-10 · strata-chat-2026-10-10-12-04.md</sub>

**/health**
- There is an engine running. `ps` shows PID 150375 — `/home/ai-user0/Strata/engine/strata`, 24.4 GB RSS, started by `/home/ai-user0/Strata/serve/server.py --port 8080`, and it answers `/health` right now.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**/metrics**
- `--expert-cache auto` became 11,015 slots / 14,521 MiB; the About tab's "Speculation" line is computed from the engine's reported `mtp_max`/`spec` (`MTP drafts up to max(mtp_max, spec) - 1`), which reads 6 in `/metrics` even though the flag says `--spec 4`.
  <sub>2026-10-07 · strata-chat-2026-10-07-09-31.md</sub>

**after reading prompt**
- after reading the prompt, it reads the user input and then stops with no model write: strata] reading the prompt: 112,498 of 112,948 tokens, 26 s so far [strata] thinking: 1 of max 149188 tokens, 381300.4 tok/s, 27 s [strata] done: 14 tokens in 28 s (138.8 tok/s) (stop, cancel=False), expert cache 94.5% hit
  <sub>2026-10-06 · 2026-10-06-strata-chat-2026-10-06-18-57.md</sub>

**also tuning failed**
- This is also why `Tuning: FAILED` said "the draft head does not fit: needs 178 MiB of VRAM and 170 MiB is free" — the MTP head competes for the same held-back pool.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**build-shared/hypercore-worker**
- Also, `--short --dry-run` no longer runs a real calibration; it says so and falls back to the staged sweep: The next blocker you will hit `nvidia-smi` reports 28 MiB free right now: `serve/server.py` (pid 1646195) is running with `build-shared/hypercore-worker` holding the card.
  <sub>2026-10-12 · hypercore-chat-2026-10-12-13-18.md · also in hypercore-chat-2026-10-12-11-07.md</sub>

**include/strata/plan/plan.hpp**
- Memory plan — `include/strata/plan/plan.hpp`, CLI `src/plan/plan_main.cpp`: "the engine has 33.97 GB of expert weights … a fixed VRAM budget that has to hold the dense weights, the KV cache, the indexer keys, the recurrent state and as many expert-cache slots as remain." Computed from measured free VRAM, not configured.
  <sub>2026-10-16 · clean_add_timing_strata-chat-2026-10-16-02-09.md</sub>

**iota**
- Run this next — I couldn't, the card is held by the Strata engine (pid 3459587, 24104/24564 MiB): It will now print `prefill: fault seen at gr_broadcast` (or `ple_upload` / `steps_upload` / `iota` / `before the prompt path starts`) instead of a phase name.
  <sub>2026-10-14 · strata-chat-2026-10-14-15-52.md</sub>

**pinable flags grow**
- The pinable CLI flags are: --ple-io, --kv, --kv-grow, --host-core, --pool-affinity, --pool-workers, --prompt-cache-tail, --prefill.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md · also in strata-chat-2026-10-17-14-18.md</sub>

**src/prefill/**
- The verify window is now proven clean, so the fault is in `src/prefill/` (the batched group copy at `prefill.cpp:2614` is where the reported kernel is), and it needs the card free to reproduce — the Strata engine (pid 3443824) currently holds 24104 of 24564 MiB.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**tier takes slots**
- The KV tier takes slots from the expert tier as context grows: At 131072 the KV hands back roughly 0.56 GiB compared with 262144, which is about 430 more resident experts.
  <sub>2026-10-17 · hypercore-chat-2026-10-17-00-38.md</sub>

**verdict runs pasted**
- Verdict on the two runs you pasted: the `--prefill-cap auto:16384` probe is a clean, controlled A/B, and it lost. Same configuration in both, `vram=1280 pcie=0.0917 worker=16 tail=1 draft_floor=0.296`, three boots each: `-19.4%`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

**what ramp made**
- What the ramp is made of, from the log: the VRAM expert tier fills across the session (38,435 → 51,309 exchanges with the tier), and draft acceptance rises from 86–94% on short requests to 97% on long ones.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-18-06.md</sub>

## From outside this project

figures a chat took from a source we did not run here; check the source before repeating one


**verdict give defensible**
- Verdict: I can give a defensible probabilistic forecast, but the honest headline is that the dominant uncertainty is observational, not mechanistic. Forecast under stated pathways. IPCC AR6 gives a likely decline of ~34% (10–43%) by 2100 under high emissions, and explicitly states low confidence in collapse.
  <sub>2026-10-07 · strata-chat-2026-10-07-18-16.md</sub>

## Gotchas

things that bit once and will bite again


**--gen**
- Also worth porting directly, because they fix the specific defects found in the black_box data: `other_objective_history()` (the deployed config came from a `--gen` sweep and was never re-scored under `--prefill`), `FAIL_STREAK` (49 `NO_VRAM_FOR_MTP` timeouts are being re-booted), and the digest in `run_log_name`.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-12-00.md</sub>

**./hypercore-worker**
- One gotcha worth knowing: `worker_mode_test.c` resolves the worker binary from `./hypercore-worker` in the cwd.
  <sub>2026-10-10 · hypercore-chat-2026-10-10-11-52.md</sub>

**bstop**
- Add `STRATA_STOP_REASON_CANCEL`, or say explicitly that the boundary never cancels — but then the protocol's `STOP`/`BSTOP` have no equivalent, and a caller that stops reading tokens still holds a batch slot until the engine finishes.
  <sub>2026-10-09 · strata-chat-2026-10-09-18-55.md</sub>

**build-shared/hypercore-worker**
- Call sites: Production now runs `HyperCoreProcessSupervisor` → `build-shared/hypercore-worker` over a Unix socket (`serve/server.py:4587`), and that path never calls the narrator.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**config/**
- a bad name now lists what is in `config/` instead of just saying "not readable" Engine resolution — implemented the order the README promised but the code never had: run config `"exe"` → `engine/strata` in this project → `$STRATA_ENGINE`.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**copy_i32**
- Why all three arms printed the same line: they never reach the code that differs. `hypercore-q2_0.log` (13:19, the last arm) contains one load, and it ends at: There is no `verify: MMVQ_IL trace`, no `interleaved matvec` line — the worker dies in the batched prompt path (`src/prefill/prefill.cpp:2614`, the first `copy_i32` of the routed ids) before any interleaved call exists.
  <sub>2026-10-14 · strata-chat-2026-10-14-14-42.md</sub>

**decode_ms**
- Those were empty for every real request, and the cause was in the engine adapter, not the dashboard: Closing the generator at the end-of-turn token means the worker's terminal frame is never read, so `HyperCoreRequestHandler.last` stayed `None` and every column fed from it (`reused`, `prompt_read`, `decode_ms`, `decode_tok_s`, `hit_rate`) rendered as "–".
  <sub>2026-10-11 · hypercore-chat-2026-10-11-11-46.md</sub>

**engine-src/**
- Next up, if you want me to keep going: an actual from-source Build-tab run (clone → inject `engine-src/` → configure → build → transfer → clean `tmp/`) against v0.1.45, and a Tune-tab dry run with the stub engine so the background sweep and the "do not close the page" note are exercised.
  <sub>2026-10-17 · strata-chat-2026-10-17-20-23.md</sub>

**ggml/**
- The engine build only reads `ggml/`, so it passed; the gap only appeared when the image encoder was configured — hence "exit 1: cmake" exactly at the images step, with a file setup never mentioned.
  <sub>2026-10-10 · strata-chat-2026-10-10-20-47.md</sub>

**gguf kv_modes_for returns**
- from GLM-5.3-…gguf`), `kv_modes_for()` returns the arms that runner can run, and `build_space(pack=…, has_drafter=…)` gates: `engine_args()` now refuses a request that cannot fit the context (`generate.cpp:3620/3624`) before booting, and never emits `--kv k8v4` with `--kv-streaming on` (3569 exits 2; 3405 sets the resident window).
  <sub>2026-10-15 · strata-chat-2026-10-15-14-22.md</sub>

**native_mmvq_il**
- `native_mmvq_il` is called from exactly one place; the MTP drafter has its own `xq_` and never touches the copy.
  <sub>2026-10-14 · strata-chat-2026-10-14-12-53.md</sub>

**winner worker pcie**
- The "winner" (`worker=20 pcie=0.1348 draft_floor=0.2626`, prefill 2306.88) is a re-scored prior, never re-booted.
  <sub>2026-10-16 · hypercore-chat-2026-10-16-13-27.md</sub>

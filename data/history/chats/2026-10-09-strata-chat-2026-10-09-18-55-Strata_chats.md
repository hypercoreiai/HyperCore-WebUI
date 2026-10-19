# strata-chat-2026-10-09-18-55.md

2026-10-09 · 2520 KB · 3 requests · topics: deployment-permissions, engine-internals, general-knowledge, hardware-environment, issue-log, mcp-tooling, memory-vram, performance-benchmarks, setup-configuration, testing-quality

## What was asked
- The attached file is the context: project home: /home/ai-user0/HyperCore-WebUI, code name: hypercore, atributions: Qwen-Next-strata
- Can you use this platform to test your code? pip install daytona
- api+key: [key] https://app.daytona.io/dashboard/onboarding

## What came of it
- A `StrataEngine` class and a JSON→`Options` mapper are both implementations of a boundary; if the boundary is undefined, you end up with a class that happens to be CUDA-shaped and a config mapper that can't be tested against anything.
- `strata_session_t` is the wrong unit. The engine already owns conversation state: a request continues "from the live session or the longest conversation checkpoint its prompt starts with" (generate.cpp:5036).
- Requests are the unit; conversation reuse is inside the box, observable through `summary.prompt_reused`.
- Per-request sampling is on the hot path and needs validation (`top_k` must be 1..64).
- `CMakeLists.txt` has no library target at all today — only `add_executable` and `INTERFACE` libs — so the `.so` needs a target too.
- Do internal `std::exit()` paths become error codes, or does the box stay crash-on-error and the embedder accepts a lost process? This determines whether the `.so` is embeddable at all, and it's the largest single piece of work in the refactor.
- Is the SYCL/HIP side expected to implement this contract, or is the `.so` CUDA-only for v1? If the 25-header `sycl/` tree is meant to implement it, the contract needs a capability-negotiation step now — otherwise you ship a C API that is a CUDA wrapper with a neutral name, which is the failure mode your third decision was specifically trying to avoid.
- If the host application submits a request when all concurrent slots configured via --batch are full, the engine will refuse the payload.
- For example, if a consumer sends top_k = 128, it returns STRATA_STATUS_INVALID_ARG immediately rather than bubbling down to src/kernels/sampler.cu and causing an abrupt std::exit().
- `<cstdlib>`, `<cstring>`, `<new>` are missing.
- `run(cfg, say, start_engine)` takes `start_engine` as an injected parameter, and `tools/test_calibrate.py` already injects a stand-in.
- Add those two fields to the request config — that's exactly what `struct_size` is for.
- Either is fine; both are additive, and neither is expressible with the current `strata_output_chunk_t`.
- Files are in `bench/results/2026-10-09-blackbox-api/`: `strata_bridge_review.cpp` (as written), `strata_bridge_fixed.cpp` (compiles, 5 exported entry points), `abi_test.c` and `busy_test.c` (both pass).
- Script is at `tools/daytona_bridge_test.py`; it's `ruff`-clean.

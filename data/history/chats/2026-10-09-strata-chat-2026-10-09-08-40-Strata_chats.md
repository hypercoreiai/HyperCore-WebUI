# strata-chat-2026-10-09-08-40.md

2026-10-09 · 946 KB · 9 requests · topics: deployment-permissions, mcp-tooling, web-ui-monitor

## What was asked
- Map the Strata engine (/home/ai-user0/Strata/src, /home/ai-user0/Strata/include/strata, /home/ai-user0/Strata/third_party) in a way structured to understand codebase mapping or structural visualization -> Strata_engine_map
- was this code written by a person
- is ollama opensource
- map the flags used in strata tune and config back to the map above
- can you read this website: https://build.nvidia.com/models?filters=nimType%3Anim_type_preview
- can you see the model card that are free endpoints
- save it to a csv
- I added a new line to the system prompt that starts with the word NEVER. do you see it?
- how about now

## What came of it
- The engine is a three-tier, header-first C++20/CUDA codebase with a strict separation between interface (`include/strata/`, 118 headers, ~12.3k lines) and implementation (`src/`, 183 files, ~88.2k lines of `.cpp`/`.cu`).
- It is organised as eleven modules that mirror eleven CMake libraries, and the dependency graph is acyclic and layered: `artifact → plan → core → kernels → engine → prefill → program`, with `spec` and `ngram` as side branches consumed only by the driver.
- One source tree, three GPU backends. CUDA is the native dialect; AMD is reached by a header shim (`include/strata/hip_compat/`, `include/strata/platform/hip_compat/`) that redefines `cuda_runtime.h`/`cublas_v2.h` onto HIP so "the engine's sources stay one tree" (`include/strata/platform/hip_compat/strata_hip.h:2-6`).
- Intel Arc is a separate project (`sycl/`, 202 files) that reuses the same `src/` and is mutually exclusive at configure time (`CMakeLists.txt:14-20`).
- `third_party` is a narrow, pinned dependency: only ggml is consumed, and only three files are vendored in-tree; the build normally FetchContent-pulls llama.cpp at commit `3cf03257f219afbe7334045ff7c6a06ac68c627d` (2026-09-20).
- `core ↔ kernels` is the dominant coupling (112 + 7 edges).
- `program` is the only consumer of `spec` and of `prefill` — spec/prefill are not part of the core.
- `artifact` and `plan` are leaf modules with no GPU dependency, which is deliberate: `strata_artifact` and `strata_plan` are `INTERFACE` libraries with no CUDA link (`CMakeLists.txt:227, 258`).
- This is a cross-cutting layer, not a directory: Quant-format capability is likewise compile-gated: `STRATA_MMQ_KQUANTS` (Q4_K/Q5_K/Q5_1/Q6_K), `STRATA_Q6K_EXPERTS`, `STRATA_ORCA_Q4KS_MMQ`, `STRATA_NATIVE_EXPERTS`.
- It is not referenced by the build — no `third_party/llama.cpp` path appears in `CMakeLists.txt`, `setup.py`, or `setup.sh`.
- Actual ggml surface used is tiny and specific: 16 source/header files include `ggml.h`, `ggml-cpu.h`, or `ggml-common.h`, concentrated in `src/kernels/cpu/{iq_avx2,iq_avx512,kq_avx2,native_expert}.cpp`, `src/kernels/cuda/iq_kernels.cu`, `src/prefill/moe_fused_iq.cu`, and `src/core/mtp.cpp`.
- Link is only `ggml-cpu` + `ggml-base` (`CMakeLists.txt:1158`).
- Structural implication: llama.cpp is vendored as a reference/upstream mirror, not as a linked dependency.
- Parity/bench/test files are colocated with the kernels they check (`src/kernels/_parity.cpp`), plus a separate `tests/` tree: `tests/core` 12 files, `tests/hip` 16, `tests/cuda` 3, `tests/prefill` 1, `tests/platform` 1, `tests/data/native_experts` 6 fixtures.
- The pattern is systematic: every GPU kernel in `src/kernels/cuda/` has a CPU or reference counterpart driven from `src/kernels/` — e.g.

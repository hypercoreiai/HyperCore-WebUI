# strata-chat-2026-10-10-03-30.md

2026-10-10 · 1266 KB · 2 requests · topics: deployment-permissions, engine-internals, hardware-environment, issue-log, mcp-tooling, setup-configuration, testing-quality

## What was asked
- You are working on a project in /home/ai-user0/_Strata_ so you need to review the project
- continue, ask questions when necessary

## What came of it
- Test culture is genuinely strong: 94 Python test files, 37 C++/CUDA/HIP test files, ABI alignment checks (`tools/check_abi_alignment.py`, `serve/test_abi_contract.py`), a glibc-floor gate, cross-toolchain test (Daytona), backend-parity tests.
- 21 Python files are unparseable. A bulk find/replace inserted a stray `"` after every bare `strata`:
- `tools/` test suite cannot run. Beyond the syntax errors, `tools/test_clean_up.py:8` does `import clean_up`, which only resolves when run from inside `tools/`.
- CI runs `python3 -m unittest tools.test_glibc_floor` from the root; the rest of the suite is unreachable from the documented invocation.
- The C++ test suite does not compile. 32 files under `tests/` still `#include "strata/..."` while the headers now live in `include/hypercore/`.
- Configuring with `-DHYPERCORE_BUILD_TESTS=ON` fails (`gguf_split_test.cpp:4: fatal error: strata/artifact/gguf_reader.hpp`).
- The rename script itself is the root cause. `tools/rename_hypercore.sh` greps only `src include sycl serve tools bench CMakeLists.txt` — `tests/` is not in the list.
- Environment-variable names are split. `serve/rag.py:23` reads `HYPERCORE_RAG_ROOT`, but `serve/mcp/tests/test_home.py` and `test_contracts.py` still set `STRATA_RAG_ROOT`.
- `serve/docs_mcp.py:93` reads `STRATA_DOCS_ROOT` while everything else is `HYPERCORE_`.
- This is why the MCP suites regressed: an earlier run (``.run_all_now.txt`, Oct 6) shows 204/204 OK; today the same suites produce 22 failures + 11 errors, e.g.
- `path must be a directory inside /home` — the tests' temp workspace is ignored because the env var name no longer matches.
- CI is not running. `.github/workflows/` is untracked; `raw.githubusercontent.com/.../main/.github/workflows/boundary.yml` returns 404.
- There is no README in the working tree. `README.md`, `README.{de,es,fr,ja,pt-BR,zh-CN}.md`, `AGENTS.md`, `SECURITY.md`, `SETUP.bat`, `START-HERE.bat`, `UPDATE.bat` are all deleted (uncommitted), while 8 doc links still point at `../README.md` and at `paper/HyperCore-Paper.pdf` (the file is `docs/paper/Strata-Paper.pdf`).
- Stale references that will break at runtime. `setup.py:4810` calls `str(ROOT / "tools" / "strata_tokenizer.py")` — that file is now `hypercore_tokenizer.py`.
- `serve/web/app.js:1151` still reads `j.strata_mcp`.

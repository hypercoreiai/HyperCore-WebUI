# unknown-end_hypercore.md

unknown · 0 KB · 0 requests · topics: nothing worth keeping

## What was asked

## What came of it
- cross-toolchain: build `hypercore_api hypercore_glibc_floor_check` so the floor is reported, and also run the artifact floor check against the sandbox floor.
- tools/glibc_floor.py reads it off the binary; --require fails the build.
- README section 26 documents the failure, the fix, and the measured floors; `.blackbox_api_state.json` is at loop 42 and parses.
- The floor gate special-cases `sqrtf`; a newly versioned symbol from ggml would fail the build with the symbol named, and the fix is one more `__wrap_` shim — there is no generated shim yet.

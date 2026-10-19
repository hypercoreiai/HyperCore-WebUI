# Where the engine stands

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

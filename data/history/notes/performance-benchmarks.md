# What has actually been measured here

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

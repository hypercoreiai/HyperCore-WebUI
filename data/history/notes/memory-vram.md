# Memory: what is on the GPU and what is not

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

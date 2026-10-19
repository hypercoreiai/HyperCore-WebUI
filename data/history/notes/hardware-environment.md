# The machine this runs on

**GPU.** An NVIDIA RTX 4090 (24 GB) is the working card; the 5090 and 3090 are the other
architectures the ready-made engine builds cover. `nvidia-smi` numbers the cards, and that is the
number `--gpu` and the run config use. AMD is supported through HIP/ROCm (`--backend hip`),
chosen automatically when there is no usable NVIDIA card; the engine refuses a hipBLASLt version
it has no table for and falls back to plain hipBLAS.

**Engine builds are per-architecture.** A ready-made build exists for RTX 20/30/40/50; if none
fits, setup installs the build tools and compiles for the GPU in the box. AVX-512 is detected and
used for the CPU path where available.

**PCIe matters more than people expect.** `--pcie-frac` is tuned by measuring this box, not by a
default; the measured value on this machine is 0.00, and a config that still carries the 0.75
default is running against a baseline it never measured.

**Firefox is needed for the browser tools.** Without it (or `STRATA_FIREFOX` pointing at a
browser) the browser MCP server reports that it cannot search or scrape. `xvfb` is used for the
headless Playwright path.

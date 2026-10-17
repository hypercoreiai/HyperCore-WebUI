# HyperCore-WebUI

**HyperCore — Powered by Strata.**

The HyperCore web app (Chat, Assist, Monitor) running on a stock
[Strata](https://github.com/Niko1221/Strata) engine, plus two tabs the engine
does not ship: **Tune** (an Optuna sweep over the engine's settings) and
**Build** (check for a new Strata release, get it, and compile it).

This repository contains no engine source. The only native thing it needs at
runtime is `engine/strata`.

---

## What it is

| Tab | What it does |
|---|---|
| **Chat** | OpenAI/Anthropic chat over the engine, streaming, thinking, agent profiles, attachments, images, MCP tools |
| **Assist** | A second chat through any OpenAI-compatible endpoint, with a lag-probed model list |
| **Monitor** | Live engine and hardware metrics, request table, conversation cache, MCP switches |
| **Tune** | Runs `optuna_tune/max_tune.sh` in the background and reports the sweep and the winner's benchmark |
| **Build** | Version check against Niko1221/Strata, ready-made download or a from-source build with dropdowns read from the release itself |
| **About** | Engine provenance, this PC, API addresses, model settings |

## Running it

```bash
./setup.sh                 # create .venv, find or fetch an engine
./run.sh                   # start the WebUI on http://localhost:8081
```

Point the app at an engine and a model in `config/run-<model>.json`, or set them
in the Config view. The defaults are host `0.0.0.0`, port `8081`, API key `0000`.

The engine is found in this order: `--engine`, the run config's `"exe"`,
`engine/strata`, then `STRATA_ENGINE`.

## How the Build tab works

1. Reads the installed version from `engine/BUILD.json`, or from the
   `engine=X.Y.Z` string compiled into the binary.
2. Queries the GitHub releases API for `Niko1221/Strata`.
3. Either downloads the ready-made archive for this OS and verifies its published
   SHA-256, or shallow-clones the release into `tmp/`, builds it, and copies the
   binaries into `engine/`.
4. The build dropdowns are extracted from the cloned release's own
   `CMakeLists.txt` and `setup.py` by `tools/extract_build_options.py`, and
   diffed against the previous manifest so the tab can show what changed.

`tmp/` holds exactly one clone, is deleted when a build finishes, and is never
read at runtime. Deleting it changes nothing.

## How the Tune tab works

The sweep is a subprocess of `optuna_tune/bayesian_tune.py`, so the CLI stays
the single source of truth and the web server stays responsive during a run that
can take an hour. The chat engine is stopped first, because a sweep boots its own
engine and needs the GPU.

**Do not close the page while a tune is running.** The results are written to
disk by the subprocess and reported here when it exits.

## Layout

```
serve/          the web app and its server
optuna_tune/    the Bayesian tuner
tools/          the build-option extractor and the build script
engine/         the engine binaries (not committed)
engine-src/     the HyperCore process boundary injected into a Strata clone
config/         run configs and the build manifest
prompts/        the agent profiles
data/ logs/ tmp/  local, gitignored
```

Nothing loose sits in the repository root.

## License

MIT. The Strata engine is copyright Niko1221 and contributors, MIT licensed.

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
in the Config view. `config/run-example.json` is the template; copy it and fill in
the paths. The defaults are host `0.0.0.0`, port `8081`, API key `0000`.

The engine is found in this order: `--engine`, the run config's `"engine_url"`,
`--engine-url` / `$HYPERCORE_ENGINE_URL`, the run config's `"exe"`,
`engine/strata`, then `STRATA_ENGINE`.

## One engine per machine

A Strata engine is tens of GB. Two WebUIs on one PC cannot both hold it, so the
second one attaches to the first instead of starting a second copy:

```bash
HYPERCORE_ENGINE_URL=http://127.0.0.1:8080 ./run.sh
```

`run.sh` also checks the port before starting: if the WebUI is already up there, it
opens that page rather than loading the model twice.

Attaching goes through the OpenAI API the running engine's server already speaks.
The prompt is decoded with the model's tokenizer, sent as one message, and every
text piece that comes back is encoded again, so Chat, the token counters and the
Monitor see the same token stream as from a local engine. The server that started
the engine still owns it: `stop`, `unload` and VRAM resize are asks, not commands,
and the About tab says which address the engine lives at.

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

### Priors: reusing measurements you already have

A sweep does not start from nothing. `optuna_tune/history/` holds one file per
machine, model, objective and context length, and each sweep folds its own trials
into it, so the next one searches around what already worked on this box.

If you have a previous HyperCore install, its measured configurations can be brought
over:

```bash
python tools/import_tune_history.py                      # from ~/black_box/optuna_tune/history
python tools/import_tune_history.py --source /path/to/history --top 50
python tools/import_tune_history.py --all                # keep every configuration
```

The importer keeps the record format unchanged and drops the weight: DRY rows (a
`--dry-run` bookkeeping entry is not a measurement) are discarded, duplicate
configurations fold to their best score, and only the top N by score are written.
Imported files are named `seed-<slug>_<model>_<objective>_<context>.json` and carry
`"seed": true` plus the path they came from, so a prior can be traced.

Seven such priors ship with the repository: the best 30 configurations per
objective measured on an i9-14900KF / RTX 4090 / 31 GB / PCIe4x16 box, 228 KB in
total. They are real measurements of real runs, but of that machine. The tuner
loads a record only when the hardware fingerprint matches, so on any other box the
Tune tab lists them as "other hardware" and you have to tick **Use priors from
other hardware** before a sweep will use them. Nothing here makes a foreign
measurement silently authoritative.

`optuna_tune/results/` - the raw per-trial logs and CSVs - is not imported and is
not committed; the history file is the distilled form of it.


## Layout

```
serve/          the web app and its server
optuna_tune/    the Bayesian tuner
  history/      run data keyed to this machine; seed-* priors are committed
  results/      raw sweep logs and CSVs (not committed)
tools/          the build-option extractor, the priors importer and the build script
engine/         the engine binaries (not committed)
engine-src/     the HyperCore process boundary injected into a Strata clone
config/         run configs and the build manifest
prompts/        the agent profiles
data/ logs/ tmp/  local, gitignored
```

Nothing loose sits in the repository root.

## License

MIT. The Strata engine is copyright Niko1221 and contributors, MIT licensed.

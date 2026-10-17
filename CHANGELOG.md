# Changelog

## 0.1.0 - in development

The HyperCore web app (Chat, Assist, Monitor, About) running on a stock Strata
engine, plus two tabs the engine does not ship: Tune and Build.

- **Tabs** in the order Chat, Assist, Monitor, Tune, Build, About. Chat, Assist,
  Monitor and About are the existing views, unchanged in behaviour; anything
  HyperCore-only in the engine's INFO line is shown only when the engine reports it.
- **Attach mode** (`--engine-url`, `HYPERCORE_ENGINE_URL`, or an `"engine_url"` in
  a run config): the WebUI talks to an engine another server already loaded, over
  its OpenAI API, instead of starting a second copy of a tens-of-GB model.
  `serve/attached_engine.py` implements the same
  `generate(ids, max_new, sampling, cancel) -> token ids` boundary as the direct
  engine, so no tab knows the difference. It is now automatic: before starting an
  engine the server probes the port the run config names and attaches to whatever
  answers. `--no-attach` forces a local engine.
- **Run configs**: `config/run-example.json` is a template and is never picked by
  accident; `run.sh`/`run.bat` skip it and say so when asked for it. Every path an
  engine argument names is checked before the process is spawned, and an engine that
  exits is reported in its own words - a missing model shard is called a missing
  model shard, not a memory problem.
- **Build tab**: version check against the Niko1221/Strata releases, ready-made
  download with SHA-256 verification, or a from-source build whose dropdowns are
  extracted from that release's own `CMakeLists.txt` and `setup.py` and diffed
  against the previous manifest. One clone in `tmp/` at a time; it is deleted when
  the build finishes and nothing at runtime reads it.
- **Tune tab**: `optuna_tune/bayesian_tune.py` as a background subprocess, with the
  chat engine unloaded first, live trial rows read from the CLI's own stdout, the
  winner's benchmark, and export of the run config pair. The pin boxes are filled
  from the search space, so a value the tuner would reject cannot be typed.
- **Priors**: `tools/import_tune_history.py` imports measured configurations from
  another HyperCore install, trimmed to the best N per objective. Seven priors from an
  i9-14900KF / RTX 4090 box are committed (228 KB); the Tune tab lists what run data a
  sweep would load and whether it matches this machine, with boxes for "start cold",
  "use priors from other hardware" and "reuse priors from other objectives".
- **Windows**: `run.bat`, `setup.bat`, `tools/build_engine.bat`.
- **Tests**: `tools/run_tests.sh` runs the pytest suite and the browser-free JS
  checks that read `serve/web/app.js` as text.

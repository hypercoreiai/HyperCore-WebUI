# How this project is set up, and what a new install does

**Entry point.** `./setup.sh` (Linux) or `START-HERE.bat` (Windows) is the only thing a user
needs to run. It finds or creates `.venv`, installs the packages, gets or builds the engine,
writes `hypercore-<model>.json` and `run-<model>.sh`/`.bat`, and starts the server. A second
run does not reinstall; it just starts.

**Defaults written by setup (since 2026-10-11).** Every run config setup writes now carries:

- `"host": "0.0.0.0"` — the server answers to the other devices on the network
- `"api_key": "0000"` — a placeholder key, so a default install is not wide open
- `"turn_tail_check": true` — the end-of-turn completion check is on

`run-<model>.sh`/`.bat` passes `--turn-tail-check` accordingly. `--host`, `--api-key` and
`--no-turn-tail-check` override them; a model whose config already sets one keeps it. The
defaults are filled in only where the key is absent (`apply_server_defaults` in `setup.py`),
and a config written by an older setup gets them on its next start.

**Run configs are the source of truth for a model.** `hypercore-<model>.json` holds the engine
path, the engine args, the tokenizer, the log path, the port, the GPU selection, and the
server-side settings (`api_key`, `host`, `cors_origins`, `open_browser`, `reasoning_effort`,
`turn_tail_check`, `sampling.*`). Keys setup does not write are carried over when setup runs
again (`carry_over`), and the previous file is kept as `.bak`.

**Two config paths are checked before anything else:** `--config` on the command line, then
`hypercore-<model>.json` beside the server. `find_prompt_file` follows the same rule for the
agent profiles.

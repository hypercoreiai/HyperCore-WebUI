#!/usr/bin/env bash
#
# optuna_tune/max_tune.sh - entry point for the Bayesian (Optuna) sweep.
#
#   ./optuna_tune/max_tune.sh --model q2_0 --gen      --cntx 131072
#   ./optuna_tune/max_tune.sh --model q2_0 --prefill  --cntx 131072 --trials 60
#   ./optuna_tune/max_tune.sh --model q2_0 --latency  --mode staged --dry-run
#   ./optuna_tune/max_tune.sh --model q2_0 --balance  --cntx 262144 --cross-objective
#   ./optuna_tune/max_tune.sh --install                # put optuna into .venv, then exit
#
# The objective is one of four user-selected metrics:
#   --gen      maximise generation tokens/s
#   --prefill  maximise prompt tokens/s
#   --latency  minimise prompt_ms + decode_ms
#   --balance  maximise prefill tokens/s discounted by the share of the prompt time the
#              engine spent reading the pack (optuna_tune/prompt_transfer.py). The bus
#              share comes from the DONE line's counters 9-13 and 15 and the read rate the
#              engine measured at boot; with no telemetry it falls back to plain prefill.
#   --cross-objective seeds the study with configurations already measured on this box
#              under a different objective, re-scored for the one running.
#
# The dimensions searched are the columns every row is written with:
#   layer  vram  pcie  ple-io  kv  kvgrow  hostcore  affinity  worker  tail  draft-floor
#   prefill-cap  status
#
# Hold a dimension constant instead of searching it:
#   --pcie --ple-io --kv --kvgrow --hostcore --affinity --worker --tail --draft-floor
#   --prefill-cap auto:16384   the prompt chunk; the engine caps plain auto at 8192 unless
#                          auto:N is given (src/program/generate.cpp:1782-1790)
# --draft-floor is --spec-min-p on the engine, searched in [0.001, 1] unless pinned.
# --prompt-cache-tail is kept in the deliverable: this engine accepts it.
#
# --trials is unseen configurations: a repeat of a point already in history is redrawn
# and does not count. --sampler gp needs torch (Optuna 5); without it the sweep falls
# back to TPE.
#
# Deliverables, at the repository root, in the project's own run-config format:
#   max_run_<model>.sh    the startup script: serve/server.py with the winning config
#   max_run_<model>.json  that run config, with the winning engine arguments in "args"
#   optuna_tune/results/max_run_<model>.record.json   every trial and its metrics
#   optuna_tune/results/bayesian-<machine>-<model>-<objective>-<stamp>.csv
#
# Run data is kept per machine in optuna_tune/history/, named
#   <CPU>_<GPU>_<RAM>_<PCIe>x<width>_<model>_<objective>_<cntx>.json
# so a sweep on the same box reuses what it already measured instead of booting again,
# and keeps drawing until --trials new points have been evaluated.
#   --no-load                    start the study cold
#   --allow-hardware-mismatch   load run data from a different machine too
#   --hardware '<json>'         pin the fingerprint instead of probing the host
#   --replay <record.json>      rewrite the deliverables from a finished sweep, no engine boot
#   --from-records              rebuild the deliverables from the best measured trial in the
#                             CSVs under optuna_tune/results/, no engine boot
#   --benchmark                 skip the search and benchmark the winner configuration that is
#                             already written (max_run_<model>.json). Same shape as
#                             ~/Strata/setup.sh --setup: one command, it checks the card, boots
#                             the config, and prints medians over --repeat boots including the
#                             off-card share of the decode's routed experts. It then walks a
#                             prompt ladder (512, 1024, 2048, 4096, 8192, 16384 tokens, clipped
#                             to the winner's --max-context) with the same arguments, so the
#                             report shows how prefill and the off-card share move with prompt
#                             size. --prompt-ladder 512,2048 changes the sizes, --no-ladder
#                             benches the sweep prompt only.
#
# Every evaluated configuration prints its off-card percentage on the terminal line. The engine's
# #588 [offloaded] counter is only written by the CPU-side routing loop, so on a box running the
# device-decided hit path the percentage is taken from the decode cache misses (lookups - hits),
# the same number the engine prints beside its hit rate.
# The sweep ends by re-running the winner through optuna_tune/winner_bench.py
# (--no-benchmark to skip it, --bench-repeat N for more boots).
#
# The Python side runs under the project venv: setup.sh/install.sh create .venv and install
# requirements.txt into it. If .venv is missing the system python3 is used and the script
# says so. optuna is not in requirements.txt because the sweep is optional; --install adds it.
#
# Requires: bash 4.4+, a free GPU (./restart.sh stop first) for a real run.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${HYPERCORE_ROOT:-$(dirname "$HERE")}"
VENV="$ROOT/.venv"

pick_python() {
    if [ -n "${HC_PYTHON:-}" ] && [ -x "$HC_PYTHON" ]; then
        printf '%s\n' "$HC_PYTHON"
        return 0
    fi
    for p in "$VENV/bin/python" "$VENV/bin/python3" \
            "$ROOT/venv/bin/python" "$(command -v python3 2>/dev/null || true)"; do
        [ -n "$p" ] && [ -x "$p" ] || continue
        printf '%s\n' "$p"
        return 0
    done
    return 1
}

PY="$(pick_python)" || {
    echo "max_tune.sh: no python interpreter found. Create the venv: python3 -m venv .venv" >&2
    exit 1
}

if [ "${1:-}" = "--install" ]; then
    echo "[*] installing optuna into $PY"
    "$PY" -m pip install --quiet optuna || { echo "pip install optuna failed" >&2; exit 1; }
    "$PY" -c "import optuna; print('optuna', optuna.__version__)"
    exit 0
fi

is_bench=0
for a in "$@"; do [ "$a" = "--benchmark" ] && is_bench=1; done
if [ "$is_bench" = 1 ]; then
    # The benchmark needs no optuna and no study: it runs the winner config that is on disk.
    exec "$PY" "$HERE/winner_bench.py" "$@"
fi

if ! "$PY" -c "import optuna" >/dev/null 2>&1; then
    echo "max_tune.sh: $PY cannot import optuna. Install it:" >&2
    echo "    $PY -m pip install optuna" >&2
    echo "  or run: ./optuna_tune/max_tune.sh --install" >&2
    exit 1
fi

engine_override=0
for a in "$@"; do [ "$a" = "--engine" ] && engine_override=1; done
if [ "$engine_override" = 0 ] && [ ! -x "$ROOT/engine/hypercore" ]; then
    echo "max_tune.sh: no engine at $ROOT/engine/hypercore. Build it (./install.sh) or pass --engine." >&2
    exit 2
fi

free="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1)"
is_dry=0
for a in "$@"; do [ "$a" = "--dry-run" ] && is_dry=1; done
is_help=0
for a in "$@"; do [ "$a" = "--help" ] || [ "$a" = "-h" ] && is_help=1; done
# A replay or a rebuild from stored records never boots the engine, so it needs no card.
is_offline=0
for a in "$@"; do [ "$a" = "--replay" ] || [ "$a" = "--from-records" ] && is_offline=1; done
if [ "$is_dry" = 0 ] && [ "$is_help" = 0 ] && [ "$is_offline" = 0 ] && [ "${free:-0}" -lt "${GPU_MIN_FREE:-12000}" ]; then
    echo "max_tune.sh: the GPU has ${free:-0} MiB free. Stop the running server first: ./restart.sh stop" >&2
    exit 3
fi

echo "[*] using $PY ($( "$PY" -V 2>&1 ))"
exec "$PY" "$HERE/bayesian_tune.py" "$@"

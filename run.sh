#!/bin/sh
# Start the HyperCore WebUI.
#
#   ./run.sh                  newest run config in config/, else engine/strata directly
#   ./run.sh q2_0           config/run-q2_0.json, config/q2_0.json, or any name
#   ./run.sh config/x.json  a path, verbatim
#   ./run.sh --no-attach    flags after the name are passed to the server as they are
#   ./run.sh --ui-only      the app without an engine: usable while the GPU is busy
#
# A fresh clone has nothing in it yet, and that is not a reason to refuse to start:
# the Build tab is what produces the first engine and the first run config, so when
# there is neither run.sh brings the app up with no engine and says so.
#
# One engine per machine. Two things follow from that:
#
#   * if a server is already listening on the port, this opens that page rather than
#     starting a second copy of the model;
#   * if an engine is already loaded somewhere else, point at it with
#     HYPERCORE_ENGINE_URL=http://127.0.0.1:8080 and the WebUI attaches to it over
#     its API instead of starting its own.
#
# The engine mode follows what is in engine/: the HyperCore worker when it was built
# with the engine, otherwise the engine's own --serve protocol.
cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

CFG_DIR="config"
HOST="${HYPERCORE_HOST:-0.0.0.0}"
PORT="${HYPERCORE_PORT:-8081}"
URL="http://${HYPERCORE_ENGINE_HOST:-127.0.0.1}:${PORT}"

list_configs() {
  ls -1 "$CFG_DIR"/*.json 2>/dev/null | grep -v build_options.json | sed 's/^/  /'
}

# A config with the template's placeholder paths in it is not a run config: it is
# the shape of one. Never pick it by accident, and say so when asked for it.
is_template() { grep -q "ABSOLUTE/PATH/TO" "$1" 2>/dev/null; }

pick_config() {
  if [ -n "$1" ]; then
    for c in "$1" "$CFG_DIR/$1" "$CFG_DIR/run-$1" "$CFG_DIR/$1.json" "$CFG_DIR/run-$1.json"; do
      if [ -f "$c" ]; then printf '%s\n' "$c"; return 0; fi
    done
    # The name matches nothing. That is usually a mistyped version or a stray argument
    # (run.sh 8081), not a machine with nothing built on it: taking the newest run config
    # keeps the engine running rather than starting the app engineless and reporting
    # "no engine was asked for" for a model that is sitting in engine/.
    for c in $(ls -t "$CFG_DIR"/run-*.json 2>/dev/null | grep -v 'run-example\.json'); do
      if ! is_template "$c"; then
        echo "no config named '$1' in $CFG_DIR/ - using the newest, $c" >&2
        printf '%s\n' "$c"; return 0
      fi
    done
    return 1
  fi
  for c in $(ls -t "$CFG_DIR"/run-*.json 2>/dev/null | grep -v 'run-example\.json'); do
    if ! is_template "$c"; then printf '%s\n' "$c"; return 0; fi
  done
  c=$(ls -t "$CFG_DIR"/*.json 2>/dev/null | grep -v build_options.json | grep -v 'run-example\.json' | head -1)
  [ -n "$c" ] && printf '%s\n' "$c"
  return 0
}

# Is the WebUI already up on this port? (A port in use is not necessarily ours.)
already_running() {
  "$PY" tools/is_running.py "$PORT"
}

open_browser() {
  for opener in xdg-open open start; do
    if command -v "$opener" >/dev/null 2>&1; then "$opener" "$1" >/dev/null 2>&1; return 0; fi
  done
  return 1
}

if already_running; then
  echo "the WebUI is already running at $URL - opening it instead of starting a second engine."
  open_browser "$URL" || echo "open $URL in your browser."
  exit 0
fi

NAME=""
EXTRA=""
EXPLICIT=""      # --ui-only asked for on the command line
for arg in "$@"; do
  case "$arg" in
    -*) EXTRA="$EXTRA $arg" ;;
    *) [ -z "$NAME" ] && NAME="$arg" ;;
  esac
  case " $arg " in " --ui-only "*) EXPLICIT="--ui-only" ;; esac
done
ENGINELESS=""    # serve the page with no engine, so do not name one

CFG=$(pick_config "$NAME")
if [ -z "$CFG" ]; then
  if [ -f engine/strata ] || [ -n "$HYPERCORE_ENGINE_URL" ]; then
    echo "no run config in $CFG_DIR/ - starting engine/strata; set the model paths in the Config view."
  else
    # Nothing installed yet, which is the state a fresh clone is in. The page has to
    # come up anyway: a build is what installs engine/engine_<quant>/ and
    # config/run-<quant>.json, and the Model dropdown at the top of the page lists a
    # version from then on. Refusing to start here is what stopped the Build tab.
    echo "nothing installed yet: no run config in $CFG_DIR/ and no engine in engine/."
    echo "starting the app without an engine so the Build tab is reachable."
    echo "The first build installs the engine and its run config under this root; set the"
    echo "model paths in the Config view, then the Model dropdown lists the build."
    list_configs
    ENGINELESS="--ui-only"
    [ -z "$EXPLICIT" ] && EXTRA="$EXTRA --ui-only"
  fi
elif is_template "$CFG"; then
  echo "warning: $CFG is the template - its paths are placeholders."
  echo "Fill them in, or run ./run.sh <name> for a config that has real paths."
fi

MODE=strata
[ -f engine/hypercore-worker ] && MODE=hypercore
[ -n "$HYPERCORE_ENGINE_URL" ] && MODE=attach

ARGS="--host $HOST --port $PORT --api-key ${HYPERCORE_API_KEY:-0000} --open"
# Engineless means there is no engine to name: --ui-only is the mode, so --engine is
# left out rather than pointed at a binary that is not there.
[ -z "$ENGINELESS" ] && ARGS="$ARGS --engine $MODE"
[ -n "$CFG" ] && ARGS="$ARGS --config $CFG"
[ -n "$HYPERCORE_ENGINE_URL" ] && ARGS="$ARGS --engine-url $HYPERCORE_ENGINE_URL"
[ -n "$EXTRA" ] && ARGS="$ARGS $EXTRA"

if [ "$MODE" = "attach" ]; then
  echo "engine: attached to $HYPERCORE_ENGINE_URL   http://$HOST:$PORT"
elif [ -n "$ENGINELESS" ]; then
  echo "engine: none, the app only   http://$HOST:$PORT"
elif [ -n "$CFG" ]; then
  echo "config: $CFG   engine mode: $MODE   http://$HOST:$PORT"
else
  echo "engine: engine/strata   engine mode: $MODE   http://$HOST:$PORT"
fi

exec "$PY" serve/server.py $ARGS

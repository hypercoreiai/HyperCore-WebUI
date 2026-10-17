#!/bin/sh
# Start the HyperCore WebUI.
#
#   ./run.sh                  newest run config in config/, else engine/strata directly
#   ./run.sh q2_0           config/run-q2_0.json, config/q2_0.json, or any name
#   ./run.sh config/x.json  a path, verbatim
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

pick_config() {
  if [ -n "$1" ]; then
    for c in "$1" "$CFG_DIR/$1" "$CFG_DIR/run-$1" "$CFG_DIR/$1.json" "$CFG_DIR/run-$1.json"; do
      if [ -f "$c" ]; then printf '%s\n' "$c"; return 0; fi
    done
    return 1
  fi
  c=$(ls -t "$CFG_DIR"/run-*.json 2>/dev/null | head -1)
  [ -z "$c" ] && c=$(ls -t "$CFG_DIR"/*.json 2>/dev/null | grep -v build_options.json | head -1)
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

CFG=$(pick_config "${1:-}")
if [ -z "$CFG" ]; then
  if [ -f engine/strata ] || [ -n "$HYPERCORE_ENGINE_URL" ]; then
    echo "no run config in $CFG_DIR/ - starting engine/strata; set the model paths in the Config view."
  else
    echo "no run config in $CFG_DIR/ and no engine in engine/."
    echo "Build one from the Build tab, or put a run config in $CFG_DIR/:"
    list_configs
    exit 1
  fi
fi

MODE=strata
[ -f engine/hypercore-worker ] && MODE=hypercore
[ -n "$HYPERCORE_ENGINE_URL" ] && MODE=attach

ARGS="--engine $MODE --host $HOST --port $PORT --api-key ${HYPERCORE_API_KEY:-0000} --open"
[ -n "$CFG" ] && ARGS="$ARGS --config $CFG"
[ -n "$HYPERCORE_ENGINE_URL" ] && ARGS="$ARGS --engine-url $HYPERCORE_ENGINE_URL"

if [ "$MODE" = "attach" ]; then
  echo "engine: attached to $HYPERCORE_ENGINE_URL   http://$HOST:$PORT"
elif [ -n "$CFG" ]; then
  echo "config: $CFG   engine mode: $MODE   http://$HOST:$PORT"
else
  echo "engine: engine/strata   engine mode: $MODE   http://$HOST:$PORT"
fi

exec "$PY" serve/server.py $ARGS

#!/bin/sh
# Start the HyperCore WebUI against the engine in engine/.
cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
exec "$PY" serve/server.py --engine hypercore --config "config/run-${1:-q2_0}.json" \
  --host "${HYPERCORE_HOST:-0.0.0.0}" --port "${HYPERCORE_PORT:-8081}" \
  --api-key "${HYPERCORE_API_KEY:-0000}" --open

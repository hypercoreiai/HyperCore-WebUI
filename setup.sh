#!/bin/sh
# Create the environment, then find or fetch an engine. No model download logic:
# the model paths are yours to set in the Config view or in config/run-<model>.json.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv || { echo "could not create .venv: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/python -m pip install -r requirements.txt
if [ ! -f engine/strata ]; then
  echo "no engine in engine/. Open the Build tab to fetch or compile one, or set STRATA_ENGINE."
fi
echo "ready. start with ./run.sh"

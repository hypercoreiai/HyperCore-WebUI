#!/usr/bin/env bash
# Clone a Strata release, build the engine, the vision engine and the worker,
# copy them into engine/, and leave nothing behind in tmp/.
#
#   ./tools/build_engine.sh v0.1.45
#
# The Build tab calls the same steps through serve/build_api.py; this is the
# command-line path for people who do not want to open the web app.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-main}"
shift || true            # the rest of the line is cmake -D flags for the build
CLONE="$ROOT/tmp/strata-$TAG"

rm -rf "$CLONE"
mkdir -p "$ROOT/tmp"
git clone --depth 1 --branch "$TAG" https://github.com/Niko1221/Strata.git "$CLONE"

# Inject the HyperCore process boundary so the worker targets exist.
cp -r "$ROOT/engine-src" "$CLONE/hypercore-interface"

python3 "$ROOT/tools/extract_build_options.py" "$CLONE" \
  --out "$ROOT/config/build_options.json" --diff "$ROOT/config/build_options.json"

cmake -G Ninja -S "$CLONE" -B "$CLONE/build" "$@"
cmake --build "$CLONE/build" --target strata strata-vision hypercore-worker -j "$(nproc)"

mkdir -p "$ROOT/engine"
for bin in strata strata-vision hypercore-worker; do
  if [ -f "$CLONE/build/$bin" ]; then cp "$CLONE/build/$bin" "$ROOT/engine/$bin"; fi
done
rm -rf "$CLONE"
echo "built $TAG into engine/; tmp/ is empty"

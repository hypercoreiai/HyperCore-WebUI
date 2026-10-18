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

# The HyperCore interface is ours, the engine is upstream's. If the worker target is
# what fails against this Strata revision, the engine and the vision engine are still
# buildable and the server drives the engine over its stdin/stdout protocol instead.
WORKER=1
if ! cmake --build "$CLONE/build" --target strata strata-vision hypercore-worker -j "$(nproc)"; then
  echo "the hypercore-worker target failed; building strata and strata-vision only"
  cmake --build "$CLONE/build" --target strata strata-vision -j "$(nproc)"
  WORKER=0
fi

mkdir -p "$ROOT/engine"
for bin in strata strata-vision hypercore-worker; do
  if [ -f "$CLONE/build/$bin" ]; then cp "$CLONE/build/$bin" "$ROOT/engine/$bin"; fi
done

# Record what was built so the Monitor can say "worker: not built" instead of
# guessing from the files in engine/.
python3 - "$CLONE" "$TAG" "$WORKER" <<'PY'
import datetime, hashlib, json, sys
from pathlib import Path
clone, tag, worker = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
meta = {"source": "built-from-source", "version": tag, "tag": tag, "worker": worker,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "src": hashlib.sha256(Path(clone, "CMakeLists.txt").read_bytes()).hexdigest()[:16]}
(Path("engine") / "BUILD.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
PY

rm -rf "$CLONE"
echo "built $TAG into engine/ (worker: $WORKER); tmp/ is empty"

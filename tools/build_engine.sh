#!/usr/bin/env bash
# Clone a Strata release, build the engine, the vision engine and the worker,
# and install them - the same way the Build tab does.
#
#   ./tools/build_engine.sh v0.1.45                       # the default engine
#   ./tools/build_engine.sh v0.1.45 --quant q2_0         # a model version
#   ./tools/build_engine.sh v0.1.45 --quant q2_0 -DSTRATA_ENABLE_CUDA=ON
#
# With --quant the binaries go in engine/engine_<quant>/, the build's runtime data
# is copied next to them, and config/run-<quant>.json is written or repaired so the
# Model version dropdown in the web app can start it. Without it they are simply
# the engine in engine/. Nothing is written outside this repository.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-main}"
shift || true
QUANT=""
FLAGS=()
while [ $# -gt 0 ]; do
  arg="$1"
  shift
  case "$arg" in
    --quant)      QUANT="${1:-}"; shift ;;
    --quant=*)    QUANT="${arg#--quant=}" ;;
    *)            FLAGS+=("$arg") ;;
  esac
done

CLONE="$ROOT/tmp/strata-$TAG"

rm -rf "$CLONE"
mkdir -p "$ROOT/tmp"
git clone --depth 1 --branch "$TAG" https://github.com/Niko1221/Strata.git "$CLONE"

# Inject the HyperCore process boundary so the worker targets exist.
cp -r "$ROOT/engine-src" "$CLONE/hypercore-interface"

python3 "$ROOT/tools/extract_build_options.py" "$CLONE" \
  --out "$ROOT/config/build_options.json" --diff "$ROOT/config/build_options.json"

cmake -G Ninja -S "$CLONE" -B "$CLONE/build" "${FLAGS[@]}"

# The HyperCore interface is ours, the engine is upstream's. If the worker target is
# what fails against this Strata revision, the engine and the vision engine are still
# buildable and the server drives the engine over its stdin/stdout protocol instead.
WORKER=1
if ! cmake --build "$CLONE/build" --target strata strata-vision hypercore-worker -j "$(nproc)"; then
  echo "the hypercore-worker target failed; building strata and strata-vision only"
  cmake --build "$CLONE/build" --target strata strata-vision -j "$(nproc)"
  WORKER=0
fi

if [ -n "$QUANT" ]; then
  # Hand it to the same store the Build tab uses: engine/engine_<quant>/, the
  # runtime data beside the binaries, and the run config that starts it.
  cd "$ROOT"
  python3 - "$QUANT" "$TAG" "$CLONE" "$WORKER" <<'PY'
import datetime, hashlib, json, sys
from pathlib import Path

from serve import quant_store

quant, tag, clone, worker = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4] == "1"
build = clone / "build"
sources = [p for place in (build, build / "hypercore-interface") for p in place.iterdir()
           if p.is_file() and quant_store.is_artifact(p.name)]
if not sources:
    raise SystemExit("the build produced no engine binary")
meta = {"source": "built-from-source", "version": tag, "tag": tag, "worker": worker,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "src": hashlib.sha256((clone / "CMakeLists.txt").read_bytes()).hexdigest()[:16]}
entry = quant_store.store_binaries(quant, sources, meta)
cfg, note = quant_store.ensure_run_config(quant, source_root=clone)
refreshed = quant_store.refresh_run_configs(quant, clone)
chosen = quant_store.select(quant)
print(f"stored engine/{quant_store.store_dir(quant).name}: {', '.join(sorted(entry['files']))}")
print(f"run config: {note}")
for line in refreshed:
    print(f"run config: {line}")
print(f"active engine: {chosen['note']}")
PY
  rm -rf "$CLONE"
  echo "built $TAG for quant $QUANT into engine/engine-$QUANT/; tmp/ is empty"
  exit 0
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

"""tests/test_worker_config_keys.py - the two sides of the worker config must name the same keys.

The server writes the worker's config as JSON (serve/server.py, hypercore_worker_config).
hypercore-worker reads it with the small hand-written reader in
engine-src/interface/strata_bridge.cpp, and that reader only knows the key names it was
written to know: a name it does not look up is not an error, it returns nothing, and the
worker carries on with an empty value. The numeric knobs are deliberately ignored - the engine
gets them as arguments - but the two that name the model are not, and a reader that misses
one starts the engine with no model at all.

That is why a mismatch here does not read as a config error. The engine reports its own
argument check instead of a missing path, and the first line it prints is the memory guard, so
the failure looks like an out-of-memory problem:

    strata generate: memory guard: limit 30.5 GiB ...
    strata generate: --ple-gguf is required; --no-ple explicitly enables a diagnostic ablation

What happened here: the bridge read engine_exe with exe as an alias, but read only "args",
while the server writes engine_args. A run config still worked when passed as a file; the
worker only fails when the server hands it the same config inline.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "engine-src" / "interface" / "strata_bridge.cpp"

sys.path.insert(0, str(ROOT))
from serve.server import hypercore_worker_config

# The keys that name the model. Both spellings have to work: a run config written by
# setup.py uses exe/args, the server's worker config uses engine_exe/engine_args.
MODEL_KEYS = ({"exe", "engine_exe"}, {"args", "engine_args"})


def reader_keys(source: str) -> set[str]:
    """Every key name the worker's JSON reader is asked to look up."""
    keys = set()
    for call in re.findall(r'json_(?:string|array|number)[a-z_]*\(([^;]*)\)', source):
        keys.update(re.findall(r'"([a-z_]+)"', call))
    return keys


def cfg() -> dict:
    return {
        "exe": "/tmp/strata",
        "cwd": "/tmp",
        "args": ["--pack", "pack/full", "--ple-gguf", "ple.gguf", "--max-context", "4096"],
        "parallel": 4,
        "pool_workers": 15,
    }


class WorkerConfigKeys(unittest.TestCase):
    def test_the_reader_accepts_both_spellings_of_the_model_keys(self):
        keys = reader_keys(BRIDGE.read_text())
        for pair in MODEL_KEYS:
            missing = sorted(pair - keys)
            read = sorted(keys & pair)
            self.assertEqual(missing, [],
                             f"the worker reads only {read}; it does not look up {missing}")

    def test_the_engine_args_survive_the_config(self):
        # The round trip, not just the key names: what the engine is handed must be what
        # the run config said.
        got = json.loads(hypercore_worker_config(cfg()))
        self.assertEqual(got["engine_args"], cfg()["args"])
        self.assertEqual(got["engine_exe"], "/tmp/strata")


if __name__ == "__main__":
    unittest.main()

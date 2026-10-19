#!/usr/bin/env python3
"""tests/test_import_tune_history.py - the priors importer.

~/black_box/optuna_tune/history is 2.1 MB of per-trial metrics for one machine. What
ships in the repo is the best few configurations from it, in the same schema, so a
first sweep is not cold and the repository is not a data dump. These tests check the
trimming is honest: DRY rows are dropped, duplicates fold to the better score, the
order is by score, and running the importer twice does not double-count.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "optuna_tune"))

from history import SCHEMA  # noqa: E402
from import_tune_history import build_seed, import_history, seed_trials  # noqa: E402

CFG = {"vram": 1024, "pcie": 1.0, "ple_io": "ram", "kv": "k8v4", "kvgrow": "1",
       "host_core": "last", "affinity": "auto", "worker": 0, "tail": "0",
       "draft_floor": 0.5, "prefill_cap": "auto:16384"}


def trial(**over):
    cfg = dict(CFG)
    cfg.update(over.pop("cfg", {}))
    return {"cfg": cfg, "score": over.get("score", 0.0),
            "status": over.get("status", "PASS"), "layer": "vram",
            "metrics": {"gen_tok_s": over.get("score", 0.0)},
            "max_new": 512, "spec_min_p": 0.5, "measured": 1}


def record(trials):
    return {"schema": SCHEMA, "model": "q2_0", "objective": "gen", "context": 131072,
            "hardware": {"cpu": "Test-CPU", "gpu": ("Test-GPU",), "ram_gib": 8,
                        "pci_width": 16, "pci_gen": 4},
            "hardware_slug": "Test-CPU_Test-GPU_8GB_PCIe4x16",
            "runs": [{"generated_utc": "2026-01-01T00:00:00+00:00", "trials": len(trials)}],
            "trials": trials, "updated_utc": "2026-01-01T00:00:00+00:00"}


class SeedTrials(unittest.TestCase):
    def test_dry_rows_are_not_measurements(self):
        rec = record([trial(score=500.0), trial(score=0.0, status="DRY")])
        self.assertEqual([t["score"] for t in seed_trials(rec, 10)], [500.0])

    def test_a_repeated_configuration_folds_to_its_best_score(self):
        rec = record([trial(score=100.0), trial(score=140.0), trial(score=90.0)])
        kept = seed_trials(rec, 10)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["score"], 140.0)
        self.assertEqual(kept[0]["measured"], 3)

    def test_top_n_is_the_best_n_in_score_order(self):
        rec = record([trial(score=float(i), cfg={"vram": i * 8}) for i in range(1, 61)])
        kept = seed_trials(rec, 5)
        self.assertEqual([t["score"] for t in kept], [60.0, 59.0, 58.0, 57.0, 56.0])

    def test_top_zero_keeps_everything(self):
        rec = record([trial(score=float(i), cfg={"vram": i * 8}) for i in range(1, 21)])
        self.assertEqual(len(seed_trials(rec, 0)), 20)


class SeedFile(unittest.TestCase):
    def test_provenance_and_shape_survive_the_trim(self):
        rec = record([trial(score=float(i), cfg={"vram": i * 8}) for i in range(1, 41)])
        seed = build_seed(rec, Path("/elsewhere/history/x.json"), 10)
        self.assertEqual(seed["schema"], SCHEMA)
        self.assertTrue(seed["seed"])
        self.assertEqual(seed["seed_source"], "/elsewhere/history/x.json")
        self.assertEqual(seed["trials_available"], 40)
        self.assertEqual(seed["trials_shipped"], 10)
        self.assertEqual(seed["runs"], [])
        self.assertEqual(seed["model"], "q2_0")
        self.assertEqual(seed["hardware_slug"], "Test-CPU_Test-GPU_8GB_PCIe4x16")

    def test_a_missing_source_is_not_an_error_free_pass(self):
        self.assertEqual(0, import_history(Path("does/not/exist"), Path("/tmp/x"), 10, False, True))


class ImportRun(unittest.TestCase):
    def setUp(self):
        self.tmp = ROOT / "logs" / "import-test"
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)
        self.name = "Test-CPU_Test-GPU_8GB_PCIe4x16_q2_0_gen_131072.json"
        (self.src / self.name).write_text(json.dumps(record(
            [trial(score=float(i), cfg={"vram": i * 8}) for i in range(1, 41)])), encoding="utf-8")

    def test_import_writes_a_seed_file_named_for_the_loader_glob(self):
        n = import_history(self.src, self.dst, 10, False, False)
        self.assertEqual(n, 1)
        out = self.dst / f"seed-{self.name}"
        self.assertTrue(out.is_file())
        # the tuner globs "*_<model>_<objective>_<context>.json"; the seed- prefix
        # must not break that match.
        needle = "_q2_0_gen_131072.json"
        self.assertTrue(out.name.endswith(needle))

    def test_importing_twice_does_not_double_count_a_measurement(self):
        import_history(self.src, self.dst, 10, False, False)
        import_history(self.src, self.dst, 10, False, False)
        rec = json.loads((self.dst / f"seed-{self.name}").read_text(encoding="utf-8"))
        self.assertEqual(len(rec["trials"]), 10)
        self.assertEqual(rec["trials"][0]["measured"], 2)

    def test_force_replaces_instead_of_merging(self):
        import_history(self.src, self.dst, 10, False, False)
        import_history(self.src, self.dst, 3, True, False)
        rec = json.loads((self.dst / f"seed-{self.name}").read_text(encoding="utf-8"))
        self.assertEqual(len(rec["trials"]), 3)
        self.assertEqual(rec["trials"][0]["measured"], 1)

    def tearDown(self):
        for path in self.tmp.rglob("*"):
            if path.is_file():
                path.unlink()
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
        self.tmp.rmdir()


class ShippedPriors(unittest.TestCase):
    """The seed files in the repository must be loadable by the tuner, not just JSON."""

    def test_shipped_seeds_are_valid_history_records(self):
        files = sorted((ROOT / "optuna_tune" / "history").glob("seed-*.json"))
        self.assertTrue(files, "no priors were imported")
        for path in files:
            rec = json.loads(path.read_text(encoding="utf-8-sig"))
            self.assertEqual(rec["schema"], SCHEMA, path.name)
            self.assertIsInstance(rec["trials"], list, path.name)
            self.assertTrue(rec["trials"], path.name)
            self.assertTrue(all(t.get("cfg") for t in rec["trials"]), path.name)
            self.assertLessEqual(len(rec["trials"]), 50,
                                f"{path.name} ships more than a seed should")


if __name__ == "__main__":
    unittest.main()

"""tests/test_quant_store.py - the per-quant engine store behind the Model version dropdown.

The tests point serve/quant_store.py at a temporary engine/ and config/ so nothing
here touches the real binaries. Run with:

    python3 -m pytest tests/test_quant_store.py -q
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from serve import quant_store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = self.root / "engine"
        self.config = self.root / "config"
        self.engine.mkdir()
        self.config.mkdir()
        self._saved = (quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR,
                       quant_store.INDEX_PATH, quant_store.CONFIG_DIR)
        quant_store.ENGINE_DIR = self.engine
        quant_store.VERSIONS_DIR = self.engine / "versions"
        quant_store.INDEX_PATH = self.engine / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.config

    def tearDown(self) -> None:
        quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR, quant_store.INDEX_PATH, \
            quant_store.CONFIG_DIR = self._saved
        self.tmp.cleanup()

    # ---- names ---------------------------------------------------------------
    def test_quant_names_are_normalised(self) -> None:
        for raw, want in (("Q2_0", "q2_0"), ("IQ2_S", "iq2_s"), ("Q4-K-M", "q4_k_m"),
                          (" q3_K_S ", "q3_k_s"), ("Q5_1", "q5_1")):
            self.assertEqual(quant_store.normalise_quant(raw), want)

    def test_a_name_that_is_not_a_quant_is_refused(self) -> None:
        for bad in ("", "  ", "2_0", "q2 0!", "%q2_0", "../q2_0", None, "q2_0%"):
            with self.assertRaises(ValueError):
                quant_store.normalise_quant(bad)
        # an underscore typo is a name, not an error: it is folded, not refused
        self.assertEqual(quant_store.normalise_quant("q2__0"), "q2_0")

    def test_the_quant_of_a_run_config_is_read_from_its_paths(self) -> None:
        cfg = {"model_name": "qwen3.8-flash-next-q2_0",
              "args": ["--pack", "/home/x/Strata-data/packs/q2_0",
                       "--native", "/home/x/models/Q2_0/model-Q2_0.gguf"]}
        self.assertEqual(quant_store.quant_from_config(cfg), "q2_0")
        self.assertEqual(quant_store.quant_from_config({"args": ["--model", "m-IQ2_S.gguf"]}), "iq2_s")
        self.assertIsNone(quant_store.quant_from_config({"model_name": "no-quant-here"}))

    # ---- storing a build ---------------------------------------------------
    def test_a_build_is_stored_under_its_quant(self) -> None:
        built = self.root / "build"
        built.mkdir()
        for name in ("strata", "strata-vision", "hypercore-worker"):
            (built / name).write_bytes(f"binary {name}".encode())
        entry = quant_store.store_binaries("IQ2_S", [built / n for n in ("strata", "strata-vision", "hypercore-worker")],
                                          {"version": "0.1.45", "source": "built-from-source",
                                           "options": {"STRATA_NATIVE_EXPERTS": "ON"}})
        self.assertEqual(entry["key"], "iq2_s")
        self.assertTrue((self.engine / "versions" / "iq2_s" / "strata").is_file())
        self.assertTrue((self.engine / "versions" / "iq2_s" / "BUILD.json").is_file())
        self.assertEqual(len(json.loads((self.engine / "VERSIONS.json").read_text())["versions"]), 1)
        self.assertEqual(sorted(entry["files"]), sorted(quant_store.BINARIES))
        self.assertTrue(entry["files"]["strata"]["sha256"])

    def test_a_binary_that_was_not_built_is_skipped_not_failed(self) -> None:
        built = self.root / "build"
        built.mkdir()
        (built / "strata").write_bytes(b"engine")
        entry = quant_store.store_binaries("q2_0", [built / "strata", built / "hypercore-worker"],
                                          {"version": "0.1.45"})
        self.assertEqual(list(entry["files"]), [quant_store.EXE])
        self.assertFalse(entry["worker"])

    # ---- choosing one ------------------------------------------------------
    def test_selecting_places_the_binaries_where_the_run_configs_look(self) -> None:
        self._store("q2_0", b"q2 engine", worker=False)
        self._store("iq2_s", b"iq engine", worker=True)
        entry = quant_store.select("IQ2_S")
        self.assertTrue((self.engine / "strata").is_file())
        self.assertEqual((self.engine / "strata").read_bytes(), b"iq engine")
        self.assertTrue(quant_store.index()["active"] == "iq2_s")
        self.assertTrue(entry["active"])

    def test_selecting_a_quant_that_was_never_built_says_so(self) -> None:
        self._store("q2_0", b"q2 engine")
        with self.assertRaises(ValueError) as exc:
            quant_store.select("iq4_s")
        self.assertIn("no model version iq4_s is available", str(exc.exception))

    def test_a_run_config_with_no_build_of_its_own_can_still_be_selected(self) -> None:
        (self.engine / "strata").write_bytes(b"engine")
        (self.config / "run-q4_k_m.json").write_text(json.dumps(
            {"model_name": "qwen3.8-flash-next-q4_k_m", "exe": "engine/strata",
             "args": ["--pack", "/data/packs/q4_k_m"]}), encoding="utf-8")
        entry = quant_store.select("q4_k_m")
        self.assertTrue(entry["active"])
        self.assertEqual(entry["note"], "uses the engine already in engine/")
        self.assertEqual(quant_store.index()["active"], "q4_k_m")
        # nothing was built for it, so the index still holds only real engine builds
        self.assertEqual(list(quant_store.index()["versions"]), [])

    def test_the_active_build_is_the_one_engine_strata_is(self) -> None:
        self._store("q2_0", b"q2 engine")
        quant_store.select("q2_0")
        # the index was deleted: rebuilding it from disk finds the running engine
        quant_store.INDEX_PATH.unlink()
        data = quant_store.index()
        self.assertEqual(data["active"], "q2_0")

    # ---- what the UI sees -----------------------------------------------
    def test_a_run_config_with_no_build_of_its_own_is_still_listed(self) -> None:
        (self.engine / "strata").write_bytes(b"engine")
        (self.config / "run-q4_k_m.json").write_text(json.dumps(
            {"model_name": "qwen3.8-flash-next-q4_k_m", "exe": "engine/strata",
             "args": ["--pack", "/data/packs/q4_k_m"]}), encoding="utf-8")
        versions = quant_store.list_versions()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["quant"], "q4_k_m")
        self.assertTrue(versions[0]["ready"])
        self.assertEqual(versions[0]["config_name"], "run-q4_k_m.json")

    def test_build_options_json_is_never_a_model_version(self) -> None:
        (self.config / "build_options.json").write_text(json.dumps({"version": "0.1.45"}), encoding="utf-8")
        self.assertEqual(quant_store.list_versions(), [])

    def test_the_template_config_is_not_offered_as_a_version(self) -> None:
        (self.engine / "strata").write_bytes(b"engine")
        (self.config / "run-example.json").write_text(json.dumps(
            {"model_name": "qwen3.8-flash-next-q2_0", "exe": "engine/strata",
             "args": ["--pack", "/ABSOLUTE/PATH/TO/packs/q2_0"]}), encoding="utf-8")
        (self.config / "run-q2_0.json").write_text(json.dumps(
            {"model_name": "qwen3.8-flash-next-q2_0", "exe": "engine/strata",
             "args": ["--pack", "/data/packs/q2_0"]}), encoding="utf-8")
        versions = quant_store.list_versions()
        self.assertEqual(len(versions), 1, "the placeholder template is listed as a model version")
        self.assertEqual(versions[0]["config_name"], "run-q2_0.json")

    def test_a_corrupt_index_is_rebuilt_not_crashed_on(self) -> None:
        self._store("q2_0", b"q2 engine")
        quant_store.INDEX_PATH.write_text("{not json", encoding="utf-8")
        data = quant_store.index()
        self.assertEqual(list(data["versions"]), ["q2_0"])
        self.assertEqual(len(quant_store.list_versions()), 1)

    def test_the_view_reports_the_active_version(self) -> None:
        self._store("q2_0", b"q2 engine")
        quant_store.select("q2_0")
        payload = quant_store.view()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["active"]["key"], "q2_0")
        self.assertTrue(payload["active"]["ready"])

    def test_an_empty_store_answers_with_nothing(self) -> None:
        self.assertEqual(quant_store.view(), {"active": None, "versions": [], "count": 0})

    def _store(self, key: str, payload: bytes, worker: bool = False) -> None:
        built = self.root / f"build-{key}"
        built.mkdir()
        (built / "strata").write_bytes(payload)
        if worker:
            (built / "hypercore-worker").write_bytes(b"worker")
        quant_store.store_binaries(key, [built / n for n in built.iterdir()],
                                  {"version": "0.1.45", "source": "built-from-source"})


if __name__ == "__main__":
    unittest.main()

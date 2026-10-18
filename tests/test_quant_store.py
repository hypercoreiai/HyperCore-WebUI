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
        self._saved = (quant_store.ENGINE_DIR, quant_store.LEGACY_DIR,
                       quant_store.INDEX_PATH, quant_store.CONFIG_DIR,
                       quant_store.LOG_DIR, quant_store.TMP_DIR, quant_store.ROOT)
        quant_store.ENGINE_DIR = self.engine
        quant_store.LEGACY_DIR = self.engine / "versions"
        quant_store.INDEX_PATH = self.engine / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.config
        quant_store.LOG_DIR = self.root / "logs"
        quant_store.TMP_DIR = self.root / "tmp"
        quant_store.ROOT = self.root

    def tearDown(self) -> None:
        quant_store.ENGINE_DIR, quant_store.LEGACY_DIR, quant_store.INDEX_PATH, \
            quant_store.CONFIG_DIR, quant_store.LOG_DIR, quant_store.TMP_DIR, \
            quant_store.ROOT = self._saved
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
    def test_a_build_is_stored_in_a_folder_named_for_it(self) -> None:
        built = self.root / "build"
        built.mkdir()
        for name in ("strata", "strata-vision", "hypercore-worker"):
            (built / name).write_bytes(f"binary {name}".encode())
        entry = quant_store.store_binaries("IQ2_S", [built / n for n in ("strata", "strata-vision", "hypercore-worker")],
                                          {"version": "0.1.45", "source": "built-from-source",
                                           "options": {"STRATA_NATIVE_EXPERTS": "ON"}})
        self.assertEqual(entry["key"], "iq2_s")
        self.assertTrue((self.engine / "engine_iq2_s" / "strata").is_file())
        self.assertTrue((self.engine / "engine_iq2_s" / "BUILD.json").is_file())
        self.assertEqual(len(json.loads((self.engine / "VERSIONS.json").read_text())["versions"]), 1)
        self.assertEqual(sorted(entry["files"]), sorted(quant_store.BINARIES))
        self.assertTrue(entry["files"]["strata"]["sha256"])
        self.assertEqual(entry["engine_dir"], str(self.engine / "engine_iq2_s"))

    def test_a_shared_library_the_engine_dlopen_is_carried_with_it(self) -> None:
        built = self.root / "build"
        built.mkdir()
        (built / "strata").write_bytes(b"engine")
        (built / "libstrata_kq.so").write_bytes(b"kernels")
        (built / "libstrata_core.a").write_bytes(b"static, not needed at run time")
        entry = quant_store.store_binaries("iq2_xs", list(built.iterdir()), {"version": "0.1.45"})
        self.assertEqual(sorted(entry["files"]), ["libstrata_kq.so", "strata"])
        self.assertTrue((self.engine / "engine_iq2_xs" / "libstrata_kq.so").is_file())
        self.assertFalse((self.engine / "engine_iq2_xs" / "libstrata_core.a").is_file())

    def test_the_old_engine_versions_layout_is_still_read(self) -> None:
        old = self.engine / "versions" / "q4_k_m"
        old.mkdir(parents=True)
        (old / "strata").write_bytes(b"old engine")
        (old / "BUILD.json").write_text(json.dumps({"version": "0.1.44"}), encoding="utf-8")
        versions = quant_store.list_versions()
        self.assertEqual([v["quant"] for v in versions], ["q4_k_m"])
        self.assertTrue(versions[0]["ready"])
        ok, note = quant_store.materialise("q4_k_m")
        self.assertTrue(ok, note)
        self.assertEqual((self.engine / "strata").read_bytes(), b"old engine")

    def test_nothing_in_the_store_is_written_outside_the_root(self) -> None:
        built = self.root / "build"
        built.mkdir()
        (built / "strata").write_bytes(b"engine")
        quant_store.store_binaries("q2_0", [built / "strata"], {"version": "0.1.45"})
        for path in self.engine.rglob("*"):
            try:
                path.resolve().relative_to(self.root)
            except ValueError:
                self.fail(f"the store wrote {path} outside the root")

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


class RunConfigTests(unittest.TestCase):
    """A build named for a quant also writes the run config that starts it.

    The engine binaries and the model files are two different things: a build puts a
    `strata` in engine/engine_<quant>/, and without a config naming that quant the
    Model version dropdown has nothing to point at. The generated config is the nearest
    one on the box with the quant rewritten, and every path that is not here is named
    in a _note rather than left to fail at start-up.
    """

    BASE = json.dumps({
        "exe": "engine/strata",
        "args": ["--pack", "/data/packs/q2_0",
                 "--native", "/data/models/Q2_0/Qwen3.8-Flash-Next-Q2_0-00001.gguf",
                 "--kv", "q4_0",
                 "--max-context", "262144"],
        "tokenizer": "/data/packs/q2_0/tokenizer",
        "model_name": "qwen3.8-flash-next-q2_0",
        "log": "logs/strata-q2_0.log",
        "port": 8080,
    })

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = self.root / "engine"
        self.config = self.root / "config"
        self.engine.mkdir()
        self.config.mkdir()
        self._saved = (quant_store.ENGINE_DIR, quant_store.LEGACY_DIR,
                       quant_store.INDEX_PATH, quant_store.CONFIG_DIR, quant_store.ROOT,
                       quant_store.LOG_DIR, quant_store.TMP_DIR)
        quant_store.ENGINE_DIR = self.engine
        quant_store.LEGACY_DIR = self.engine / "versions"
        quant_store.INDEX_PATH = self.engine / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.config
        quant_store.ROOT = self.root
        quant_store.LOG_DIR = self.root / "logs"
        quant_store.TMP_DIR = self.root / "tmp"
        (self.config / "run-q2_0.json").write_text(self.BASE, encoding="utf-8")

    def tearDown(self) -> None:
        quant_store.ENGINE_DIR, quant_store.LEGACY_DIR, quant_store.INDEX_PATH, \
            quant_store.CONFIG_DIR, quant_store.ROOT, quant_store.LOG_DIR, \
            quant_store.TMP_DIR = self._saved
        self.tmp.cleanup()

    def test_a_new_quant_gets_a_config_with_its_own_paths(self) -> None:
        path, note = quant_store.ensure_run_config("IQ2_XS")
        self.assertEqual(path, self.config / "run-iq2_xs.json")
        self.assertIn("created", note)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["args"][1], "/data/packs/iq2_xs",
                         "a lowercase path segment stays lowercase")
        self.assertEqual(cfg["args"][3],
                         "/data/models/IQ2_XS/Qwen3.8-Flash-Next-IQ2_XS-00001.gguf",
                         "an uppercase segment keeps its case")
        self.assertEqual(cfg["model_name"], "qwen3.8-flash-next-iq2_xs")
        self.assertEqual(cfg["log"], "logs/strata-iq2_xs.log")

    def test_a_flag_value_that_is_not_a_model_is_left_alone(self) -> None:
        path, _ = quant_store.ensure_run_config("IQ2_XS")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["args"][4:6], ["--kv", "q4_0"], "--kv q4_0 is a cache format, not a model")
        self.assertEqual(cfg["port"], 8080)

    def test_paths_that_are_not_on_the_box_are_named_not_hidden(self) -> None:
        path, note = quant_store.ensure_run_config("IQ2_XS")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("_note", cfg)
        self.assertIn("/data/packs/iq2_xs", cfg["_note"])
        self.assertIn("fill in", note)

    def test_a_config_copied_from_another_install_points_at_this_projects_area(self) -> None:
        """The data area belongs to the install, so a copied config is repointed at it."""
        project = self.root / "HyperCore-WebUI"
        project.mkdir()
        area = self.root / "Strata-data"
        other = self.root / "ai-user1" / "Strata-data"
        (area / "packs" / "iq2_xs").mkdir(parents=True)
        (area / "packs" / "iq2_xs" / "tokenizer").write_text("tok")
        (area / "models" / "IQ2_XS").mkdir(parents=True)
        (area / "models" / "IQ2_XS" / "model-00001.gguf").write_text("m")
        base = json.loads(self.BASE)
        base["tokenizer"] = str(other / "packs" / "iq2_xs" / "tokenizer")
        base["args"] = ["--pack", str(other / "packs" / "iq2_xs"),
                        "--native", str(other / "models" / "IQ2_XS" / "model-00001.gguf")]
        base_text = json.dumps(base)
        (self.config / "run-base.json").write_text(base_text, encoding="utf-8")
        saved = quant_store.ROOT
        quant_store.ROOT = project
        try:
            path, note = quant_store.ensure_run_config("IQ2_XS")
        finally:
            quant_store.ROOT = saved
        self.assertIsNotNone(path, note)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["tokenizer"], str(area / "packs" / "iq2_xs" / "tokenizer"))
        self.assertEqual(cfg["args"][3], str(area / "models" / "IQ2_XS" / "model-00001.gguf"))
        self.assertIn("data area", note)
        self.assertEqual((self.config / "run-base.json").read_text(encoding="utf-8"), base_text,
                         "the config the new one was copied from is left as it was")

    def test_a_config_written_from_the_template_is_filled_from_the_data_area(self) -> None:
        """A placeholder is a shape; the model files on this box are the answer."""
        project = self.root / "HyperCore-WebUI"
        project.mkdir()
        area = self.root / "Strata-data"
        (area / "packs" / "iq2_xs").mkdir(parents=True)
        (area / "packs" / "iq2_xs" / "tokenizer").write_text("tok")
        (area / "models" / "IQ2_XS").mkdir(parents=True)
        (area / "models" / "IQ2_XS" / "Qwen3.8-Flash-Next-IQ2_XS-00001.gguf").write_text("m")
        saved = quant_store.ROOT
        quant_store.ROOT = project
        try:
            path, note = quant_store.ensure_run_config("IQ2_XS")
        finally:
            quant_store.ROOT = saved
        self.assertIsNotNone(path, note)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["tokenizer"], str(area / "packs" / "iq2_xs" / "tokenizer"))
        self.assertEqual(cfg["args"][3],
                         str(area / "models" / "IQ2_XS" / "Qwen3.8-Flash-Next-IQ2_XS-00001.gguf"))
        self.assertIn("filled from the data area", note)
        # the log is not model data, so it stays in the project's logs dir, not the area
        self.assertTrue(str(cfg["log"]).startswith(str(self.root / "logs")), cfg.get("log"))
        self.assertNotIn(str(area), str(cfg.get("log", "")))
        self.assertEqual(cfg["model_name"], "qwen3.8-flash-next-iq2_xs")

    def test_a_placeholder_shard_is_matched_to_the_file_the_area_actually_names(self) -> None:
        """The template names a shard; the area holds it under the model's own name."""
        project = self.root / "HyperCore-WebUI"
        project.mkdir()
        area = self.root / "Strata-data"
        model = area / "models" / "IQ2_XS"
        model.mkdir(parents=True)
        for shard in ("00001", "00002"):
            (model / f"Qwen3.8-Flash-Next-IQ2_XS-{shard}-of-00002.gguf").write_text("m")
        (model / "Qwen3.8-Flash-Next-IQ2_XS-00001-of-00002.gguf.done").write_text("d")
        (self.config / "run-q2_0.json").unlink()          # the template is the only shape left
        template = {
            "exe": "engine/strata",
            "args": ["--pack", "/ABSOLUTE/PATH/TO/packs/q2_0",
                     "--native", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00001-of-00002.gguf",
                     "--ple-gguf", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00002-of-00002.gguf",
                     "--native", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00007-of-00002.gguf"],
            "model_name": "q2_0",
            "log": "logs/strata-q2_0.log",
            "port": 8080,
        }
        (self.config / "run-example.json").write_text(json.dumps(template), encoding="utf-8")
        saved = quant_store.ROOT
        quant_store.ROOT = project
        try:
            path, note = quant_store.ensure_run_config("IQ2_XS")
        finally:
            quant_store.ROOT = saved
        self.assertIsNotNone(path, note)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        shard1 = model / "Qwen3.8-Flash-Next-IQ2_XS-00001-of-00002.gguf"
        shard2 = model / "Qwen3.8-Flash-Next-IQ2_XS-00002-of-00002.gguf"
        absent = "/ABSOLUTE/PATH/TO/models/IQ2_XS/model-00007-of-00002.gguf"
        self.assertEqual(cfg["args"][3], str(shard1))
        self.assertEqual(cfg["args"][5], str(shard2))
        self.assertEqual(cfg["args"][7], absent, "a shard not on this box is named, not invented")
        self.assertIn("filled from the data area", note)
        self.assertIn("fill in", note)
        self.assertNotIn(".done", json.dumps(cfg), ".done is a marker, not the model")

    def test_two_files_carrying_the_same_shard_are_not_a_guess(self) -> None:
        project = self.root / "HyperCore-WebUI"
        project.mkdir()
        area = self.root / "Strata-data"
        model = area / "models" / "IQ2_XS"
        model.mkdir(parents=True)
        (model / "a-00001-of-00002.gguf").write_text("m")
        (model / "b-00001-of-00002.gguf").write_text("m")
        (self.config / "run-q2_0.json").unlink()
        template = {"exe": "engine/strata",
                    "args": ["--native", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00001-of-00002.gguf"],
                    "model_name": "q2_0", "log": "logs/strata-q2_0.log", "port": 8080}
        (self.config / "run-example.json").write_text(json.dumps(template), encoding="utf-8")
        saved = quant_store.ROOT
        quant_store.ROOT = project
        try:
            path, note = quant_store.ensure_run_config("IQ2_XS")
        finally:
            quant_store.ROOT = saved
        self.assertIsNotNone(path, note)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        placeholder = "/ABSOLUTE/PATH/TO/models/IQ2_XS/model-00001-of-00002.gguf"
        self.assertEqual(cfg["args"][1], placeholder, "two matches is not an answer")
        self.assertIn("fill in", note)

    def test_an_existing_config_is_never_overwritten(self) -> None:
        path, note = quant_store.ensure_run_config("q2_0")
        self.assertEqual(path, self.config / "run-q2_0.json")
        self.assertIn("already points at this build", note)
        self.assertEqual(json.loads(path.read_text())["model_name"], "qwen3.8-flash-next-q2_0")

    def test_a_new_config_is_listed_as_a_model_version(self) -> None:
        quant_store.ensure_run_config("IQ2_XS")
        keys = [v["quant"] for v in quant_store.list_versions()]
        self.assertIn("iq2_xs", keys)

    def _store(self, key: str, payload: bytes = b"engine") -> Path:
        built = self.root / f"build-{key}"
        built.mkdir()
        (built / "strata").write_bytes(payload)
        quant_store.store_binaries(key, [built / "strata"], {"version": "0.1.45"})
        return quant_store.store_dir(key)

    def test_an_existing_config_is_pointed_at_the_build_that_runs_it(self) -> None:
        store = self._store("q2_0", b"the engine")
        path, note = quant_store.ensure_run_config("q2_0")
        cfg = json.loads(path.read_text())
        self.assertEqual(cfg["exe"], str(store / "strata"))
        self.assertIn("exe ->", note)
        log = Path(cfg["log"])
        if not log.is_absolute():
            log = quant_store.ROOT / log
        self.assertEqual(log, quant_store.LOG_DIR / "strata-q2_0.log")
        # a second call has nothing left to change
        path2, note2 = quant_store.ensure_run_config("q2_0")
        self.assertIn("already points at this build", note2)
        self.assertEqual(path2, path)

    def test_an_existing_config_still_named_by_a_placeholder_is_filled(self) -> None:
        """A config written before the model files arrived is repaired on the next build."""
        store = self._store("q2_0", b"the engine")
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--native", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00001-of-00002.gguf"]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        model = self.root / "Strata-data" / "models" / "Q2_0"
        model.mkdir(parents=True)
        (model / "Qwen3.8-Flash-Next-Q2_0-00001-of-00002.gguf").write_text("m")
        path, note = quant_store.ensure_run_config("q2_0")
        out = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(out["args"][1], str(model / "Qwen3.8-Flash-Next-Q2_0-00001-of-00002.gguf"))
        self.assertIn("filled from the data area", note)
        self.assertEqual(out["exe"], str(store / "strata"), "the engine path is still the build's")

    def test_a_log_written_outside_the_repository_is_moved_into_logs(self) -> None:
        cfg = json.loads(self.BASE)
        cfg["log"] = "/home/anotheruser/Strata/strata-q2_0.log"
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, note = quant_store.ensure_run_config("q2_0")
        self.assertIn("log ->", note)
        self.assertEqual(json.loads(path.read_text())["log"],
                         str(quant_store.LOG_DIR / "strata-q2_0.log"))

    def test_a_cwd_in_the_build_scratch_dir_is_replaced_with_the_root(self) -> None:
        cfg = json.loads(self.BASE)
        cfg["cwd"] = str(quant_store.TMP_DIR / "strata-v0.1.45")
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, note = quant_store.ensure_run_config("q2_0")
        self.assertIn("cwd -> the repository root", note)
        self.assertEqual(json.loads(path.read_text())["cwd"], str(self.root))

    def test_runtime_files_in_the_scratch_dir_are_carried_into_the_engine_folder(self) -> None:
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        profile = clone / "data" / "expert-profile.bin"
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b"profile")
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--expert-profile", str(profile)]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, note = quant_store.ensure_run_config("q2_0", source_root=clone)
        carried = json.loads(path.read_text())["args"][1]
        self.assertTrue(carried.startswith(str(quant_store.store_dir("q2_0") / "data")))
        self.assertTrue(Path(carried).is_file(), "the file was not copied next to the engine")
        self.assertIn("runtime file path(s) moved", note)
        # the engine folder keeps the file after tmp/ is deleted with the clone
        import shutil
        shutil.rmtree(clone)
        self.assertTrue(Path(carried).is_file())

    def test_a_model_store_outside_the_repository_is_left_where_it_is(self) -> None:
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--pack", "/home/anotheruser/Strata-data/packs/q2_0"]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, _ = quant_store.ensure_run_config("q2_0", source_root=quant_store.TMP_DIR / "strata-v0.1.45")
        self.assertEqual(json.loads(path.read_text())["args"][1],
                         "/home/anotheruser/Strata-data/packs/q2_0",
                         "the Build tab moved a model pack the user installed elsewhere")

    def test_with_no_config_to_copy_there_is_nothing_to_write(self) -> None:
        for path in list(self.config.iterdir()):
            path.unlink()
        target, note = quant_store.ensure_run_config("iq2_s")
        self.assertIsNone(target)
        self.assertIn("no run config", note)

    def test_a_bad_quant_name_is_refused_not_turned_into_a_file(self) -> None:
        with self.assertRaises(ValueError):
            quant_store.ensure_run_config("q2 0%!")


if __name__ == "__main__":
    unittest.main()

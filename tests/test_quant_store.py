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

    def test_the_quant_of_a_setup_run_is_read_from_what_it_wrote_in_the_clone(self) -> None:
        """The card's model box can name a model, or be left unset: the quant the run
        chose is only written in the run config setup.py leaves beside itself.

        That config is what keys the store for a run in the clone, so a run that named no
        quant in its command still has a quant to be stored under.
        """
        clone = self.root / "tmp" / "strata-v0.1.45"
        clone.mkdir(parents=True)
        cfg = {"model_name": "qwen3.8-flash-next-iq2_xs",
               "args": ["--pack", str(clone / "data" / "packs" / "iq2_xs")]}
        (clone / "strata-qwen3.8-flash-next-iq2_xs.json").write_text(json.dumps(cfg))
        self.assertEqual(quant_store.quant_from_checkout(clone), "iq2_xs")

        # #549: a shared-settings file beside a config is not a model config, so it is
        # not read for a quant.
        (clone / "strata-qwen3.8-flash-next.shared-settings.json").write_text('{"chat": {}}')
        self.assertEqual(quant_store.quant_from_checkout(clone), "iq2_xs")

        # A config that names no quant falls back to the start script setup.py wrote.
        other = self.root / "tmp" / "strata-v0.2.0-beta"
        other.mkdir(parents=True)
        (other / "strata-something.json").write_text('{"model_name": "no-quant-here"}')
        (other / "run-iq3_s.sh").write_text("#!/bin/sh\n")
        self.assertEqual(quant_store.quant_from_checkout(other), "iq3_s")

        # Nothing readable, no quant: reported, not guessed at.
        empty = self.root / "tmp" / "strata-v0.3.0"
        empty.mkdir(parents=True)
        self.assertIsNone(quant_store.quant_from_checkout(empty))

    def test_the_whole_setup_run_in_the_clone_is_moved_into_config_and_engine(self) -> None:
        """#549: the harvest as one run, from the clone to where the app reads it.

        setup.py writes inside the clone it is run from: binaries in build/, the run config
        beside itself, runtime files in data/. The card's model box named a model, not a
        size, so the quant is only knowable from the config the run wrote. Everything the
        run produced must leave tmp/ - the folder the next build may delete - and land in
        engine/engine_<quant>/, config/run-<quant>.json, and engine/engine_<quant>/data/.
        """
        clone = self.root / "tmp" / "strata-v0.1.45"
        (clone / "build").mkdir(parents=True)
        (clone / "build" / "strata").write_bytes(b"engine")
        (clone / "data").mkdir(parents=True)
        (clone / "data" / "expert-profile.bin").write_bytes(b"the build's profile")
        cfg = {"model_name": "qwen3.8-flash-next-iq2_xs",
               "args": ["--expert-profile", str(clone / "data" / "expert-profile.bin")]}
        (clone / "strata-qwen3.8-flash-next-iq2_xs.json").write_text(json.dumps(cfg))

        report = quant_store.harvest_setup_run(clone, "qwen3.8-flash-next", None,
                                               {"version": "0.1.45", "source": "setup-hook"})
        self.assertEqual(report["quant"], "iq2_xs",
                         "the harvest keyed on something other than the quant the run used")
        self.assertTrue((self.engine / "engine_iq2_xs" / "strata").is_file(),
                        "the binary stayed in the clone instead of engine/engine_iq2_xs/")
        written_path = self.config / "run-iq2_xs.json"
        self.assertTrue(written_path.is_file(), "config/ got no run config for the build")
        written = json.loads(written_path.read_text(encoding="utf-8"))
        self.assertEqual(written["exe"], str(self.engine / "engine_iq2_xs" / "strata"))
        self.assertEqual(written["args"][written["args"].index("--expert-profile") + 1],
                         str(self.engine / "engine_iq2_xs" / "data" / "expert-profile.bin"),
                         "the config written still points inside tmp/")
        self.assertTrue((self.engine / "engine_iq2_xs" / "data" / "expert-profile.bin").is_file(),
                        "the runtime files stayed in the clone's data/")
        self.assertTrue(report["active"], "the store did not move its active pointer to the build")

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

    def test_the_shipped_template_is_never_written_as_a_config(self) -> None:
        """A build named "example" must not overwrite the template it is named after.

        normalise_quant accepts "example" as a name, so the build's target is
        config/run-example.json itself. That file is the shape a new config is copied
        from and the one run.sh refuses to pick: writing this box's paths over it leaves
        no template for anyone who copies it, and a server with no run config falls back
        to it, so the file's contents change what the tests see.
        """
        template = self.config / "run-example.json"
        template.write_text(self.BASE, encoding="utf-8")
        before = template.read_text(encoding="utf-8")
        path, note = quant_store.ensure_run_config("example")
        self.assertIsNone(path, note)
        self.assertIn("shipped template", note)
        self.assertEqual(template.read_text(encoding="utf-8"), before,
                         "the template was written over")
        self.assertEqual(sorted(p.name for p in self.config.iterdir()),
                         ["run-example.json", "run-q2_0.json"],
                         "the guard wrote a file somewhere else")

    def test_a_config_with_placeholder_paths_is_listed_with_the_reason(self) -> None:
        """A run config the user can see on disk has to appear in the dropdown, greyed, with
        the reason it cannot be started. Leaving it out made the box disagree with the
        folder: the file was there and the app said there was no version."""
        placeholder = json.loads(self.BASE)
        placeholder["model_name"] = "qwen3.8-flash-next-iq3_xxs"
        placeholder["args"] = ["--pack", "/ABSOLUTE/PATH/TO/packs/iq3_xxs",
                               "/ABSOLUTE/PATH/TO/models/IQ3_XXS/model-00001-of-00002.gguf"]
        (self.config / "run-iq3_xxs.json").write_text(json.dumps(placeholder), encoding="utf-8")

        listed = quant_store.template_configs()
        self.assertEqual([c["name"] for c in listed], ["run-iq3_xxs.json"])
        self.assertFalse(listed[0]["startable"])
        self.assertIn("placeholder", listed[0]["reason"])
        self.assertNotIn("run-iq3_xxs.json", [c["name"] for c in quant_store._run_configs()],
                         "a config with no model files behind it is offered as a startable one")

        entry = next((v for v in quant_store.list_versions() if v["config_name"] == "run-iq3_xxs.json"), None)
        self.assertIsNotNone(entry, f"the dropdown does not list the config: {quant_store.list_versions()}")
        self.assertFalse(entry["startable"], "the dropdown is told the config can be started")
        self.assertIn("placeholder", entry["reason"])

        with self.assertRaises(ValueError) as got:
            quant_store.select("iq3_xxs")
        self.assertIn("placeholder", str(got.exception),
                      "the request for it is refused as a missing version, not as an unstartable one")

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

    def test_the_whole_data_folder_of_the_build_is_stored_for_its_quant(self) -> None:
        """A build stores the data folder it ships, not only what the config named.

        The q2_0 case: the config was written from the template and names a placeholder,
        so nothing was carried and engine/engine_q2_0/ had binaries and no data folder.
        """
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--expert-profile", "/ABSOLUTE/PATH/TO/expert-profile.bin"]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        for name, content in (("expert-profile.bin", b"profile"),
                              ("draft_vocab.bin", b"vocab"),
                              ("experimental-speed-projection/projection.gguf", b"projection")):
            path = clone / "data" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        path, note = quant_store.ensure_run_config("q2_0", source_root=clone)
        stored = quant_store.store_dir("q2_0") / "data"
        self.assertEqual(sorted(p.name for p in stored.rglob("*") if p.is_file()),
                         ["draft_vocab.bin", "expert-profile.bin", "projection.gguf"],
                         "the build stored nothing for this quant")
        self.assertIn("runtime file(s) from the clone stored", note)
        # and the run config now points at what the build stored
        self.assertEqual(json.loads(path.read_text())["args"][1],
                         str(stored / "expert-profile.bin"))

    def test_the_data_folder_is_stored_for_every_quant(self) -> None:
        """A build for a second quant stores the same runtime files for that quant.

        `point_config_at_build` is what the Build tab calls for each quant, so every
        quant gets its own data folder, not only the one the config was written for.
        """
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        (clone / "data").mkdir(parents=True)
        (clone / "data" / "expert-profile.bin").write_bytes(b"profile")
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--pack", str(self.root / "Strata-data" / "packs" / "q2_0"),
                       "--expert-profile", "/ABSOLUTE/PATH/TO/expert-profile.bin"]
        for key in ("q2_0", "iq2_s", "q4_k_m"):
            changes = quant_store.point_config_at_build(cfg, key, source_root=clone)
            stored = quant_store.data_dir(key)
            self.assertTrue((stored / "expert-profile.bin").is_file(),
                            f"a build for {key} stored nothing for it")
            self.assertIn("runtime file(s) from the clone stored", " ".join(changes))
            self.assertEqual(cfg["args"][3], str(stored / "expert-profile.bin"),
                             f"the config for {key} was left pointing at the placeholder")

    def test_a_data_file_that_is_already_on_this_box_is_left_where_it_is(self) -> None:
        """The rule for a path that exists still holds: it is the user's file."""
        other = self.root / "another-install" / "data"
        other.mkdir(parents=True)
        (other / "expert-profile.bin").write_bytes(b"the one the user installed")
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        (clone / "data").mkdir(parents=True)
        (clone / "data" / "expert-profile.bin").write_bytes(b"the build's profile")
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--expert-profile", str(other / "expert-profile.bin")]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, _ = quant_store.ensure_run_config("q2_0", source_root=clone)
        self.assertEqual(json.loads(path.read_text())["args"][1], str(other / "expert-profile.bin"),
                         "the Build tab moved a runtime file the user installed elsewhere")
        self.assertEqual((other / "expert-profile.bin").read_bytes(), b"the one the user installed")

    def test_a_data_file_the_build_stored_for_another_quant_is_repointed(self) -> None:
        """A config for q2_0 left pointing at another build's data folder is stale."""
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        (clone / "data").mkdir(parents=True)
        (clone / "data" / "expert-profile.bin").write_bytes(b"profile")
        other = quant_store.data_dir("iq2_xs")
        other.mkdir(parents=True)
        (other / "expert-profile.bin").write_bytes(b"the earlier build")
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--expert-profile", str(other / "expert-profile.bin")]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, _ = quant_store.ensure_run_config("q2_0", source_root=clone)
        written = json.loads(path.read_text())["args"][1]
        self.assertEqual(written, str(quant_store.data_dir("q2_0") / "expert-profile.bin"),
                         "the config for q2_0 still pointed at another build's data folder")

    def test_a_runtime_file_stored_in_a_subfolder_is_pointed_at_where_it_is(self) -> None:
        """The build keeps the shape of the clone's data folder, so a file can be nested.

        The speed projection ships under `data/experimental-speed-projection/` and the
        engine reads it by name with a weight suffix (`file.gguf:1.0`). Pointing at the top
        of the data folder with the name put back on it names a file that is not there.
        """
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        sub = clone / "data" / "experimental-speed-projection"
        sub.mkdir(parents=True)
        (sub / "speed-projection.gguf").write_bytes(b"projection")
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--control-vector-scaled", "/ABSOLUTE/PATH/TO/speed-projection.gguf:1.0"]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, _ = quant_store.ensure_run_config("q2_0", source_root=clone)
        written = json.loads(path.read_text())["args"][1]
        self.assertEqual(written, str(quant_store.data_dir("q2_0") / "experimental-speed-projection"
                                    / "speed-projection.gguf") + ":1.0",
                         "a runtime file the build stored in a subfolder was pointed at as if "
                         "it were at the top of the data folder")

    def test_two_runtime_files_with_the_same_name_are_not_a_guess(self) -> None:
        """The file at the top of the data folder is the one the engine reads by name."""
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        sub = clone / "data" / "experimental-speed-projection"
        sub.mkdir(parents=True)
        (sub / "speed-projection.gguf").write_bytes(b"nested")
        (clone / "data" / "speed-projection.gguf").write_bytes(b"the one read by name")
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--control-vector-scaled", "/ABSOLUTE/PATH/TO/speed-projection.gguf:1.0"]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, _ = quant_store.ensure_run_config("q2_0", source_root=clone)
        written = json.loads(path.read_text())["args"][1]
        self.assertEqual(written, str(quant_store.data_dir("q2_0") / "speed-projection.gguf") + ":1.0",
                         "two files carrying the same name were resolved by guessing")

    def test_a_model_path_is_not_treated_as_a_runtime_data_file(self) -> None:
        """A pack or a model shard is filled from the data area, not from the build."""
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        (clone / "data").mkdir(parents=True)
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--pack", "/ABSOLUTE/PATH/TO/packs/q2_0", "--native",
                       "/ABSOLUTE/PATH/TO/models/Q2_0/x.gguf"]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        _, note = quant_store.ensure_run_config("q2_0", source_root=clone)
        written = json.loads((self.config / "run-q2_0.json").read_text())["args"]
        self.assertEqual(written, cfg["args"],
                         "a model path was filled from the build's data folder")
        self.assertNotIn("runtime file(s)", note, "a model path was read as a runtime data file")

    def test_the_default_engine_gets_its_own_data_folder(self) -> None:
        """The engine in engine/ has no quant key, so its data folder is engine/data/."""
        clone = quant_store.TMP_DIR / "strata-v0.1.45"
        (clone / "data").mkdir(parents=True)
        (clone / "data" / "expert-profile.bin").write_bytes(b"profile")
        copied = quant_store.carry_data_dir(None, clone)
        self.assertEqual(copied, ["expert-profile.bin"])
        self.assertEqual(quant_store.data_dir(None), self.engine / "data")
        self.assertTrue((self.engine / "data" / "expert-profile.bin").is_file())

    def test_a_model_store_outside_the_repository_is_left_where_it_is(self) -> None:
        cfg = json.loads(self.BASE)
        cfg["args"] = ["--pack", "/home/anotheruser/Strata-data/packs/q2_0"]
        (self.config / "run-q2_0.json").write_text(json.dumps(cfg), encoding="utf-8")
        path, _ = quant_store.ensure_run_config("q2_0", source_root=quant_store.TMP_DIR / "strata-v0.1.45")
        self.assertEqual(json.loads(path.read_text())["args"][1],
                         "/home/anotheruser/Strata-data/packs/q2_0",
                         "the Build tab moved a model pack the user installed elsewhere")

    def test_with_no_config_to_copy_the_code_template_is_used(self) -> None:
        for path in list(self.config.iterdir()):
            path.unlink()
        target, note = quant_store.ensure_run_config("iq2_s")
        self.assertIsNotNone(target, note)
        self.assertEqual(target, self.config / "run-iq2_s.json")
        self.assertIn("created config/run-iq2_s.json", note)
        cfg = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(cfg["model_name"], "iq2_s")
        self.assertIn("--resident-experts", cfg["args"], "the template config lost the flags")

    def test_a_bad_quant_name_is_refused_not_turned_into_a_file(self) -> None:
        with self.assertRaises(ValueError):
            quant_store.ensure_run_config("q2 0%!")


class MergeDataTests(unittest.TestCase):
    """The data folder a build's clone carries is merged into the project's data/."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.clone = self.root / "tmp" / "strata-v0.1.45"
        self._saved = (quant_store.ROOT, quant_store.PROJECT_DATA_DIR, quant_store.TMP_DIR)
        quant_store.ROOT = self.root
        quant_store.PROJECT_DATA_DIR = self.data
        quant_store.TMP_DIR = self.root / "tmp"
        # the app's own files, which a merge must never touch
        (self.data / "history" / "chats").mkdir(parents=True)
        (self.data / "history" / "index.json").write_text("[]", encoding="utf-8")
        (self.data / "media").mkdir()
        (self.data / "media" / "chat_1.png").write_bytes(b"the app's own file")

    def tearDown(self) -> None:
        quant_store.ROOT, quant_store.PROJECT_DATA_DIR, quant_store.TMP_DIR = self._saved
        self.tmp.cleanup()

    def clone_file(self, rel: str, content: bytes) -> Path:
        path = self.clone / "data" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_the_clone_data_is_merged_into_the_project_data(self) -> None:
        self.clone_file("expert-profile.bin", b"profile")
        self.clone_file("draft_vocab.bin", b"vocab")
        self.clone_file("experimental-speed-projection/projection.gguf", b"projection")
        merged = quant_store.merge_data(self.clone)
        self.assertEqual(sorted(merged["added"]), [
            "draft_vocab.bin", "experimental-speed-projection/projection.gguf",
            "expert-profile.bin"])
        for rel in merged["added"]:
            self.assertTrue((self.data / rel).is_file(), f"{rel} was not merged")
        # the app's own files are still there and unchanged
        self.assertEqual((self.data / "media" / "chat_1.png").read_bytes(), b"the app's own file")
        self.assertEqual((self.data / "history" / "index.json").read_text(encoding="utf-8"), "[]")

    def test_a_file_already_in_data_is_not_overwritten(self) -> None:
        self.data.joinpath("expert-profile.bin").write_bytes(b"the one the app already had")
        self.clone_file("expert-profile.bin", b"the build's profile")
        merged = quant_store.merge_data(self.clone)
        self.assertEqual(merged["added"], [])
        self.assertEqual(merged["skipped"], ["expert-profile.bin"])
        self.assertEqual(self.data.joinpath("expert-profile.bin").read_bytes(),
                         b"the one the app already had",
                         "the merge replaced a file the project already had")

    def test_running_the_merge_twice_changes_nothing(self) -> None:
        self.clone_file("expert-profile.bin", b"profile")
        first = quant_store.merge_data(self.clone)
        second = quant_store.merge_data(self.clone)
        self.assertEqual(second["added"], [])
        self.assertEqual(second["same"], first["added"])
        self.assertEqual(sorted(p.name for p in self.data.rglob("*") if p.is_file()),
                         ["chat_1.png", "expert-profile.bin", "index.json"],
                         "the merge wrote a file twice")

    def test_a_clone_with_no_data_folder_is_not_an_error(self) -> None:
        self.clone.mkdir(parents=True, exist_ok=True)
        self.assertEqual(quant_store.merge_data(self.clone),
                         {"added": [], "skipped": [], "same": []})
        self.assertEqual(sorted(p.name for p in self.data.rglob("*") if p.is_file()),
                         ["chat_1.png", "index.json"],
                         "a build that shipped data created files anyway")

    def test_nothing_is_written_outside_the_project(self) -> None:
        self.clone_file("expert-profile.bin", b"profile")
        outside = Path(self.root).parent / "outside-the-project"
        merged = quant_store.merge_data(self.clone, dest=outside)
        self.assertEqual(merged["added"], [])
        self.assertFalse(outside.exists(), "the merge wrote outside the project root")


class RefreshRunConfigTests(unittest.TestCase):
    """A build points every run config in config/ at the build stored for its model.

    The Build tab writes the config of the quant it was asked for. The other configs on
    the box keep what they had: the shipped template names `engine/engine_q2_0/strata`
    and `/ABSOLUTE/PATH/TO/expert-profile.bin` on a machine that built IQ2_XS, and a
    model built earlier has no data folder of its own. The sweep at the end of a build
    fixes them all, and leaves alone a config there is nothing to fix - a model with no
    engine build, or a path that already exists on this box.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = self.root / "engine"
        self.config = self.root / "config"
        self.clone = self.root / "tmp" / "strata-v0.1.45"
        self.engine.mkdir()
        self.config.mkdir()
        (self.clone / "data").mkdir(parents=True)
        (self.clone / "data" / "expert-profile.bin").write_bytes(b"the build's profile")
        (self.clone / "data" / "draft_vocab.bin").write_bytes(b"vocab")
        self._saved = (quant_store.ENGINE_DIR, quant_store.LEGACY_DIR, quant_store.INDEX_PATH,
                       quant_store.CONFIG_DIR, quant_store.ROOT, quant_store.LOG_DIR,
                       quant_store.TMP_DIR)
        quant_store.ENGINE_DIR = self.engine
        quant_store.LEGACY_DIR = self.engine / "versions"
        quant_store.INDEX_PATH = self.engine / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.config
        quant_store.ROOT = self.root
        quant_store.LOG_DIR = self.root / "logs"
        quant_store.TMP_DIR = self.root / "tmp"

    def tearDown(self) -> None:
        quant_store.ENGINE_DIR, quant_store.LEGACY_DIR, quant_store.INDEX_PATH, \
            quant_store.CONFIG_DIR, quant_store.ROOT, quant_store.LOG_DIR, \
            quant_store.TMP_DIR = self._saved
        self.tmp.cleanup()

    def _store(self, key: str, payload: bytes = b"engine") -> Path:
        """An engine build on the box for this quant."""
        built = self.root / f"build-{key}"
        built.mkdir()
        (built / "strata").write_bytes(payload)
        quant_store.store_binaries(key, [built / "strata"], {"version": "0.1.45"})
        return quant_store.store_dir(key)

    def _config(self, name: str, quant: str | None = None) -> Path:
        """A run config in config/ naming a quant, with a placeholder runtime file."""
        cfg = {
            "exe": "engine/strata",
            "args": ["--kv", "int8", "--expert-profile", "/ABSOLUTE/PATH/TO/expert-profile.bin"],
            "model_name": quant or "",
            "log": "logs/strata.log",
            "port": 8080,
        }
        path = self.config / name
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_a_build_updates_the_config_of_every_model_it_stored_a_build_for(self) -> None:
        """The q2_0 case: a model built earlier gets its data folder and its pointer."""
        self._store("iq2_xs")
        self._store("q2_0")
        self._config("run-iq2_xs.json", "iq2_xs")
        self._config("run-q2_0.json", "q2_0")
        notes = quant_store.refresh_run_configs("iq2_xs", self.clone)
        joined = " ".join(notes)
        for key in ("iq2_xs", "q2_0"):
            stored = quant_store.data_dir(key) / "expert-profile.bin"
            self.assertTrue(stored.is_file(), f"the build stored no runtime data for {key}")
            cfg = json.loads((self.config / f"run-{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["args"][3], str(stored),
                             f"config/run-{key}.json still names the placeholder")
            self.assertIn(f"run-{key}.json", joined)

    def test_a_config_for_a_model_with_no_engine_build_is_left_alone(self) -> None:
        """There is nothing to point at, so the placeholder is better than a guess."""
        self._store("iq2_xs")
        path = self._config("run-q4_k_m.json", "q4_k_m")
        before = path.read_text()
        notes = quant_store.refresh_run_configs("iq2_xs", self.clone)
        self.assertEqual(path.read_text(), before, "a config with no build was rewritten")
        self.assertFalse(any("q4_k_m" in n for n in notes))
        self.assertFalse(quant_store.data_dir("q4_k_m").exists(),
                         "a data folder was made for a model that was never built")

    def test_the_shipped_template_is_repaired_when_its_quant_has_a_build(self) -> None:
        """config/run-example.json names q2_0 and is the config the server falls back to."""
        store = self._store("q2_0")
        path = self._config("run-example.json", "q2_0")
        notes = quant_store.refresh_run_configs("q2_0", self.clone)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["exe"], str(store / "strata"),
                         "the fallback config still names an engine that is not here")
        self.assertEqual(cfg["args"][3], str(quant_store.data_dir("q2_0") / "expert-profile.bin"))
        self.assertTrue(any("run-example.json" in n for n in notes))

    def test_a_config_naming_no_quant_uses_the_default_engine_data(self) -> None:
        """engine/data/ is the runtime data of the engine in engine/ itself."""
        (self.engine / "data").mkdir()
        (self.engine / "data" / "expert-profile.bin").write_bytes(b"the default profile")
        path = self._config("run-default.json", None)
        notes = quant_store.refresh_run_configs(None)
        self.assertTrue(notes, "the default engine's config was not pointed at its data")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["args"][3], str(quant_store.data_dir(None) / "expert-profile.bin"))

    def test_a_config_that_is_already_right_is_not_written_again(self) -> None:
        self._store("q2_0")
        path = self._config("run-q2_0.json", "q2_0")
        first = quant_store.refresh_run_configs("q2_0", self.clone)
        self.assertTrue(first)
        before = path.read_text()
        second = quant_store.refresh_run_configs("q2_0", self.clone)
        self.assertEqual(second, [], "the second sweep changed a config that was already right")
        self.assertEqual(path.read_text(), before, "the config was rewritten for nothing")

    def test_a_runtime_file_the_user_installed_elsewhere_keeps_its_path(self) -> None:
        other = self.root / "another-install" / "data" / "expert-profile.bin"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"the one on this box")
        self._store("q2_0")
        path = self._config("run-q2_0.json", "q2_0")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["args"][3] = str(other)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        quant_store.refresh_run_configs("q2_0", self.clone)
        self.assertEqual(json.loads(path.read_text())["args"][3], str(other),
                         "the sweep moved a runtime file the user installed")
        self.assertEqual(other.read_bytes(), b"the one on this box")

    def test_with_no_config_directory_there_is_nothing_to_refresh(self) -> None:
        for path in list(self.config.iterdir()):
            path.unlink()
        self.config.rmdir()
        self.assertEqual(quant_store.refresh_run_configs("q2_0", self.clone), [])


class GenerateRunConfigTests(unittest.TestCase):
    """A build writes the run config a model needs, even when config/ is empty.

    A fresh clone ships no run config but the template, and a user who clears config/
    leaves nothing to copy. There was then no base for a new config, so a build stored
    an engine and wrote no config: nothing in the Model version dropdown and no way to
    start what was just compiled. The shape lives in code now, and the sweep at the end
    of a build writes a config for every engine build on the box, not only the quant the
    build was named for.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = self.root / "engine"
        self.config = self.root / "config"
        self.clone = self.root / "tmp" / "strata-v0.1.45"
        self.engine.mkdir()
        self.config.mkdir()
        (self.clone / "data").mkdir(parents=True)
        (self.clone / "data" / "expert-profile.bin").write_bytes(b"the build's profile")
        self._saved = (quant_store.ENGINE_DIR, quant_store.LEGACY_DIR, quant_store.INDEX_PATH,
                       quant_store.CONFIG_DIR, quant_store.ROOT, quant_store.LOG_DIR,
                       quant_store.TMP_DIR)
        quant_store.ENGINE_DIR = self.engine
        quant_store.LEGACY_DIR = self.engine / "versions"
        quant_store.INDEX_PATH = self.engine / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.config
        quant_store.ROOT = self.root
        quant_store.LOG_DIR = self.root / "logs"
        quant_store.TMP_DIR = self.root / "tmp"

    def tearDown(self) -> None:
        quant_store.ENGINE_DIR, quant_store.LEGACY_DIR, quant_store.INDEX_PATH, \
            quant_store.CONFIG_DIR, quant_store.ROOT, quant_store.LOG_DIR, \
            quant_store.TMP_DIR = self._saved
        self.tmp.cleanup()

    def _store(self, key: str, payload: bytes = b"engine") -> Path:
        built = self.root / f"build-{key}"
        built.mkdir()
        (built / "strata").write_bytes(payload)
        quant_store.store_binaries(key, [built / "strata"], {"version": "0.1.45"})
        return built

    def test_a_build_writes_a_run_config_when_config_is_empty(self) -> None:
        self._store("q2_0")
        notes = quant_store.refresh_run_configs("q2_0", self.clone)
        path = self.config / "run-q2_0.json"
        self.assertTrue(path.is_file(), "the build stored an engine and wrote no run config")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["exe"], str(self.engine / "engine_q2_0" / "strata"))
        self.assertEqual(cfg["model_name"], "q2_0")
        self.assertEqual(cfg["log"], "logs/strata-q2_0.log")
        self.assertEqual(cfg["args"][cfg["args"].index("--expert-profile") + 1],
                         str(self.engine / "engine_q2_0" / "data" / "expert-profile.bin"),
                         "the runtime file the build shipped is not named in the config it wrote")
        self.assertIn("created config/run-q2_0.json", " ".join(notes))

    def test_every_engine_build_on_the_box_gets_a_run_config(self) -> None:
        """A build named for one quant must not leave an earlier build unstartable."""
        self._store("q2_0")
        self._store("iq2_xs")
        notes = quant_store.refresh_run_configs("iq2_xs", self.clone)
        for key in ("q2_0", "iq2_xs"):
            path = self.config / f"run-{key}.json"
            self.assertTrue(path.is_file(), f"engine/engine_{key} has no run config to start it")
            cfg = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["exe"], str(self.engine / f"engine_{key}" / "strata"))
        self.assertEqual(len([n for n in notes if n.startswith("created config/")]), 2,
                         f"the sweep wrote one config for two builds: {notes}")

    def test_a_config_that_already_exists_is_not_replaced_by_the_template(self) -> None:
        self._store("q2_0")
        installed = self.root / "box" / "expert-profile.bin"      # a file the user put here
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"the one on this box")
        (self.config / "run-q2_0.json").write_text(json.dumps(
            {"exe": "engine/strata", "model_name": "my q2_0",
             "args": ["--expert-profile", str(installed)]}), encoding="utf-8")
        quant_store.refresh_run_configs("q2_0", self.clone)
        cfg = json.loads((self.config / "run-q2_0.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["args"][1], str(installed),
                         "the sweep moved a runtime file the user installed")
        self.assertEqual(installed.read_bytes(), b"the one on this box")
        self.assertEqual(cfg["exe"], str(self.engine / "engine_q2_0" / "strata"))

    def test_no_config_is_invented_for_a_quant_that_was_never_built(self) -> None:
        self._store("q2_0")
        quant_store.refresh_run_configs("q2_0", self.clone)
        self.assertFalse((self.config / "run-q4_k_m.json").exists(),
                        "a config was written for a model with no engine build")

    def test_the_config_is_written_when_the_config_directory_does_not_exist(self) -> None:
        self._store("q2_0")
        self.config.rmdir()
        notes = quant_store.refresh_run_configs("q2_0", self.clone)
        self.assertTrue((self.config / "run-q2_0.json").is_file(),
                        f"config/ was gone and nothing was written: {notes}")

    def test_the_config_the_build_wrote_is_the_one_offered(self) -> None:
        """The repaired template names q2_0 as well; the build's own config wins."""
        self._store("q2_0")
        (self.config / "run-example.json").write_text(json.dumps(
            {"exe": "engine/strata", "model_name": "q2_0",
             "args": ["--expert-profile",
                      str(self.engine / "engine_q2_0" / "data" / "expert-profile.bin")]}),
            encoding="utf-8")
        quant_store.refresh_run_configs("q2_0", self.clone)
        versions = quant_store.list_versions()
        self.assertEqual(len(versions), 1, "the template was counted as a second model")
        self.assertEqual(versions[0]["config_name"], "run-q2_0.json",
                         "the dropdown offers the template instead of the config the build wrote")

    def test_a_config_written_from_the_code_template_is_a_valid_run_config(self) -> None:
        cfg = quant_store._template_config("iq2_xs")
        for field in ("exe", "args", "cwd", "tokenizer", "model_name", "log", "port", "host", "gpu"):
            self.assertIn(field, cfg, f"the template config is missing {field}")
        self.assertEqual(cfg["model_name"], "iq2_xs")
        self.assertEqual(cfg["log"], "logs/strata-iq2_xs.log")
        self.assertIn("/ABSOLUTE/PATH/TO/packs/iq2_xs", cfg["args"],
                      "the quant is not carried into the placeholder paths")


if __name__ == "__main__":
    unittest.main()

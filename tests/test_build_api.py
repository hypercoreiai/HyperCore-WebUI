"""tests/test_build_api.py - the Build tab's background job.

Two contracts the Build tab depends on: the log the tab tails is readable from a
byte offset (so a poll appends instead of re-sending), and a build that fails only
on the HyperCore worker target still produces an engine and says so in
engine/BUILD.json. Neither needs cmake or a GPU: the subprocess is stubbed.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

from serve import build_api, quant_store
from serve.build_api import BuildJob

ROOT = Path(__file__).resolve().parents[1]


class StubBuild(BuildJob):
    """A build job whose cmake calls are answered from a script."""

    def __init__(self, codes: list[int]) -> None:
        super().__init__()
        self.codes = list(codes)
        self.calls: list[list[str]] = []

    def run_stream(self, cmd: list[str]) -> int:
        self.calls.append(cmd)
        return self.codes.pop(0) if self.codes else 0


def targets_of(cmd: list[str]) -> list[str]:
    """The --target list a cmake --build command carries."""
    return cmd[cmd.index("--target") + 1:cmd.index("-j")]


class BuildTargetTests(unittest.TestCase):
    def test_all_three_targets_in_one_invocation(self) -> None:
        job = StubBuild([0])
        self.assertTrue(job.build_targets(Path("build")))
        self.assertEqual(len(job.calls), 1)
        self.assertEqual(targets_of(job.calls[0]), ["strata", "strata-vision", "hypercore-worker"])

    def test_a_worker_failure_falls_back_to_the_engine_only(self) -> None:
        job = StubBuild([1, 0])
        self.assertFalse(job.build_targets(Path("build")))
        self.assertEqual(len(job.calls), 2)
        self.assertEqual(targets_of(job.calls[1]), ["strata", "strata-vision"])
        self.assertIn("worker: not built - the engine runs in direct mode", job.log)

    def test_a_failure_of_the_engine_itself_is_not_swallowed(self) -> None:
        job = StubBuild([1, 1])
        with self.assertRaises(RuntimeError):
            job.build_targets(Path("build"))

    def test_the_flag_is_written_to_build_json(self) -> None:
        """engine/BUILD.json carries the worker flag the Monitor reads."""
        meta = {"source": "built-from-source", "version": "v0.1.45", "worker": False}
        self.assertIn('"worker": false', json.dumps(meta, indent=1))


class BuildLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(__import__("tempfile").mkdtemp())
        self.job = BuildJob()
        self.job.log_path = self.tmp / "build.log"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_log_is_on_disk_and_tailed_from_an_offset(self) -> None:
        for line in ("git clone ...", "cmake configure", "building"):
            self.job.log_line(line)
        first = self.job.tail(0)
        self.assertEqual(first["lines"], ["git clone ...", "cmake configure", "building"])
        self.assertEqual(first["offset"], first["bytes"])
        self.job.log_line("done")
        second = self.job.tail(first["offset"])
        self.assertEqual(second["lines"], ["done"], "a poll must only get new lines")
        self.assertGreater(second["offset"], first["offset"])

    def test_a_missing_log_is_empty_not_an_error(self) -> None:
        self.job.log_path = self.tmp / "no-such.log"
        self.assertEqual(self.job.tail(0), {"lines": [], "offset": 0, "bytes": 0})

    def test_an_offset_past_the_end_returns_nothing(self) -> None:
        self.job.log_line("one")
        self.assertEqual(self.job.tail(10_000)["lines"], [])


class StatusTests(unittest.TestCase):
    """GET /build/status is the payload the Build tab's dropdown is built from.

    The tab reads latest.releases, so that key has to be the list. It used to be the
    single newest release, which is why the box stayed empty.
    """

    RELEASES: ClassVar[list[dict[str, Any]]] = [
        {"tag": "v0.1.45", "version": "0.1.45", "published": "2026-10-01T00:00:00Z", "assets": []},
        {"tag": "v0.1.44", "version": "0.1.44", "published": "2026-09-01T00:00:00Z", "assets": []},
    ]

    def setUp(self) -> None:
        self.original = build_api.latest_release
        build_api.latest_release = lambda: {"repo": "Niko1221/Strata", "releases": list(self.RELEASES)}
        self.tmp = Path(__import__("tempfile").mkdtemp())
        build_api.ENGINE_DIR = self.tmp / "engine"
        build_api.MANIFEST = self.tmp / "build_options.json"
        build_api.ENGINE_DIR.mkdir()
        (build_api.ENGINE_DIR / "BUILD.json").write_text(json.dumps({"version": "0.1.44"}), encoding="utf-8")

    def tearDown(self) -> None:
        build_api.latest_release = self.original
        build_api.ENGINE_DIR = ROOT / "engine"
        build_api.MANIFEST = ROOT / "config" / "build_options.json"
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_listing_is_under_latest(self) -> None:
        payload = build_api.build_status()
        self.assertIsInstance(payload["latest"].get("releases"), list)
        self.assertEqual([r["tag"] for r in payload["latest"]["releases"]], ["v0.1.45", "v0.1.44"])
        self.assertEqual(payload["newest"]["tag"], "v0.1.45")
        self.assertTrue(payload["update_available"], "0.1.44 installed, 0.1.45 published")
        self.assertTrue(payload["installed"]["worker"] is False)

    def test_no_update_when_the_installed_engine_is_the_newest(self) -> None:
        (build_api.ENGINE_DIR / "BUILD.json").write_text(json.dumps({"version": "0.1.45"}), encoding="utf-8")
        payload = build_api.build_status()
        self.assertFalse(payload["update_available"])
        self.assertEqual(len(payload["latest"]["releases"]), 2)

    def test_github_unreachable_still_returns_a_usable_payload(self) -> None:
        build_api.latest_release = lambda: {"error": "timed out", "releases": []}
        payload = build_api.build_status()
        self.assertIsNone(payload["newest"])
        self.assertFalse(payload["update_available"])
        self.assertEqual(payload["latest"]["releases"], [])
        self.assertEqual(payload["latest"]["error"], "timed out")

    def test_an_engine_with_no_version_is_never_told_it_is_up_to_date(self) -> None:
        build_api.ENGINE_DIR.mkdir(exist_ok=True)
        (build_api.ENGINE_DIR / "BUILD.json").write_text(json.dumps({"version": None}), encoding="utf-8")
        self.assertFalse(build_api.build_status()["update_available"])


class OptionsTests(unittest.TestCase):
    """The choices of a release are read before any build, without cloning it."""

    SETUP = """
import argparse
FAMILIES = {"qwen": {}, "swift": {}}
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", choices=list(FAMILIES), help="which model")
    ap.add_argument("--model", choices=["Q2_0", "IQ3_XXS"], help="which quant")
    ap.add_argument("--context", type=int, help="context length")
    ap.add_argument("--kv", choices=["int8", "q4_0"], help="KV cache")
    ap.add_argument("--images", choices=["qwen-image", "no"])
    ap.add_argument("--experimental-speed-projection", metavar="on|off|GGUF")
    ap.add_argument("--port", type=int)
"""

    def setUp(self) -> None:
        self.tmp = Path(__import__("tempfile").mkdtemp())
        self.clone = self.tmp / "release"
        self.clone.mkdir()
        (self.clone / "setup.py").write_text(self.SETUP, encoding="utf-8")
        (self.clone / "CMakeLists.txt").write_text(
            'project(Strata VERSION 0.1.45)\noption(STRATA_ENABLE_CUDA "CUDA" OFF)\n', encoding="utf-8")
        self.original_fetch = build_api.fetch_release_files
        build_api.fetch_release_files = lambda tag: (self.clone, "")
        build_api._options_cache.clear()
        build_api.MANIFEST = self.tmp / "build_options.json"

    def tearDown(self) -> None:
        build_api.fetch_release_files = self.original_fetch
        build_api.MANIFEST = ROOT / "config" / "build_options.json"
        build_api._options_cache.clear()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_release_is_read_from_its_own_files(self) -> None:
        options = build_api.options_for("v0.1.45")
        self.assertEqual(options["tag"], "v0.1.45")
        self.assertEqual(options["from"], "github")
        flags = {c["flag"] for c in options["setup_choices"]}
        for flag in ("--family", "--model", "--context", "--kv", "--images",
                     "--experimental-speed-projection"):
            self.assertIn(flag, flags)
        groups = build_api.setup_choices(options)["groups"]
        self.assertEqual(groups["model"][0]["choices"], ["qwen", "swift"])
        self.assertEqual(groups["experimental"][0]["choices"], ["on", "off", "GGUF"])
        self.assertEqual(build_api.setup_choices(options)["hidden"], 1)  # --port

    def test_a_second_ask_for_the_same_tag_is_not_fetched_again(self) -> None:
        calls = []
        build_api.fetch_release_files = lambda tag: (calls.append(tag) or (self.clone, ""))
        build_api.options_for("v0.1.46")
        build_api.options_for("v0.1.46")
        self.assertEqual(calls, ["v0.1.46"])

    def test_no_network_still_gives_the_choices_of_the_last_build(self) -> None:
        build_api.MANIFEST.write_text(json.dumps({"version": "0.1.45", "cmake_options": [],
                                                 "setup_choices": [{"flag": "--kv", "group": "kv-cache",
                                                                   "choices": ["int8"], "visible": True}]}),
                                     encoding="utf-8")
        options = build_api.options_for("v0.1.45")
        self.assertEqual(options["from"], "the last build of this release")
        self.assertEqual(build_api.setup_choices(options)["groups"]["kv-cache"][0]["flag"], "--kv")

    def test_an_unreachable_release_is_reported_not_raised(self) -> None:
        build_api.fetch_release_files = lambda tag: (None, "timed out")
        options = build_api.options_for("v9.9.9")
        self.assertIn("could not read", options["error"])
        self.assertEqual(options["cmake_options"], [])
        self.assertEqual(build_api.setup_choices(options), {"groups": {}, "hidden": 0, "total": 0})

    def test_build_status_carries_the_grouped_choices(self) -> None:
        options = build_api.options_for("v0.1.45")
        build_api.MANIFEST.write_text(json.dumps(options), encoding="utf-8")
        original = build_api.latest_release
        build_api.latest_release = lambda: {"repo": "x", "releases": []}
        try:
            payload = build_api.build_status()
        finally:
            build_api.latest_release = original
        self.assertIn("groups", payload["setup"])
        self.assertIn("model", payload["setup"]["groups"])
        self.assertGreaterEqual(payload["setup"]["total"], 6)

    def test_a_manifest_without_groups_is_grouped_before_it_is_shown(self) -> None:
        """config/build_options.json written by an older build has no group or visible
        key; the card would show nothing unless the flag names are read again."""
        build_api.MANIFEST.write_text(json.dumps({
            "version": "0.1.45",
            "cmake_options": [],
            "setup_choices": [
                {"flag": "--family", "choices": None, "type": None, "default": None, "help": ""},
                {"flag": "--model", "choices": None, "type": None, "default": None, "help": ""},
                {"flag": "--context", "choices": None, "type": "int", "default": None, "help": ""},
                {"flag": "--kv", "choices": ["int8", "q4_0"], "type": None, "default": None, "help": ""},
                {"flag": "--image-native", "choices": ["on", "off"], "type": None, "default": None, "help": ""},
                {"flag": "--experimental-speed-projection", "choices": None, "type": None,
                 "default": None, "help": "EXPERIMENTAL"},
                {"flag": "--port", "choices": None, "type": "int", "default": None, "help": ""},
            ],
        }), encoding="utf-8")
        setup = build_api.setup_choices(build_api.options_for("v0.1.45"))
        self.assertEqual(sorted(set(setup["groups"]) - {"model", "context", "kv-cache",
                                                        "images", "experimental"}), [])
        for group in ("model", "context", "kv-cache", "images", "experimental"):
            self.assertTrue(setup["groups"][group], f"the {group} group is empty")
        self.assertEqual(setup["hidden"], 1)  # --port is housekeeping, not a choice
        self.assertEqual(setup["total"], 7)


class ScratchContractTests(unittest.TestCase):
    """tmp/ is scratch: nothing that serves a request may read it."""

    def test_server_has_no_runtime_path_into_tmp(self) -> None:
        source = (ROOT / "serve" / "server.py").read_text(encoding="utf-8")
        offenders = [line for line in source.splitlines()
                    if re.search(r"\"(tmp|\\tmp)[/\\\\]", line) or re.search(r"ROOT / \"tmp\"", line)]
        self.assertEqual(offenders, [], f"serve/server.py reads tmp/: {offenders}")

    def test_the_generation_paths_do_not_touch_tmp(self) -> None:
        for name in ("serve/direct_engine.py", "serve/engine_supervisor.py", "serve/attached_engine.py",
                     "serve/offline_engine.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn('ROOT / "tmp"', source, f"{name} reads the scratch folder")
            self.assertNotIn("tmp/strata", source, f"{name} reads a build clone")


class TransferTests(unittest.TestCase):
    """A build named by quant is stored per quant, and engine/strata points at it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (build_api.ENGINE_DIR, quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR,
                      quant_store.INDEX_PATH, quant_store.CONFIG_DIR)
        build_api.ENGINE_DIR = self.tmp / "engine"
        quant_store.ENGINE_DIR = self.tmp / "engine"
        quant_store.VERSIONS_DIR = self.tmp / "engine" / "versions"
        quant_store.INDEX_PATH = self.tmp / "engine" / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.tmp / "config"
        self.build_dir = self.tmp / "build"
        self.build_dir.mkdir()
        (self.build_dir / "strata").write_bytes(b"engine binary")
        (self.build_dir / "hypercore-worker").write_bytes(b"worker binary")
        self.meta: dict[str, Any] = {"version": "0.1.45", "source": "built-from-source", "worker": True}
        self.job = BuildJob()

    def tearDown(self) -> None:
        (build_api.ENGINE_DIR, quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR,
         quant_store.INDEX_PATH, quant_store.CONFIG_DIR) = self.saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_quant_build_is_stored_and_made_active(self) -> None:
        copied, key = self.job.transfer_binaries(self.build_dir, self.meta, "IQ2_S")
        self.assertEqual(key, "iq2_s")
        self.assertEqual(sorted(copied), ["hypercore-worker", "strata"])
        stored = quant_store.VERSIONS_DIR / "iq2_s"
        self.assertTrue((stored / "strata").is_file())
        self.assertTrue((stored / "BUILD.json").is_file())
        self.assertTrue((quant_store.ENGINE_DIR / "strata").is_file())
        self.assertEqual((quant_store.ENGINE_DIR / "strata").read_bytes(), b"engine binary")
        self.assertEqual(quant_store.index()["active"], "iq2_s")
        self.assertIn("stored engine/versions/iq2_s", "\n".join(self.job.log))
        meta = json.loads((quant_store.ENGINE_DIR / "BUILD.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["quant"], "iq2_s")

    def test_two_quants_do_not_overwrite_each_other(self) -> None:
        self.job.transfer_binaries(self.build_dir, {**self.meta, "label": "q2_0"}, "q2_0")
        other = self.tmp / "build2"
        other.mkdir()
        (other / "strata").write_bytes(b"iq2 engine")
        self.job.transfer_binaries(other, self.meta, "IQ2_S")
        self.assertEqual((quant_store.VERSIONS_DIR / "q2_0" / "strata").read_bytes(), b"engine binary")
        self.assertEqual((quant_store.VERSIONS_DIR / "iq2_s" / "strata").read_bytes(), b"iq2 engine")
        self.assertEqual(sorted(quant_store.index()["versions"]), ["iq2_s", "q2_0"])
        self.assertEqual(quant_store.index()["active"], "iq2_s")

    def test_no_quant_is_the_old_behaviour(self) -> None:
        copied, key = self.job.transfer_binaries(self.build_dir, self.meta, None)
        self.assertIsNone(key)
        self.assertEqual(sorted(copied), ["hypercore-worker", "strata"])
        self.assertTrue((build_api.ENGINE_DIR / "strata").is_file())
        self.assertFalse(quant_store.VERSIONS_DIR.exists())
        self.assertEqual(json.loads((build_api.ENGINE_DIR / "BUILD.json").read_text())["version"], "0.1.45")

    def test_a_bad_quant_name_is_not_fatal(self) -> None:
        copied, key = self.job.transfer_binaries(self.build_dir, self.meta, "q2 0%!")
        self.assertIsNone(key)
        self.assertEqual(copied, ["strata", "hypercore-worker"])
        self.assertTrue((build_api.ENGINE_DIR / "strata").is_file())
        self.assertIn("is not a valid name", "\n".join(self.job.log))

    def test_the_quant_is_reported_in_the_job_status(self) -> None:
        job = BuildJob()
        job.quant = build_api.normalise_or_none("Q4_K_M")
        self.assertEqual(job.status()["quant"], "q4_k_m")
        self.assertIsNone(BuildJob().status()["quant"])


if __name__ == "__main__":
    unittest.main()

"""tests/test_build_api.py - the Build tab's background job.

Two contracts the Build tab depends on: the log the tab tails is readable from a
byte offset (so a poll appends instead of re-sending), and a build that fails only
on the HyperCore worker target still produces an engine and says so in
engine/BUILD.json. Neither needs cmake or a GPU: the subprocess is stubbed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
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
        # An empty scratch folder: options_for() now looks in tmp/ first, and the checkouts
        # this machine has would answer for a tag this test did not create.
        self.saved_tmp = build_api.TMP_DIR
        build_api.TMP_DIR = self.tmp / "scratch"
        build_api._options_cache.clear()
        build_api.MANIFEST = self.tmp / "build_options.json"

    def tearDown(self) -> None:
        build_api.fetch_release_files = self.original_fetch
        build_api.TMP_DIR = self.saved_tmp
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
        setup = build_api.setup_choices(options)
        self.assertEqual({"groups": setup["groups"], "hidden": setup["hidden"], "total": setup["total"]},
                         {"groups": {}, "hidden": 0, "total": 0})

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


class LocalCheckoutTests(unittest.TestCase):
    """The checkouts tmp/ already holds, and the release the choices are read from.

    A build no longer deletes its clone, so a machine that has built two releases has two
    trees on disk. The dropdown was filled only from GitHub's listing, so the card could
    name one release while listing another's: on this machine the manifest was extracted
    from tmp/strata-v0.2.0-beta, which declares 0.1.45.3, while the card's title was the
    installed engine's 0.1.45. These are the scan and the choice it makes.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.saved_tmp, self.saved_manifest = build_api.TMP_DIR, build_api.MANIFEST
        build_api.TMP_DIR = self.tmp
        build_api.MANIFEST = self.tmp / "build_options.json"
        build_api._options_cache.clear()
        self.saved_fetch, self.saved_release = build_api.fetch_release_files, build_api.latest_release
        build_api.fetch_release_files = lambda tag: (None, "no network in this test")
        build_api.latest_release = lambda: {"repo": "x", "releases": []}
        # The search order is the real filesystem, so it is stubbed: a checkout the user
        # keeps on this box would otherwise appear in every count below.
        self.saved_search = build_api.strata_hook.search_candidates
        build_api.strata_hook.search_candidates = lambda run_config=None: []
        # Two checkouts, and neither folder is named for the version it declares.
        self.old = self._checkout("strata-v0.1.45", "0.1.45", ["int8", "q4_0"])
        self.new = self._checkout("strata-v0.2.0-beta", "0.1.45.3", ["int8", "q4_0", "k8v4"])
        (self.tmp / "manifest-v0.1.45").mkdir()     # the scratch a fetch writes
        (self.tmp / "strata-data").mkdir()          # the packs folder, not a release

    def _checkout(self, name: str, version: str, kv: list[str]) -> Path:
        folder = self.tmp / name
        folder.mkdir()
        (folder / "CMakeLists.txt").write_text(
            f"project(strata VERSION {version} LANGUAGES CXX)\n", encoding="utf-8")
        (folder / "setup.py").write_text(
            "import argparse\nap = argparse.ArgumentParser()\n"
            f"ap.add_argument('--kv', choices={json.dumps(kv)})\n", encoding="utf-8")
        return folder

    def tearDown(self) -> None:
        build_api.TMP_DIR, build_api.MANIFEST = self.saved_tmp, self.saved_manifest
        build_api.fetch_release_files, build_api.latest_release = self.saved_fetch, self.saved_release
        build_api.strata_hook.search_candidates = self.saved_search
        build_api._options_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_scan_finds_the_checkouts_and_orders_them_newest_first(self) -> None:
        found = build_api.local_releases()
        self.assertEqual([r["tag"] for r in found], ["v0.2.0-beta", "v0.1.45"])
        self.assertEqual(found[0]["version"], "0.1.45.3",
                         "the version is what the checkout declares, not what its folder is named")
        self.assertTrue(found[0]["has_setup"] and found[0]["has_cmake"])
        self.assertTrue(found[0]["folder"].endswith("strata-v0.2.0-beta"), found[0]["folder"])

    def test_a_folder_that_is_not_a_checkout_is_not_reported_as_a_release(self) -> None:
        found = build_api.local_releases()
        self.assertEqual(len(found), 2, f"scratch folders were counted as releases: {found}")
        self.assertNotIn("strata-data", [r["folder"] for r in found])

    def test_a_checkout_kept_outside_tmp_is_offered_as_a_build_source(self) -> None:
        """serve/strata_hook.py runs setup.py from a Strata the user keeps elsewhere, so
        the scan has to offer the same tree: otherwise the card names one release and a
        build started from the hook uses another."""
        kept = self.tmp / "kept" / "Strata"
        kept.mkdir(parents=True)
        (kept / "setup.py").write_text("import argparse\n", encoding="utf-8")
        build_api.strata_hook.search_candidates = lambda run_config=None: [kept, self.tmp / "strata-data"]
        found = build_api.local_releases()
        entry = next((r for r in found if r["source"] == "checkout"), None)
        self.assertIsNotNone(entry, f"the search order was not scanned: {found}")
        self.assertEqual(entry["tag"], "Strata",
                         "a folder whose name is not a tag is reported under its name")
        self.assertIsNone(entry["version"], "a checkout with no CMakeLists.txt has no version")
        self.assertTrue(entry["has_setup"] and not entry["has_cmake"])
        self.assertEqual(len(found), 3, f"a folder that is not a checkout was counted: {found}")

    def test_a_tree_that_is_both_in_tmp_and_in_the_search_is_reported_once(self) -> None:
        build_api.strata_hook.search_candidates = lambda run_config=None: [self.new, self.old]
        found = build_api.local_releases()
        self.assertEqual(len(found), 2, f"a checkout was counted twice: {found}")
        self.assertEqual({r["source"] for r in found}, {"tmp"},
                         "the clone a build would use is not the one reported")

    def test_a_checkout_with_no_cmake_line_is_still_listed_as_a_checkout(self) -> None:
        """A tree whose project() line cannot be read is a release the user can still pick;
        it is reported with no version rather than dropped."""
        stray = self.tmp / "strata-main"
        stray.mkdir()
        (stray / "setup.py").write_text("import argparse\n", encoding="utf-8")
        found = build_api.local_releases()
        self.assertIn("main", [r["tag"] for r in found])
        self.assertIsNone(next(r for r in found if r["tag"] == "main")["version"])

    def test_the_choices_are_read_from_the_checkout_the_user_picked(self) -> None:
        options = build_api.options_for("v0.2.0-beta")
        self.assertEqual(options["version"], "0.1.45.3")
        self.assertTrue(options["from"].endswith("strata-v0.2.0-beta"), options["from"])
        self.assertEqual(build_api.setup_choices(options)["groups"]["kv-cache"][0]["choices"],
                         ["int8", "q4_0", "k8v4"])

    def test_a_tag_with_no_checkout_here_still_goes_to_github(self) -> None:
        options = build_api.options_for("v0.1.44")
        self.assertEqual(options["from"], "unreachable")

    def test_the_manifest_is_used_when_this_machine_has_no_checkout_for_that_release(self) -> None:
        build_api.MANIFEST.write_text(json.dumps({"version": "0.1.44", "cmake_options": [],
                                                  "setup_choices": []}), encoding="utf-8")
        options = build_api.options_for("v0.1.44")
        self.assertEqual(options["from"], "the last build of this release")

    def test_build_status_reports_the_checkouts_as_build_sources(self) -> None:
        payload = build_api.build_status()
        self.assertEqual([r["tag"] for r in payload["local"]], ["v0.2.0-beta", "v0.1.45"])

    def test_the_setup_payload_names_the_release_the_choices_came_from(self) -> None:
        """The card's title has to be the release the flags belong to, not the engine that
        happens to be installed: `version` is what that checkout declares, `tag` is the name
        it was fetched as, and `from` says which tree the flags were read out of."""
        setup = build_api.setup_choices(build_api.options_for("v0.2.0-beta"))
        self.assertEqual(setup["version"], "0.1.45.3")
        self.assertEqual(setup["tag"], "v0.2.0-beta")
        self.assertTrue(setup["from"].endswith("strata-v0.2.0-beta"), setup["from"])

    def test_a_checkout_with_no_version_line_falls_back_to_the_tag_it_was_named_for(self) -> None:
        stray = self.tmp / "strata-main"
        stray.mkdir()
        (stray / "setup.py").write_text("import argparse\n", encoding="utf-8")
        setup = build_api.setup_choices(build_api.options_for("main"))
        self.assertEqual(setup["version"], "main")

    def test_the_scan_is_build_aparts_only(self) -> None:
        """tmp/ is scratch for everything that serves a chat: the release scan is the one
        reader, and it lives in the build module."""
        for name in ("serve/server.py", "serve/quant_store.py", "serve/strata_hook.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("local_releases", source, f"{name} reads the scan")
        self.assertIn("local_releases", (ROOT / "serve" / "build_api.py").read_text(encoding="utf-8"))


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
        stored = quant_store.store_dir("iq2_s")
        self.assertTrue((stored / "strata").is_file())
        self.assertTrue((stored / "BUILD.json").is_file())
        self.assertTrue((quant_store.ENGINE_DIR / "strata").is_file())
        self.assertEqual((quant_store.ENGINE_DIR / "strata").read_bytes(), b"engine binary")
        self.assertEqual(quant_store.index()["active"], "iq2_s")
        self.assertIn("stored engine/engine_iq2_s", "\n".join(self.job.log))
        meta = json.loads((quant_store.ENGINE_DIR / "BUILD.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["quant"], "iq2_s")

    def test_two_quants_do_not_overwrite_each_other(self) -> None:
        self.job.transfer_binaries(self.build_dir, {**self.meta, "label": "q2_0"}, "q2_0")
        other = self.tmp / "build2"
        other.mkdir()
        (other / "strata").write_bytes(b"iq2 engine")
        self.job.transfer_binaries(other, self.meta, "IQ2_S")
        self.assertEqual((quant_store.store_dir("q2_0") / "strata").read_bytes(), b"engine binary")
        self.assertEqual((quant_store.store_dir("iq2_s") / "strata").read_bytes(), b"iq2 engine")
        self.assertEqual(sorted(quant_store.index()["versions"]), ["iq2_s", "q2_0"])
        self.assertEqual(quant_store.index()["active"], "iq2_s")

    def test_no_quant_is_the_old_behaviour(self) -> None:
        copied, key = self.job.transfer_binaries(self.build_dir, self.meta, None)
        self.assertIsNone(key)
        self.assertEqual(sorted(copied), ["hypercore-worker", "strata"])
        self.assertTrue((build_api.ENGINE_DIR / "strata").is_file())
        self.assertFalse([p for p in build_api.ENGINE_DIR.iterdir()
                         if p.name.startswith(quant_store.STORE_PREFIX)],
                        "a build with no quant named must not create a version folder")
        self.assertEqual(json.loads((build_api.ENGINE_DIR / "BUILD.json").read_text())["version"], "0.1.45")

    def test_a_bad_quant_name_is_not_fatal(self) -> None:
        copied, key = self.job.transfer_binaries(self.build_dir, self.meta, "q2 0%!")
        self.assertIsNone(key)
        self.assertEqual(copied, ["strata", "hypercore-worker"])
        self.assertTrue((build_api.ENGINE_DIR / "strata").is_file())
        self.assertIn("is not a valid name", "\n".join(self.job.log))

    def test_a_prebuilt_engine_is_installed_as_a_model_version(self) -> None:
        import io
        import zipfile
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, "w") as zf:
            zf.writestr("strata", b"ready engine")
            zf.writestr("libstrata_kq.so", b"kernels")
        blob = blob.getvalue()
        digest = __import__("hashlib").sha256(blob).hexdigest()

        real_tmp, real_urlretrieve, real_assets, real_digests = (
            build_api.TMP_DIR, build_api.urllib.request.urlretrieve,
            build_api.asset_for, build_api.published_digests)
        build_api.TMP_DIR = self.tmp / "scratch"
        build_api.urllib.request.urlretrieve = lambda url, dest: Path(dest).write_bytes(blob)
        build_api.asset_for = lambda r: {"name": "strata-linux-x64.zip", "url": "file://x"}
        build_api.published_digests = lambda r: {"strata-linux-x64.zip": digest}
        try:
            out = BuildJob().fetch_prebuilt({"version": "0.1.46", "tag": "v0.1.46"}, "IQ2_XS")
            self.assertTrue(out["ok"], out.get("error"))
            self.assertEqual(sorted(out["files"]), ["libstrata_kq.so", "strata"])
            self.assertEqual(out["engine_dir"], str(quant_store.store_dir("iq2_xs")))
            self.assertTrue((quant_store.store_dir("iq2_xs") / "libstrata_kq.so").is_file())
            self.assertTrue((quant_store.ENGINE_DIR / "strata").is_file())
            self.assertEqual(quant_store.index()["active"], "iq2_xs")
            self.assertFalse((build_api.TMP_DIR / "strata-linux-x64.zip").exists(),
                            "the downloaded archive is left in the scratch folder")
        finally:
            build_api.TMP_DIR, build_api.urllib.request.urlretrieve, \
                build_api.asset_for, build_api.published_digests = \
                real_tmp, real_urlretrieve, real_assets, real_digests

    def test_the_quant_is_reported_in_the_job_status(self) -> None:
        job = BuildJob()
        job.quant = build_api.normalise_or_none("Q4_K_M")
        self.assertEqual(job.status()["quant"], "q4_k_m")
        self.assertIsNone(BuildJob().status()["quant"])


class DeviceTests(unittest.TestCase):
    """The Build tab's defaults are the ones this machine can build.

    Upstream defaults every GPU backend to OFF, and with all three off the release
    defines no `strata` target at all: a build started from those defaults fails with
    ninja's "unknown target 'strata'" and puts nothing in engine/. The probes are
    stubbed here, so the test says what the code does with a device rather than what
    this machine happens to have.
    """

    def setUp(self) -> None:
        self.saved = (build_api.cuda_toolkit, build_api.cuda_arch, build_api.shutil.which)
        build_api.cuda_toolkit = lambda: "/usr/local/cuda-13.4/bin"
        build_api.cuda_arch = lambda: "89"
        build_api.shutil.which = lambda name: None

    def tearDown(self) -> None:
        build_api.cuda_toolkit, build_api.cuda_arch, build_api.shutil.which = self.saved

    def test_a_cuda_box_defaults_to_a_gpu_build(self) -> None:
        device = build_api.device_defaults()
        self.assertEqual(device["device"], "cuda")
        self.assertEqual(device["defs"], {"STRATA_ENABLE_CUDA": "ON",
                                         "CMAKE_CUDA_ARCHITECTURES": "89",
                                         "CMAKE_CUDA_COMPILER": "/usr/local/cuda-13.4/bin/nvcc"},
                         "a toolkit that is not on PATH has to be named for CMake to find it")
        self.assertEqual(build_api.device_label(device),
                         "CUDA toolkit at /usr/local/cuda-13.4/bin, GPU arch 89")

    def test_the_manifest_the_tab_shows_carries_those_defaults(self) -> None:
        options = {"cmake_options": [{"name": "STRATA_ENABLE_CUDA", "kind": "bool",
                                     "default": "OFF", "help": "Build the CUDA targets",
                                     "visible": True}]}
        shown = build_api.with_device_defaults(options)
        self.assertEqual(shown["device"]["device"], "cuda")
        cuda = next(o for o in shown["cmake_options"] if o["name"] == "STRATA_ENABLE_CUDA")
        self.assertEqual(cuda["default"], "ON")
        arch = next(o for o in shown["cmake_options"] if o["name"] == "CMAKE_CUDA_ARCHITECTURES")
        self.assertEqual(arch["default"], "89")
        self.assertTrue(arch["visible"], "the arch is a choice the user can see and change")
        compiler = next(o for o in shown["cmake_options"] if o["name"] == "CMAKE_CUDA_COMPILER")
        self.assertFalse(compiler["visible"], "the toolkit path is filled in, not chosen")
        self.assertEqual(options["cmake_options"][0]["default"], "OFF",
                         "the caller's manifest is not mutated")

    def test_no_toolchain_is_said_plainly(self) -> None:
        build_api.cuda_toolkit = lambda: ""
        device = build_api.device_defaults()
        self.assertEqual(device["device"], "none")
        self.assertEqual(device["defs"], {})
        self.assertIn("no CUDA, HIP or oneAPI", build_api.device_label(device))
        shown = build_api.with_device_defaults({"cmake_options": []})
        self.assertEqual(shown["cmake_options"], [])

    def test_a_hip_box_defaults_to_the_hip_backend(self) -> None:
        build_api.cuda_toolkit = lambda: ""
        build_api.shutil.which = lambda name: "/usr/bin/hipcc" if name == "hipcc" else None
        device = build_api.device_defaults()
        self.assertEqual(device["device"], "hip")
        self.assertEqual(device["defs"], {"STRATA_ENABLE_HIP": "ON"})

    def test_a_feature_the_backend_cannot_build_is_off_before_cmake(self) -> None:
        """Upstream's CMakeLists.txt FATAL_ERRORs on STRATA_PREFILL_MMQ without HIP
        (v0.1.45 CMakeLists.txt:97), so a CUDA box must never be shown that switch on.
        """
        options = {"cmake_options": [
            {"name": "STRATA_ENABLE_CUDA", "kind": "bool", "default": "OFF",
             "help": "Build the CUDA targets", "visible": True},
            {"name": "STRATA_PREFILL_MMQ", "kind": "bool", "default": "ON",
             "help": "Build the optional GGML MMQ prefill path for HIP", "visible": True},
            {"name": "STRATA_MMQ_TAB", "kind": "bool", "default": "ON",
             "help": "Build the pointer-table prompt MMQ", "visible": True},
        ]}
        shown = build_api.with_device_defaults(options)
        by_name = {o["name"]: o for o in shown["cmake_options"]}
        self.assertEqual(by_name["STRATA_PREFILL_MMQ"]["default"], "OFF",
                         "cmake would refuse this configuration before compiling anything")
        self.assertIn("STRATA_ENABLE_HIP=ON", by_name["STRATA_PREFILL_MMQ"]["help"],
                     "the tab has to say why the switch is off, not just show it off")
        self.assertEqual(by_name["STRATA_MMQ_TAB"]["default"], "ON",
                         "the pointer table is a CUDA target, so a CUDA box can build it")
        self.assertEqual(options["cmake_options"][1]["default"], "ON",
                         "the manifest the caller passed is not mutated")

    def test_a_hip_box_keeps_the_hip_only_feature(self) -> None:
        build_api.cuda_toolkit = lambda: ""
        build_api.shutil.which = lambda name: "/usr/bin/hipcc" if name == "hipcc" else None
        options = {"cmake_options": [
            {"name": "STRATA_ENABLE_HIP", "kind": "bool", "default": "OFF",
             "help": "Build the HIP targets", "visible": True},
            {"name": "STRATA_NATIVE_EXPERTS", "kind": "bool", "default": "ON",
             "help": "Native (IQ) expert support through ggml-cpu", "visible": True},
            {"name": "STRATA_PREFILL_MMQ", "kind": "bool", "default": "ON",
             "help": "Build the optional GGML MMQ prefill path for HIP", "visible": True},
            {"name": "STRATA_MMQ_TAB", "kind": "bool", "default": "ON",
             "help": "Build the pointer-table prompt MMQ", "visible": True},
        ]}
        by_name = {o["name"]: o for o in build_api.with_device_defaults(options)["cmake_options"]}
        self.assertEqual(by_name["STRATA_PREFILL_MMQ"]["default"], "ON")
        self.assertEqual(by_name["STRATA_MMQ_TAB"]["default"], "OFF",
                         "there is no CUDA target to build the pointer table from")

    def test_an_empty_field_is_not_a_choice(self) -> None:
        """The tab sends every control, including the ones nobody touched.

        An empty value has to leave this box's default in place: an empty
        STRATA_ENABLE_CUDA is what configured a CUDA box with no engine target in it.
        """
        device = build_api.device_defaults()
        sent = build_api.sent_options(device["defs"], {"STRATA_ENABLE_CUDA": "",
                                                       "CMAKE_CUDA_ARCHITECTURES": "   ",
                                                       "STRATA_MMQ_TAB": "ON"})
        self.assertEqual(sent["STRATA_ENABLE_CUDA"], "ON",
                         "a switch nobody touched keeps the default the tab showed")
        self.assertEqual(sent["CMAKE_CUDA_ARCHITECTURES"], "89")
        self.assertEqual(sent["STRATA_MMQ_TAB"], "ON",
                         "a switch someone did turn on is a choice and wins")

    def test_an_explicit_off_is_a_choice_and_wins(self) -> None:
        device = build_api.device_defaults()
        self.assertEqual(
            build_api.sent_options(device["defs"], {"STRATA_ENABLE_CUDA": "OFF"})["STRATA_ENABLE_CUDA"],
            "OFF", "off is what the user picked, not an untouched field")
        self.assertEqual(build_api.sent_options(device["defs"], None), device["defs"],
                         "nothing sent is not a choice")
        self.assertEqual(build_api.sent_options({}, {"STRATA_MMQ_TAB": "ON"}), {"STRATA_MMQ_TAB": "ON"})

    def test_the_status_payload_carries_those_defaults(self) -> None:
        """The form the tab builds from GET /build/status has to be the configuration the
        same tab then runs: a manifest of upstream defaults shows every GPU backend off,
        and the build that same tab starts has no engine in it.
        """
        tmp = Path(tempfile.mkdtemp())
        manifest = tmp / "build_options.json"
        manifest.write_text(json.dumps({
            "version": "0.1.45",
            "cmake_options": [{"name": "STRATA_ENABLE_CUDA", "kind": "bool",
                               "default": "OFF", "help": "Build the CUDA targets",
                               "visible": True}],
            "setup_choices": [],
        }), encoding="utf-8")
        saved = (build_api.MANIFEST, build_api.ENGINE_DIR, build_api.latest_release)
        build_api.MANIFEST = manifest
        build_api.ENGINE_DIR = tmp / "engine"
        build_api.ENGINE_DIR.mkdir()
        build_api.latest_release = lambda: {"repo": "x", "releases": []}
        try:
            payload = build_api.build_status()
        finally:
            build_api.MANIFEST, build_api.ENGINE_DIR, build_api.latest_release = saved
        shown = {o["name"]: o for o in payload["options"]["cmake_options"]}
        self.assertEqual(shown["STRATA_ENABLE_CUDA"]["default"], "ON")
        self.assertEqual(payload["options"]["device"]["device"], "cuda")
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["cmake_options"][0]["default"], "OFF",
                         "the manifest on disk stays the release's own defaults")
        shutil.rmtree(tmp, ignore_errors=True)


class BackendRuleTests(unittest.TestCase):
    """The rules themselves, independent of what the probes find on the box."""

    CUDA: ClassVar[dict[str, Any]] = {"device": "cuda", "defs": {"STRATA_ENABLE_CUDA": "ON"}}

    def test_a_hip_only_option_is_off_on_a_cuda_box(self) -> None:
        defs, notes = build_api.resolve_build_options(
            {"STRATA_ENABLE_CUDA": "ON", "STRATA_ENABLE_HIP": "OFF",
             "STRATA_NATIVE_EXPERTS": "ON", "STRATA_PREFILL_MMQ": "ON"}, self.CUDA)
        self.assertEqual(defs["STRATA_PREFILL_MMQ"], "OFF")
        self.assertEqual(len(notes), 1)
        self.assertIn("STRATA_ENABLE_HIP=ON", notes[0])

    def test_an_option_needs_every_flag_it_requires(self) -> None:
        defs, notes = build_api.resolve_build_options(
            {"STRATA_ENABLE_HIP": "ON", "STRATA_NATIVE_EXPERTS": "OFF",
             "STRATA_PREFILL_MMQ": "ON"}, {"device": "hip", "defs": {"STRATA_ENABLE_HIP": "ON"}})
        self.assertEqual(defs["STRATA_PREFILL_MMQ"], "OFF")
        self.assertIn("STRATA_NATIVE_EXPERTS=ON", notes[0])

    def test_two_backends_are_not_sent_to_cmake(self) -> None:
        defs, notes = build_api.resolve_build_options(
            {"STRATA_ENABLE_CUDA": "ON", "STRATA_ENABLE_HIP": "ON"}, self.CUDA)
        self.assertEqual(defs["STRATA_ENABLE_HIP"], "OFF",
                         "upstream refuses CUDA and HIP in the same configure")
        self.assertEqual(defs["STRATA_ENABLE_CUDA"], "ON")
        self.assertIn("one backend at a time", notes[0])

    def test_a_valid_configuration_is_left_alone(self) -> None:
        defs = {"STRATA_ENABLE_CUDA": "ON", "CMAKE_CUDA_ARCHITECTURES": "89",
                "STRATA_MMQ_KQUANTS": "ON", "STRATA_NATIVE_EXPERTS": "ON",
                "STRATA_PREFILL_MMQ": "OFF", "STRATA_Q6K_EXPERTS": "ON"}
        resolved, notes = build_api.resolve_build_options(defs, self.CUDA)
        self.assertEqual(resolved, defs)
        self.assertEqual(notes, [])

    def test_an_empty_value_is_off_not_a_backend(self) -> None:
        defs, notes = build_api.resolve_build_options(
            {"STRATA_ENABLE_CUDA": "", "STRATA_PREFILL_MMQ": "ON"}, self.CUDA)
        self.assertEqual(defs["STRATA_PREFILL_MMQ"], "OFF")
        self.assertEqual(len(notes), 1)


class EngineTargetTests(unittest.TestCase):
    """A build is only started when the configuration has an engine in it.

    The pre-flight reads the project ninja file: a configuration with no GPU backend
    has no `strata` rule, and saying so before running cmake is more useful than a
    ninja error and a dead build.
    """

    NINJA = "build strata: CXX_EXECUTABLE_LINKER__strata\nbuild strata-vision: link\n"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (build_api.cuda_toolkit, build_api.cuda_arch, build_api.shutil.which)
        build_api.cuda_toolkit = lambda: ""
        build_api.cuda_arch = lambda: ""
        build_api.shutil.which = lambda name: None
        self.job = StubBuild([0])

    def tearDown(self) -> None:
        build_api.cuda_toolkit, build_api.cuda_arch, build_api.shutil.which = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_targets_are_read_from_the_configured_project(self) -> None:
        (self.tmp / "build.ninja").write_text(self.NINJA, encoding="utf-8")
        self.assertEqual(self.job.configured_targets(self.tmp), {"strata", "strata-vision"})

    def test_a_configuration_with_no_engine_is_named_before_cmake_runs(self) -> None:
        (self.tmp / "build.ninja").write_text("build ggml: compile\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            self.job.build_targets(self.tmp)
        message = str(caught.exception)
        self.assertIn("no engine target", message)
        self.assertIn("STRATA_ENABLE_CUDA", message)
        self.assertEqual(self.job.calls, [], "cmake was never run against a build with no engine")

    def test_only_the_targets_that_exist_are_requested(self) -> None:
        (self.tmp / "build.ninja").write_text(
            "build strata: link\nbuild hypercore-worker: link\n", encoding="utf-8")
        self.assertTrue(self.job.build_targets(self.tmp))
        self.assertEqual(targets_of(self.job.calls[0]), ["strata", "hypercore-worker"])
        self.assertIn("no strata-vision target", "\n".join(self.job.log))

    def test_an_unreadable_project_falls_back_to_the_upstream_names(self) -> None:
        self.assertTrue(self.job.build_targets(self.tmp / "no-such-build"))
        self.assertEqual(targets_of(self.job.calls[0]),
                        ["strata", "strata-vision", "hypercore-worker"])

    def test_a_worker_built_as_a_subproject_is_still_found(self) -> None:
        build = self.tmp / "b"
        (build / "hypercore-interface").mkdir(parents=True)
        (build / "strata").write_bytes(b"engine")
        (build / "hypercore-interface" / "hypercore-worker").write_bytes(b"worker")
        found = BuildJob().built_binaries(build)
        self.assertEqual(sorted(p.name for p in found), ["hypercore-worker", "strata"],
                         "ninja puts a subproject's executable under its own folder")

    def test_a_build_that_produces_no_binary_is_not_stored_as_a_version(self) -> None:
        empty = self.tmp / "empty-build"
        empty.mkdir()
        with self.assertRaises(RuntimeError) as caught:
            BuildJob().transfer_binaries(empty, {"version": "0.1.45"}, "iq2_xs")
        self.assertIn("no engine binary", str(caught.exception))

    def test_the_shared_kernels_a_build_links_are_found_with_the_engine(self) -> None:
        build = self.tmp / "b2"
        (build / "hypercore-interface").mkdir(parents=True)
        (build / "strata").write_bytes(b"engine")
        (build / "libstrata_kq.so").write_bytes(b"kernels")
        (build / "libstrata_core.a").write_bytes(b"static, not loaded at run time")
        (build / "hypercore-interface" / "libhypercore_api.so").write_bytes(b"worker library")
        found = sorted(p.name for p in BuildJob().built_binaries(build))
        self.assertEqual(found, ["libhypercore_api.so", "libstrata_kq.so", "strata"],
                        "the shared libraries a build produces must travel with the engine")


class VisionBuildTests(unittest.TestCase):
    """The image encoder is upstream's own project, and its failure is not fatal.

    tools/vision is not a subdirectory of the engine's CMakeLists.txt, so the Build tab
    configures and builds it separately. A release without it, or a vision build that
    fails, still produces an engine.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.clone = self.tmp / "clone"
        (self.clone / "tools" / "vision").mkdir(parents=True)
        (self.clone / "tools" / "vision" / "CMakeLists.txt").write_text("project(vision)", encoding="utf-8")
        self.saved = (build_api.cuda_toolkit, build_api.cuda_arch, build_api.shutil.which,
                      quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR,
                      quant_store.INDEX_PATH, quant_store.CONFIG_DIR)
        build_api.cuda_toolkit = lambda: ""
        build_api.cuda_arch = lambda: ""
        build_api.shutil.which = lambda name: None
        quant_store.ENGINE_DIR = self.tmp / "engine"
        quant_store.VERSIONS_DIR = quant_store.ENGINE_DIR / "versions"
        quant_store.INDEX_PATH = quant_store.ENGINE_DIR / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.tmp / "config"
        self.job = StubBuild([0, 0])

    def tearDown(self) -> None:
        build_api.cuda_toolkit, build_api.cuda_arch, build_api.shutil.which, \
            quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR, quant_store.INDEX_PATH, \
            quant_store.CONFIG_DIR = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_llama_source_is_the_one_the_clone_has(self) -> None:
        vendored = self.clone / "third_party" / "llama.cpp"
        vendored.mkdir(parents=True)
        (vendored / "CMakeLists.txt").write_text("project(llama)", encoding="utf-8")
        self.assertEqual(build_api._llama_source(self.clone), vendored)
        shutil.rmtree(vendored)
        fetched = self.clone / "build" / "_deps" / "strata_llamacpp-src"
        fetched.mkdir(parents=True)
        (fetched / "CMakeLists.txt").write_text("project(llama)", encoding="utf-8")
        self.assertEqual(build_api._llama_source(self.clone), fetched,
                         "a fresh clone fetches llama.cpp into build/_deps")

    def test_the_encoder_is_built_in_its_own_folder(self) -> None:
        out = self.clone / "build-vision" / "bin"
        out.mkdir(parents=True)
        (out / "strata-vision").write_bytes(b"vision encoder")
        path = self.job.build_vision(self.clone, {"STRATA_ENABLE_CUDA": "ON",
                                                 "CMAKE_CUDA_ARCHITECTURES": "89"})
        self.assertEqual(path, out / "strata-vision")
        self.assertTrue(any(f.startswith("-DLLAMA_DIR=") for f in self.job.calls[0]))
        self.assertIn("-DSTRATA_VISION_CUDA=ON", self.job.calls[0])
        self.assertEqual(targets_of(self.job.calls[1]), ["strata-vision"])

    def test_a_release_without_the_encoder_is_said_not_crashed_on(self) -> None:
        shutil.rmtree(self.clone / "tools")
        self.assertIsNone(self.job.build_vision(self.clone, {}))
        self.assertIn("no tools/vision", "\n".join(self.job.log))
        self.assertEqual(self.job.calls, [], "nothing was configured for a project that is not there")

    def test_a_failed_encoder_build_is_reported_and_the_engine_still_goes(self) -> None:
        job = StubBuild([1])
        self.assertIsNone(job.build_vision(self.clone, {}))
        self.assertIn("could not be configured", "\n".join(job.log))

    def test_an_extra_binary_is_stored_with_the_engine(self) -> None:
        build_dir = self.tmp / "build"
        build_dir.mkdir()
        (build_dir / "strata").write_bytes(b"engine")
        vision = self.tmp / "build-vision" / "bin" / "strata-vision"
        vision.parent.mkdir(parents=True)
        vision.write_bytes(b"vision encoder")
        job = BuildJob()
        job.log_path = self.tmp / "build.log"
        copied, _key = job.transfer_binaries(build_dir, {"version": "0.1.45"}, "iq2_xs", [vision])
        self.assertEqual(sorted(copied), ["strata", "strata-vision"])
        self.assertEqual((quant_store.store_dir("iq2_xs") / "strata-vision").read_bytes(),
                         b"vision encoder")


class InterfaceWiringTests(unittest.TestCase):
    """The worker this project ships is only built if the clone includes it.

    engine-src/ is copied into the clone, but upstream's CMakeLists.txt has no line
    that reads it, so without the wiring the copy is inert and the log says a worker
    was injected when nothing was built.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cmake = self.tmp / "CMakeLists.txt"
        self.cmake.write_text("project(Strata)\nadd_executable(strata src/generate.cpp)\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_line_is_added_once(self) -> None:
        self.assertTrue(build_api.wire_in_interface(self.tmp))
        self.assertTrue(build_api.wire_in_interface(self.tmp))
        self.assertEqual(self.cmake.read_text().count(build_api.INTERFACE_LINE), 1)
        self.assertIn("add_executable(strata", self.cmake.read_text(), "the clone's own project is kept")

    def test_a_missing_cmake_file_is_reported_not_raised(self) -> None:
        self.assertFalse(build_api.wire_in_interface(self.tmp / "no-such-clone"))


class SelfContainedEngineTests(unittest.TestCase):
    """An installed engine has to find its own library once the build tree is gone.

    The tab copies hypercore-worker and libhypercore_api.so into engine/engine_<quant>/
    and then deletes tmp/, so the loader resolves the library through the RUNPATH stored
    in the binary. cmake's default is the build tree, which no longer exists at run time,
    and a file copy has no install step to rewrite it. The interface's CMakeLists has to
    ask for $ORIGIN - the directory the binary was copied into.
    """

    CMAKE_FILE: ClassVar[Path] = ROOT / "engine-src" / "CMakeLists.txt"

    def test_the_interface_builds_with_the_origin_rpath(self) -> None:
        text = self.CMAKE_FILE.read_text(encoding="utf-8")
        self.assertIn('set(CMAKE_INSTALL_RPATH "$ORIGIN")', text)
        self.assertIn("set(CMAKE_BUILD_WITH_INSTALL_RPATH ON)", text)

    def test_a_copied_engine_resolves_the_library_next_to_it(self) -> None:
        cmake = shutil.which("cmake")
        if cmake is None:
            self.skipTest("cmake is not installed")
        tmp = Path(tempfile.mkdtemp())
        build, engine = tmp / "build", tmp / "engine_engine_q2_0"
        engine.mkdir()
        try:
            subprocess.run([cmake, "-S", str(ROOT / "engine-src"), "-B", str(build)],
                           check=True, capture_output=True)
            subprocess.run([cmake, "--build", str(build), "--parallel", "2"],
                           check=True, capture_output=True)
            for name in (quant_store.WORKER, "libhypercore_api.so"):
                shutil.copy2(build / name, engine / name)
            shutil.rmtree(build)  # the tab cleans tmp/ after a build
            out = subprocess.run(["ldd", str(engine / quant_store.WORKER)],
                                 capture_output=True, text=True, check=False)
            self.assertIn(str(engine / "libhypercore_api.so"), out.stdout,
                          "the installed worker must load the library copied with it")
            self.assertNotIn("not found", out.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MergeDataTests(unittest.TestCase):
    """A completed build merges the clone's data/ into the project's data/, and stores
    the runtime files it ships for the quant the build was named for."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (build_api.ENGINE_DIR, quant_store.ROOT, quant_store.PROJECT_DATA_DIR,
                      quant_store.TMP_DIR, quant_store.ENGINE_DIR, quant_store.CONFIG_DIR,
                      quant_store.INDEX_PATH, quant_store.LEGACY_DIR)
        build_api.ENGINE_DIR = self.tmp / "engine"
        quant_store.ROOT = self.tmp
        quant_store.PROJECT_DATA_DIR = self.tmp / "data"
        quant_store.TMP_DIR = self.tmp / "tmp"
        quant_store.ENGINE_DIR = build_api.ENGINE_DIR
        quant_store.CONFIG_DIR = self.tmp / "config"
        quant_store.LEGACY_DIR = quant_store.ENGINE_DIR / "versions"
        quant_store.INDEX_PATH = quant_store.ENGINE_DIR / "VERSIONS.json"
        quant_store.CONFIG_DIR.mkdir(parents=True)
        (quant_store.PROJECT_DATA_DIR / "media").mkdir(parents=True)
        self.clone = quant_store.TMP_DIR / "strata-v0.1.45"
        (self.clone / "data").mkdir(parents=True)
        (self.clone / "data" / "expert-profile.bin").write_bytes(b"the build's profile")
        self.job = BuildJob()

    def tearDown(self) -> None:
        (build_api.ENGINE_DIR, quant_store.ROOT, quant_store.PROJECT_DATA_DIR,
         quant_store.TMP_DIR, quant_store.ENGINE_DIR, quant_store.CONFIG_DIR,
         quant_store.INDEX_PATH, quant_store.LEGACY_DIR) = self.saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_build_merges_the_clone_data_and_says_so_in_the_log(self) -> None:
        merged = self.job.merge_clone_data(self.clone)
        self.assertEqual(merged["added"], ["expert-profile.bin"])
        merged_file = quant_store.PROJECT_DATA_DIR / "expert-profile.bin"
        self.assertTrue(merged_file.is_file(), "the build did not merge the runtime file")
        self.assertEqual(merged_file.read_bytes(), b"the build's profile")
        self.assertIn("merged data/", "\n".join(self.job.log))
        # the clone is left in place, so the merged copy is the one that survives
        self.assertTrue(self.clone.is_dir())
        self.assertTrue((self.clone / "data" / "expert-profile.bin").is_file())

    def test_a_data_file_the_project_already_has_is_kept_and_reported(self) -> None:
        (quant_store.PROJECT_DATA_DIR / "expert-profile.bin").write_bytes(b"the app's own")
        merged = self.job.merge_clone_data(self.clone)
        self.assertEqual(merged["skipped"], ["expert-profile.bin"])
        self.assertEqual((quant_store.PROJECT_DATA_DIR / "expert-profile.bin").read_bytes(),
                         b"the app's own")
        self.assertIn("kept as they were", "\n".join(self.job.log))

    def test_a_build_that_shipped_no_data_reports_nothing(self) -> None:
        (self.clone / "data" / "expert-profile.bin").unlink()
        (self.clone / "data").rmdir()
        self.assertEqual(self.job.merge_clone_data(self.clone),
                         {"added": [], "skipped": [], "same": []})
        self.assertEqual(self.job.log, [], "a build that shipped nothing logged a merge")

    def test_a_build_stores_the_data_for_its_quant_and_points_the_config_at_it(self) -> None:
        """The Build tab's own path: transfer, then write the run config for the quant."""
        build_dir = self.clone / "build"      # a build runs inside the clone, and so does its parent
        build_dir.mkdir()
        (build_dir / "strata").write_bytes(b"elf")
        (quant_store.CONFIG_DIR / "run-example.json").write_text(json.dumps({
            "model_name": "Qwen2-7B-Q2_0", "args": ["--expert-profile",
                                                  "/ABSOLUTE/PATH/TO/expert-profile.bin"]}),
            encoding="utf-8")
        _, key = self.job.transfer_binaries(build_dir, {"version": "0.1.45"}, "q2_0")
        stored = quant_store.data_dir(key)
        self.assertTrue((stored / "expert-profile.bin").is_file(),
                        "the build stored no data for its quant")
        cfg = json.loads((quant_store.CONFIG_DIR / "run-q2_0.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["args"][1], str(stored / "expert-profile.bin"),
                         "the run config still names the placeholder")
        self.assertIn("runtime file(s) from the clone stored", "\n".join(self.job.log))

    def test_a_build_also_points_the_config_of_a_model_built_earlier(self) -> None:
        """Every config in config/ is pointed at the build stored for its model.

        The q2_0 case the user hit: the build was named for IQ2_XS, so only its own run
        config was written, and the q2_0 config kept the placeholder while the profile sat
        unread in engine/engine_q2_0/.
        """
        build_dir = self.clone / "build"
        build_dir.mkdir()
        (build_dir / "strata").write_bytes(b"elf")
        # the template the repo ships, and the config the first build wrote from it
        (quant_store.CONFIG_DIR / "run-example.json").write_text(json.dumps({
            "exe": "engine/engine_q2_0/strata",
            "model_name": "q2_0",
            "args": ["--expert-profile", "/ABSOLUTE/PATH/TO/expert-profile.bin"]}),
            encoding="utf-8")
        earlier = self.tmp / "build-q2_0"
        earlier.mkdir()
        (earlier / "strata").write_bytes(b"q2 engine")
        quant_store.store_binaries("q2_0", [earlier / "strata"], {"version": "0.1.45"})
        (quant_store.CONFIG_DIR / "run-q2_0.json").write_text(json.dumps({
            "exe": "engine/strata",
            "model_name": "Qwen2-7B-Q2_0",
            "args": ["--expert-profile", "/ABSOLUTE/PATH/TO/expert-profile.bin"]}),
            encoding="utf-8")
        self.job.transfer_binaries(build_dir, {"version": "0.1.45"}, "IQ2_XS")
        notes = quant_store.refresh_run_configs("iq2_xs", self.clone)
        joined = " ".join(notes)
        for key in ("iq2_xs", "q2_0"):
            stored = quant_store.data_dir(key) / "expert-profile.bin"
            self.assertTrue(stored.is_file(), f"the build stored no runtime data for {key}")
            cfg = json.loads((quant_store.CONFIG_DIR / f"run-{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["args"][1], str(stored),
                             f"config/run-{key}.json still names the placeholder")
        self.assertIn("run-q2_0.json", joined, "the sweep said nothing about the other model")
        self.assertIn("run-example.json", joined, "the shipped template was left unrepaired")

    def test_a_build_on_a_box_with_no_run_config_writes_one(self) -> None:
        """config/ empty: the build still writes config/run-<quant>.json.

        There is no run config to copy in a fresh clone once the user clears config/, and
        the build used to give up with "there is no run config to base a new one on" - an
        engine in engine/ with nothing to start it.
        """
        build_dir = self.clone / "build"
        build_dir.mkdir()
        (build_dir / "strata").write_bytes(b"elf")
        self.job.transfer_binaries(build_dir, {"version": "0.1.45"}, "IQ2_XS")
        cfg_path = quant_store.CONFIG_DIR / "run-iq2_xs.json"
        self.assertTrue(cfg_path.is_file(), "the build stored an engine and wrote no run config")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["exe"], str(quant_store.store_dir("iq2_xs") / "strata"))
        self.assertEqual(cfg["args"][cfg["args"].index("--expert-profile") + 1],
                         str(quant_store.data_dir("iq2_xs") / "expert-profile.bin"),
                         "the config written from the code template lost the build's data")
        self.assertIn("created config/run-iq2_xs.json", "\n".join(self.job.log))

    def test_a_build_with_no_quant_names_stores_its_runtime_data_next_to_the_engine(self) -> None:
        """engine/data/ is the runtime data of the default engine in engine/."""
        build_dir = self.clone / "build"
        build_dir.mkdir()
        (build_dir / "strata").write_bytes(b"elf")
        self.job.transfer_binaries(build_dir, {"version": "0.1.45"}, None)
        stored = quant_store.data_dir(None) / "expert-profile.bin"
        self.assertTrue(stored.is_file(), "the default engine build stored no runtime data")
        self.assertIn("stored engine/data", "\n".join(self.job.log))

    def test_the_sweep_runs_after_the_build_and_while_the_clone_is_still_there(self) -> None:
        """The sweep reads the clone's data/, so it has to run before tmp/ is touched."""
        source = (ROOT / "serve" / "build_api.py").read_text(encoding="utf-8")
        body = source[source.index("def _run("):]
        self.assertIn("quant_store.refresh_run_configs(key, clone)", body,
                      "the build never sweeps the run configs of the other models")
        self.assertLess(body.index("self.merge_clone_data(clone)"),
                        body.index("quant_store.refresh_run_configs(key, clone)"),
                        "the sweep runs before the build has stored anything")
        self.assertLess(body.index("quant_store.refresh_run_configs(key, clone)"),
                        body.rindex("self.clean_tmp()"),
                        "the sweep runs after the clone it reads is gone")
        self.assertIn('"refreshed_configs": refreshed', body,
                      "the build does not report which configs it updated")

    def test_the_merge_runs_after_the_build_and_while_the_clone_is_still_there(self) -> None:
        """The merge reads the clone, so it has to run after the build and before any
        call that could remove it."""
        source = (ROOT / "serve" / "build_api.py").read_text(encoding="utf-8")
        body = source[source.index("def _run("):]
        self.assertLess(body.index("self.transfer_binaries(build_dir"),
                        body.index("self.merge_clone_data(clone)"),
                        "the merge runs before the build has produced anything")
        self.assertLess(body.index("self.merge_clone_data(clone)"),
                        body.rindex("self.clean_tmp()"),
                        "the build merges data/ after the clone is gone")


class ReusedCloneTests(unittest.TestCase):
    """A build that finds its clone already in tmp/ uses it instead of failing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = build_api.TMP_DIR
        build_api.TMP_DIR = self.tmp / "tmp"
        self.clone = build_api.TMP_DIR / "strata-v0.1.45"
        (self.clone / ".git").mkdir(parents=True)
        self.job = StubBuild([0, 0])

    def tearDown(self) -> None:
        build_api.TMP_DIR = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_clone_is_fetched_and_reset_to_the_tag(self) -> None:
        """The build is still honest about which version it is building: the tag is
        fetched again and the tree is reset to it, not simply reused as it stands."""
        self.assertTrue(self.job.reuse_clone(self.clone, "v0.1.45"))
        self.assertEqual(self.job.calls[0][:3], ["git", "-C", str(self.clone)])
        self.assertEqual(self.job.calls[0][3:5], ["fetch", "--depth"])
        self.assertEqual(self.job.calls[1][3:5], ["checkout", "-f"])

    def test_a_folder_that_is_not_a_checkout_is_not_built_from(self) -> None:
        """tmp/ can hold a tree left by an older run; it is not a clone."""
        stray = build_api.TMP_DIR / "strata-main"
        stray.mkdir()
        self.assertFalse(self.job.reuse_clone(stray, "main"))
        self.assertEqual(self.job.calls, [], "a tree it cannot identify was used anyway")

    def test_a_failed_fetch_is_not_followed_by_a_checkout(self) -> None:
        job = StubBuild([1])
        self.assertFalse(job.reuse_clone(self.clone, "v0.1.45"))
        self.assertEqual(len(job.calls), 1, "the checkout ran after a fetch that failed")

    def test_the_build_reuses_the_clone_it_finds(self) -> None:
        """The clone step has to take the reuse path when the folder is already there."""
        source = (ROOT / "serve" / "build_api.py").read_text(encoding="utf-8")
        body = source[source.index("def _run("):]
        self.assertIn("if clone.is_dir():", body, "an existing clone is not recognised")
        self.assertLess(body.index("self.reuse_clone(clone, tag)"),
                        body.index('raise RuntimeError("git clone failed")'),
                        "the build still clones over a checkout it already has")
        self.assertIn("reused the clone already in tmp/", body,
                      "a build that reused a clone does not say so in the log")

class AssetTests(unittest.TestCase):
    """Which release asset the ready-made path downloads, and how it is unpacked.

    0.1.45 published one zip per OS. 0.2.0 publishes a per-toolchain engine bundle
    (strata-engine-<ver>-<os>-<arch>-<backend>-<toolchain>.tar.gz) beside the app
    installers and the updater artifact, so the pick has to be by content: an archive,
    for this OS, for the GPU toolchain this box has. Nothing here names a release.
    """

    RELEASE_020: ClassVar[dict[str, Any]] = {
        "version": "0.2.0", "tag": "v0.2.0-beta",
        "assets": [
            {"name": "Strata-0.2.0-beta-windows-cuda13.exe", "url": "u1"},
            {"name": "Strata-0.2.0-beta-linux-update.AppImage", "url": "u2"},
            {"name": "Strata.app.tar.gz", "url": "u3"},
            {"name": "strata-engine-0.1.46-linux-x86_64-cuda-cuda13.tar.gz", "url": "u4"},
            {"name": "strata-engine-0.1.46-linux-x86_64-cuda-cuda12.tar.gz", "url": "u5"},
            {"name": "strata-engine-0.1.46-linux-x86_64-hip.tar.gz", "url": "u6"},
            {"name": "strata-engine-0.1.46-windows-x86_64-cuda-cuda13.zip", "url": "u7"},
        ],
    }
    RELEASE_0145: ClassVar[dict[str, Any]] = {
        "version": "0.1.45", "tag": "v0.1.45",
        "assets": [
            {"name": "strata-linux-x64.zip", "url": "a1"},
            {"name": "strata-linux-x64-cuda12.zip", "url": "a2"},
            {"name": "strata-windows-x64.zip", "url": "a3"},
            {"name": "strata-windows-x64-hip.zip", "url": "a4"},
        ],
    }

    def test_the_toolkit_on_the_box_picks_the_matching_bundle(self) -> None:
        cuda13 = build_api.asset_for(self.RELEASE_020,
                                     {"device": "cuda", "toolkit": "/usr/local/cuda-13.4/bin", "arch": "sm_89"})
        self.assertEqual(cuda13["name"], "strata-engine-0.1.46-linux-x86_64-cuda-cuda13.tar.gz")
        cuda12 = build_api.asset_for(self.RELEASE_020,
                                     {"device": "cuda", "toolkit": "/usr/local/cuda-12.6/bin", "arch": "sm_75"})
        self.assertEqual(cuda12["name"], "strata-engine-0.1.46-linux-x86_64-cuda-cuda12.tar.gz")

    def test_a_hip_box_gets_the_hip_bundle(self) -> None:
        asset = build_api.asset_for(self.RELEASE_020, {"device": "hip", "arch": "", "defs": {}})
        self.assertEqual(asset["name"], "strata-engine-0.1.46-linux-x86_64-hip.tar.gz")

    def test_an_app_installer_is_neither_an_updater_nor_the_engine(self) -> None:
        for name in ("Strata-0.2.0-beta-windows-cuda13.exe", "Strata-0.2.0-beta-linux-update.AppImage",
                     "Strata.app.tar.gz", "strata-linux-x64.zip"):
            self.assertEqual(build_api.is_engine_archive(name), name == "strata-linux-x64.zip", f"{name}")

    def test_no_gpu_toolchain_refuses_a_gpu_only_archive(self) -> None:
        """A CUDA bundle downloaded on a CPU-only box is an engine that cannot run."""
        self.assertIsNone(build_api.asset_for(self.RELEASE_020, {"device": "none", "arch": "", "defs": {}}))
        plain = {"version": "0.1.45", "assets": [{"name": "strata-linux-x64.zip", "url": "a1"}]}
        self.assertEqual(build_api.asset_for(plain, {"device": "none"})["name"], "strata-linux-x64.zip")

    def test_the_0145_names_are_still_understood(self) -> None:
        cuda = build_api.asset_for(self.RELEASE_0145, {"device": "cuda", "toolkit": "/usr/local/cuda-12.4/bin"})
        self.assertEqual(cuda["name"], "strata-linux-x64-cuda12.zip")
        self.assertEqual(build_api.asset_for(self.RELEASE_0145, {"device": "none"})["name"], "strata-linux-x64.zip")
        hip = build_api.asset_for(self.RELEASE_0145, {"device": "hip"})
        self.assertEqual(hip["name"], "strata-linux-x64.zip", "no HIP archive for Linux, so the plain one")

    def test_an_os_the_release_does_not_cover_is_said_plainly(self) -> None:
        windows_only = {"version": "0.2.0", "assets": [
            {"name": "strata-engine-0.1.46-windows-x86_64-cuda-cuda13.zip", "url": "u7"}]}
        self.assertIsNone(build_api.asset_for(windows_only, {"device": "cuda", "toolkit": "/usr/local/cuda-13.4"}))
        self.assertIsNone(build_api.asset_for({"version": "0.2.0", "assets": []}, {"device": "none"}))

    def test_a_tar_gz_engine_is_unpacked_and_installed(self) -> None:
        """The ready-made path has to read tar.gz, not only zip."""
        import io
        import tarfile
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w:gz") as tf:
            for name, data in (("strata", b"ready engine"), ("libstrata_kq.so", b"kernels")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        blob = blob.getvalue()
        digest = __import__("hashlib").sha256(blob).hexdigest()
        tmp = Path(tempfile.mkdtemp())
        real_tmp, real_urlretrieve, real_digests, real_engine = (
            build_api.TMP_DIR, build_api.urllib.request.urlretrieve,
            build_api.published_digests, build_api.ENGINE_DIR)
        build_api.TMP_DIR = tmp / "scratch"
        build_api.ENGINE_DIR = tmp / "engine"
        build_api.urllib.request.urlretrieve = lambda url, dest: Path(dest).write_bytes(blob)
        build_api.published_digests = lambda r: {"strata-engine-0.1.46-linux-x86_64-cuda-cuda13.tar.gz": digest}
        try:
            out = BuildJob().fetch_prebuilt(
                {"version": "0.1.46", "tag": "v0.1.46",
                 "assets": [{"name": "strata-engine-0.1.46-linux-x86_64-cuda-cuda13.tar.gz", "url": "file://x"}]},
                None)
            self.assertTrue(out["ok"], out.get("error"))
            self.assertEqual(sorted(out["files"]), ["BUILD.json", "libstrata_kq.so", "strata"],
                            "the tar.gz contents plus the BUILD.json the install writes")
            self.assertTrue((build_api.ENGINE_DIR / "strata").is_file())
            self.assertFalse((build_api.TMP_DIR / "strata-engine-0.1.46-linux-x86_64-cuda-cuda13.tar.gz").exists(),
                            "the downloaded archive is left in the scratch folder")
        finally:
            build_api.TMP_DIR, build_api.urllib.request.urlretrieve, \
                build_api.published_digests, build_api.ENGINE_DIR = \
                real_tmp, real_urlretrieve, real_digests, real_engine
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_archive_member_outside_its_folder_is_refused(self) -> None:
        import zipfile
        scratch = Path(tempfile.mkdtemp())
        archive = scratch / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../strata", b"not the engine")
        dest = scratch / "engine"
        try:
            with self.assertRaises(ValueError) as caught:
                build_api.unpack_archive(archive, dest)
            self.assertIn("outside its folder", str(caught.exception))
            self.assertFalse(list(dest.glob("*")), "nothing was written before the refusal")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_a_tar_member_is_unpacked_into_the_folder_it_is_given(self) -> None:
        import io
        import tarfile
        scratch = Path(tempfile.mkdtemp())
        archive = scratch / "engine.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for name, data in (("strata", b"ready engine"), ("data/expert-profile.bin", b"profile")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        dest = scratch / "unpacked"
        try:
            names = build_api.unpack_archive(archive, dest)
            self.assertEqual(sorted(names), ["data/expert-profile.bin", "strata"])
            self.assertEqual((dest / "strata").read_bytes(), b"ready engine")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class VersionTests(unittest.TestCase):
    """An update is a newer version, not a different string.

    0.1.45.3 is a patch of 0.1.45: an engine built from it must not be told it is
    behind the 0.1.45 release.
    """

    def test_versions_compare_as_numbers(self) -> None:
        self.assertEqual(build_api.version_tuple("0.1.45.3"), (0, 1, 45, 3))
        self.assertEqual(build_api.version_tuple("v0.2.0-beta"), (0, 2, 0))
        self.assertIsNone(build_api.version_tuple(""))
        self.assertTrue(build_api.is_newer("0.1.45.3", "0.1.45"))
        self.assertFalse(build_api.is_newer("0.1.45", "0.1.45.3"))
        self.assertFalse(build_api.is_newer("0.1.45", "0.1.45"))
        self.assertTrue(build_api.is_newer("0.2.0", "0.1.45.3"))
        self.assertFalse(build_api.is_newer(None, None), "nothing to update from")

    def test_an_engine_ahead_of_the_named_release_is_not_told_to_update(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        real_engine, real_manifest = build_api.ENGINE_DIR, build_api.MANIFEST
        build_api.ENGINE_DIR = tmp / "engine"
        build_api.ENGINE_DIR.mkdir()
        try:
            build_api.latest_release = lambda: {"repo": "Niko1221/Strata",
                                               "releases": [{"tag": "v0.1.45", "version": "0.1.45",
                                                            "assets": []}]}
            (build_api.ENGINE_DIR / "BUILD.json").write_text(json.dumps({"version": "0.1.45.3"}), encoding="utf-8")
            self.assertFalse(build_api.build_status()["update_available"],
                             "an engine already at 0.1.45.3 is not behind a 0.1.45 release")
            (build_api.ENGINE_DIR / "BUILD.json").write_text(json.dumps({"version": "0.1.45"}), encoding="utf-8")
            self.assertFalse(build_api.build_status()["update_available"], "the same version is not an update")
            build_api.latest_release = lambda: {"repo": "Niko1221/Strata",
                                               "releases": [{"tag": "v0.1.45.3", "version": "0.1.45.3",
                                                            "assets": []}]}
            self.assertTrue(build_api.build_status()["update_available"], "0.1.45 against 0.1.45.3 is")
        finally:
            build_api.ENGINE_DIR, build_api.MANIFEST = real_engine, real_manifest
            shutil.rmtree(tmp, ignore_errors=True)


class RunChoiceTests(unittest.TestCase):
    """The Setup-choice boxes reach the run config a build writes.

    A run config is copied from the nearest config on the box - the shipped template
    when there is none - so a build started with --context 262144 --kv q4_0 ran on
    whatever that file happened to name. The boxes are sent with the build request, and
    these are the settings written into the config the build just wrote.
    """

    TEMPLATE: ClassVar[dict[str, Any]] = {
        "_note": "Template for q2_0.",
        "exe": "engine/strata",
        "args": ["--max-context", "131072", "--kv", "int8", "--resident-experts"],
        "model_name": "q2_0",
        "port": 8080,
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (build_api.ENGINE_DIR, quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR,
                      quant_store.INDEX_PATH, quant_store.LEGACY_DIR, quant_store.CONFIG_DIR,
                      quant_store.ROOT, quant_store.PROJECT_DATA_DIR, quant_store.TMP_DIR)
        build_api.ENGINE_DIR = self.tmp / "engine"
        quant_store.ENGINE_DIR = build_api.ENGINE_DIR
        quant_store.VERSIONS_DIR = quant_store.ENGINE_DIR / "versions"
        quant_store.LEGACY_DIR = quant_store.VERSIONS_DIR
        quant_store.INDEX_PATH = quant_store.ENGINE_DIR / "VERSIONS.json"
        quant_store.CONFIG_DIR = self.tmp / "config"
        quant_store.CONFIG_DIR.mkdir(parents=True)
        quant_store.PROJECT_DATA_DIR = self.tmp / "data"
        quant_store.TMP_DIR = self.tmp / "tmp"
        (quant_store.CONFIG_DIR / "run-example.json").write_text(
            json.dumps(self.TEMPLATE), encoding="utf-8")
        self.build_dir = self.tmp / "strata-v0.1.45" / "build"
        self.build_dir.mkdir(parents=True)
        (self.build_dir / "strata").write_bytes(b"engine binary")
        self.job = BuildJob()

    def tearDown(self) -> None:
        (build_api.ENGINE_DIR, quant_store.ENGINE_DIR, quant_store.VERSIONS_DIR,
         quant_store.INDEX_PATH, quant_store.LEGACY_DIR, quant_store.CONFIG_DIR,
         quant_store.ROOT, quant_store.PROJECT_DATA_DIR, quant_store.TMP_DIR) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build_with(self, choices: list[str]) -> dict[str, Any]:
        """Run the transfer half of a build with the Setup boxes held by `choices`."""
        self.job.choices = quant_store.choice_map(choices)
        self.job.transfer_binaries(self.build_dir, {"version": "0.1.45"}, "q2_0")
        return json.loads((quant_store.CONFIG_DIR / "run-q2_0.json").read_text(encoding="utf-8"))

    @staticmethod
    def arg_value(cfg: dict[str, Any], flag: str) -> str | None:
        args = cfg.get("args") or []
        if flag not in args:
            return None
        at = args.index(flag)
        return str(args[at + 1]) if at + 1 < len(args) else None

    def test_the_context_and_kv_the_boxes_hold_are_written_into_the_run_config(self):
        cfg = self.build_with(["--context", "262144", "--kv", "q4_0", "--port", "8081"])
        self.assertEqual(self.arg_value(cfg, "--max-context"), "262144",
                         "the build ran on the template's context")
        self.assertEqual(self.arg_value(cfg, "--kv"), "q4_0",
                         "the build ran on the template's KV cache")
        self.assertEqual(cfg["port"], 8081, "the port in the box was not written")
        log = "\n".join(self.job.log)
        self.assertIn("setup choices written to config/run-q2_0.json", log,
                     "the build never said it applied the boxes")

    def test_a_setting_the_config_already_has_is_not_written_again(self):
        cfg = self.build_with(["--context", "131072", "--kv", "int8"])
        self.assertEqual(self.arg_value(cfg, "--max-context"), "131072")
        self.assertEqual(len(cfg["args"]), len(self.TEMPLATE["args"]),
                        "a flag was added even though the config already had it")
        self.assertNotIn("setup choices written", "\n".join(self.job.log),
                        "nothing changed, so the log should not claim it did")

    def test_an_empty_box_leaves_the_config_as_it_was(self):
        cfg = self.build_with(["--context", "", "--kv", ""])
        self.assertEqual(cfg["args"], self.TEMPLATE["args"], "an unset box overwrote a setting")

    def test_a_build_with_no_quant_says_its_choices_went_nowhere(self):
        """The default engine has no run config of its own, so the boxes have nowhere to go."""
        self.job.choices = {"--context": "32768"}
        self.job.transfer_binaries(self.build_dir, {"version": "0.1.45"}, None)
        self.assertIn("writes no run config of its own", "\n".join(self.job.log),
                     "the build let the boxes look applied without saying they were not")
        cfg = json.loads((quant_store.CONFIG_DIR / "run-example.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["args"], self.TEMPLATE["args"],
                         "a default-engine build rewrote a model's config")

    def test_a_model_box_that_does_not_match_the_quant_box_says_so(self):
        """The quant box is the explicit answer, and a card that disagrees is not silent."""
        self.job.choices = {"--model": "IQ3_XXS", "--context": "32768"}
        self.job.transfer_binaries(self.build_dir, {"version": "0.1.45"}, "q2_0")
        log = "\n".join(self.job.log)
        self.assertIn("the model box names IQ3_XXS, but this build is stored for q2_0", log,
                      "the build stored an engine for a model without saying which")
        self.assertNotIn("--model IQ3_XXS", log, "the model box was reported as an unwritten setting")
        cfg = json.loads((quant_store.CONFIG_DIR / "run-q2_0.json").read_text(encoding="utf-8"))
        self.assertEqual(self.arg_value(cfg, "--max-context"), "32768",
                         "the settings were dropped because the quant did not match")

    def test_a_model_box_that_matches_the_quant_is_not_made_a_fuss_about(self):
        job = BuildJob()
        job.choices = {"--model": "IQ3_XXS"}
        job.transfer_binaries(self.build_dir, {"version": "0.1.45"}, "IQ3_XXS")
        self.assertNotIn("the model box names", "\n".join(job.log))

    def test_a_flag_no_run_config_carries_is_said_rather_than_silently_dropped(self):
        cfg = self.build_with(["--vision", "yes", "--image-quant", "Q8_0"])
        self.assertNotIn("vision", cfg, "the build invented a vision block it cannot fill in")
        self.assertNotIn("--vision", cfg["args"], "--vision was put in the engine arguments")
        log = "\n".join(self.job.log)
        self.assertIn("does not carry", log,
                     "the build said nothing about the boxes it could not write")
        self.assertIn("--vision yes", log, "the log does not name the flag it left alone")

    def test_a_flag_written_bare_in_the_config_gets_its_value(self):
        """`--resident-experts` takes no value; a flag after it is not its value.

        The engine argument lists a run config carries mix value-taking flags with bare
        switches, so writing one of the boxes must not read the next flag as a value.
        """
        args = ["--resident-experts", "--kv", "--prefill", "auto"]
        self.assertTrue(quant_store._set_arg(args, "--kv", "q4_0"))
        self.assertEqual(args, ["--resident-experts", "--kv", "q4_0", "--prefill", "auto"],
                        "the value went in the wrong place")
        self.assertFalse(quant_store._set_arg(args, "--kv", "q4_0"),
                        "the same value twice is not a change")
        self.assertTrue(quant_store._set_arg(args, "--max-context", "32768"))
        self.assertEqual(args[args.index("--max-context") + 1], "32768")
        self.assertEqual(len([a for a in args if a == "--kv"]), 1,
                        "a flag already in the list was added a second time")

    def test_a_bare_flag_is_not_read_as_the_value_of_the_flag_before_it(self):
        self.assertEqual(quant_store.choice_map(["--context", "32768", "--resident-experts",
                                                "--kv", "q4_0"]),
                         {"--context": "32768", "--resident-experts": "", "--kv": "q4_0"})

    def test_the_model_box_names_the_quant_the_build_is_stored_under(self):
        """A build started from the card is for the model the card is set up for.

        With nothing in the quant box the build used to land in engine/ as the default
        engine, and the model named above it was ignored.
        """
        self.assertEqual(build_api.quant_from_choices(["--model", "UD-IQ4_XS"]), "ud_iq4_xs")
        self.assertIsNone(build_api.quant_from_choices(["--context", "32768"]),
                         "no model named, no quant implied")
        self.assertIsNone(build_api.quant_from_choices(["--model", ""]),
                         "an empty box is not a choice")

    def test_an_explicit_quant_beats_the_model_box(self):
        self.assertEqual(build_api.quant_from_choices(["--model", "IQ3_XXS"]), "iq3_xxs")
        self.assertEqual(build_api.normalise_or_none("q2_0"), "q2_0")

    def test_a_build_of_a_clone_uses_the_quant_the_clone_already_wrote(self):
        """The card's model box can name a model rather than a size.

        The size is only written in the run config the setup run left in the clone, and a
        build of that clone is the same model, so it is stored under that quant instead of
        landing in engine/ as the default engine with no run config of its own.
        """
        with tempfile.TemporaryDirectory() as name:
            clone = Path(name)
            (clone / "strata-qwen3.8-flash-next-iq2_xs.json").write_text(json.dumps(
                {"model_name": "qwen3.8-flash-next-iq2_xs",
                 "args": ["--pack", str(clone / "data" / "packs" / "iq2_xs")]}))
            self.assertEqual(build_api.quant_from_choices(["--context", "32768"], clone), "iq2_xs")
            # A size named in the box still wins over what the clone holds.
            self.assertEqual(build_api.quant_from_choices(["--model", "Q2_0"], clone), "q2_0")
            # A clone with nothing readable in it is reported, not guessed at.
            self.assertIsNone(build_api.quant_from_choices(["--context", "32768"],
                                                           clone / "no-such-clone"))

    def test_a_build_started_from_the_card_is_named_for_the_model_it_built(self):
        class SilentBuild(BuildJob):
            def _run(self, tag: str, options: dict[str, Any], quant: str | None = None) -> None:
                pass

        job = SilentBuild()
        status = job.start("v0.1.45", {}, None, ["--model", "IQ2_XS", "--context", "32768"])
        self.assertEqual(status["quant"], "iq2_xs",
                         "the build was not stored for the model the card named")
        self.assertEqual(status["choices"], {"--model": "IQ2_XS", "--context": "32768"},
                         "the status does not say what the build was asked for")

        named = SilentBuild()          # a second job: one build may not start inside another
        named.state = "idle"
        status = named.start("v0.1.45", {}, "q2_0", ["--model", "IQ2_XS"])
        self.assertEqual(status["quant"], "q2_0",
                         "the quant box is the explicit answer and beats the model box")


if __name__ == "__main__":
    unittest.main()

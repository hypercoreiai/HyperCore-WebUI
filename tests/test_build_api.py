"""tests/test_build_api.py - the Build tab's background job.

Two contracts the Build tab depends on: the log the tab tails is readable from a
byte offset (so a poll appends instead of re-sending), and a build that fails only
on the HyperCore worker target still produces an engine and says so in
engine/BUILD.json. Neither needs cmake or a GPU: the subprocess is stubbed.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any, ClassVar

from serve import build_api
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


if __name__ == "__main__":
    unittest.main()

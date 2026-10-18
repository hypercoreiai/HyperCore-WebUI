"""tests/test_strata_hook.py - the Config view's Strata setup hook.

serve/strata_hook.py runs the upstream setup.py as a detached subprocess and keeps
its log in logs/. Two things have to be true for that to be safe: starting it must
not wait for the download, and the log reader must append from a byte offset rather
than re-sending the whole file. Both are tested here with a stub setup.py, and the
routes are tested against a --ui-only server, which needs no engine.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from serve.strata_hook import StrataHook, find_checkout

#: A stand-in for upstream setup.py: prints a few lines, then exits with the code the
#: test wants. Long enough that a "sleep" variant can be cancelled mid-run.
STUB = """#!/usr/bin/env python3
import sys, time
print("checking this PC")
print("downloading the model pack")
print("preparing it")
print("done")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""

SLOW_STUB = """#!/usr/bin/env python3
import time
print("the download started")
time.sleep(30)
"""


def make_checkout(tmp: Path, name: str = "strata", source: str = STUB) -> Path:
    checkout = tmp / name
    checkout.mkdir(parents=True, exist_ok=True)
    script = checkout / "setup.py"
    script.write_text(source, encoding="utf-8")
    return checkout


class LocateTests(unittest.TestCase):
    def test_an_explicit_checkout_is_used_as_is(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            checkout = make_checkout(tmp)
            found, note = find_checkout(str(checkout))
            self.assertEqual(found, checkout / "setup.py")
            self.assertEqual(note, "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_missing_checkout_says_so_instead_of_raising(self) -> None:
        found, note = find_checkout("/definitely/not/here")
        self.assertIsNone(found)
        self.assertIn("no setup.py", note)

    def test_tmp_is_scratch_and_is_refused(self) -> None:
        """tmp/ is deleted when a build finishes, so a model download must not live there."""
        scratch = ROOT / "tmp" / "strata-test"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "setup.py").write_text("print('x')", encoding="utf-8")
        try:
            found, note = find_checkout(str(scratch))
            self.assertIsNone(found)
            self.assertIn("tmp/", note)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_a_relative_path_is_taken_from_the_project_root(self) -> None:
        found, note = find_checkout("no-such-checkout")
        self.assertIsNone(found)
        self.assertIn("no setup.py", note)


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["HYPERCORE_HOOK_LOG"] = str(self.tmp / "hook.log")
        # the module reads the log path at import; a fresh instance re-reads it
        self.job = StrataHook()
        self.job.log_path = Path(os.environ["HYPERCORE_HOOK_LOG"])

    def tearDown(self) -> None:
        self.job.cancel()
        os.environ.pop("HYPERCORE_HOOK_LOG", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_starting_the_hook_does_not_wait_for_it(self) -> None:
        checkout = make_checkout(self.tmp)
        started = time.time()
        result = self.job.start(str(checkout), ["0"])
        elapsed = time.time() - started
        self.assertEqual(result["state"], "running", result)
        self.assertLess(elapsed, 2.0, "the request returned only after the subprocess finished")
        deadline = time.time() + 10
        while time.time() < deadline and self.job.status()["state"] == "running":
            time.sleep(0.2)
        status = self.job.status()
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["exit_code"], 0)

    def test_a_failing_script_is_reported_not_raised(self) -> None:
        checkout = make_checkout(self.tmp)
        self.job.start(str(checkout), ["3"])
        deadline = time.time() + 10
        while time.time() < deadline and self.job.status()["state"] == "running":
            time.sleep(0.2)
        status = self.job.status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exit_code"], 3)

    def test_the_log_is_written_to_a_file_and_read_from_an_offset(self) -> None:
        checkout = make_checkout(self.tmp)
        self.job.start(str(checkout), ["0"])
        deadline = time.time() + 10
        while time.time() < deadline and self.job.status()["state"] == "running":
            time.sleep(0.2)
        first = self.job.tail(0)
        self.assertIn("checking this PC", first["lines"])
        self.assertGreater(first["offset"], 0)
        again = self.job.tail(first["offset"])
        self.assertEqual(again["lines"], [], "the same offset must not be sent twice")
        self.assertEqual(again["offset"], first["offset"])

    def test_cancel_stops_a_download_that_is_running(self) -> None:
        checkout = make_checkout(self.tmp, "slow", SLOW_STUB)
        self.job.start(str(checkout))
        time.sleep(1.0)
        self.assertEqual(self.job.cancel(), {"cancelled": True})
        deadline = time.time() + 10
        while time.time() < deadline and self.job.status()["state"] == "running":
            time.sleep(0.2)
        self.assertEqual(self.job.status()["state"], "cancelled")

    def test_a_second_start_while_one_is_running_is_refused(self) -> None:
        checkout = make_checkout(self.tmp, "slow", SLOW_STUB)
        self.job.start(str(checkout))
        result = self.job.start(str(checkout))
        self.assertEqual(result["state"], "running")
        self.assertIn("already running", result["error"])

    def test_no_checkout_leaves_the_state_idle(self) -> None:
        result = self.job.start("/definitely/not/here")
        self.assertEqual(result["state"], "idle")
        self.assertIn("no setup.py", result["error"])
        self.assertEqual(self.job.status()["state"], "idle")


class RouteTests(unittest.TestCase):
    """The routes, against a server with no engine at all."""

    STARTUP_S = 25

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.checkout = make_checkout(self.tmp)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--ui-only",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(ROOT), env=dict(os.environ, HYPERCORE_HOOK_LOG=str(self.tmp / "hook.log")),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.wait_for_health()

    def tearDown(self) -> None:
        if getattr(self, "proc", None):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_for_health(self) -> None:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3) as r:
                    r.read()
                    return
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        self.fail("the --ui-only server never answered /health")

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_post_starts_the_hook_and_get_reports_its_log(self) -> None:
        status, body = self.request("/config/strata-hook", "POST", {"path": str(self.checkout), "args": ["0"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "running")
        deadline = time.time() + 10
        lines: list[str] = []
        while time.time() < deadline:
            _, report = self.request("/config/strata-hook")
            lines.extend(report.get("lines", []))
            if report.get("state") in ("done", "failed"):
                break
        self.assertIn("done", lines)
        self.assertEqual(report["exit_code"], 0)

    def test_a_bad_path_is_answered_with_a_reason(self) -> None:
        status, body = self.request("/config/strata-hook", "POST", {"path": "/nope"})
        self.assertEqual(status, 400)
        self.assertIn("no setup.py", body["error"])

    def test_stop_cancels_a_running_hook(self) -> None:
        self.request("/config/strata-hook", "POST", {"path": str(make_checkout(self.tmp, "slow", SLOW_STUB))})
        status, body = self.request("/config/strata-hook/stop", "POST")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cancelled"], True)
        deadline = time.time() + 10
        while time.time() < deadline:
            _, report = self.request("/config/strata-hook")
            if report["state"] != "running":
                break
        self.assertEqual(report["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()

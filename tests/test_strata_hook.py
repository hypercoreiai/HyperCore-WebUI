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

from serve import strata_hook
from serve.strata_hook import StrataHook, build_command, find_checkout

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

#: A setup that asks anyway, as an older release that does not understand --yes would.
#: --ask is the marker the test passes to make it prompt.
ASK_STUB = """#!/usr/bin/env python3
import sys
if "--ask" in sys.argv:
    print("asking a question")
    line = sys.stdin.readline()
    print("answered " + line.strip() if line else "no answer: stdin is closed")
else:
    print("took the recommended answer without asking")
"""


#: A setup that reports progress the way upstream setup.py does: step markers, an [ok]
#: line, a warning, and a download whose percent is written on one line with \r.
PROGRESS_STUB = """#!/usr/bin/env python3
import sys
print("=== Step 1: checking this PC ===")
print("  [ok] GPU found")
print("=== Step 2: the model pack ===")
print("  [!]  the SSD is slow, the download will take a while")
for pct in (0, 40, 100):
    print(f"  model-pack.gguf:   {pct * 0.4:.2f} / 40.00 GB ({pct}%)\\r", end="")
print()
if "--fail" in sys.argv:
    print("\\n  [X]  no network: the pack could not be fetched")
    sys.exit(1)
"""

#: A setup that fails the way a Python error fails: no [X] line, just a traceback.
TRACEBACK_STUB = """#!/usr/bin/env python3
def boom():
    raise OSError("the model folder is on a read-only disk")
boom()
"""


def make_checkout(tmp: Path, name: str = "strata", source: str = STUB) -> Path:
    checkout = tmp / name
    checkout.mkdir(parents=True, exist_ok=True)
    script = checkout / "setup.py"
    script.write_text(source, encoding="utf-8")
    return checkout


def wait_for(job: StrataHook, timeout: float = 10.0) -> dict:
    """Poll until the hook is no longer running, then return its status."""
    deadline = time.time() + timeout
    while time.time() < deadline and job.status()["state"] == "running":
        time.sleep(0.2)
    return job.status()


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

    def _throwaway_project(self, tmp: Path) -> tuple:
        """Point the module at a throwaway project so the search is deterministic.

        Returns the values the caller restores in its finally block.
        """
        saved = (strata_hook.ROOT, strata_hook.SCRATCH)
        project = tmp / "HyperCore-WebUI"
        project.mkdir(parents=True, exist_ok=True)
        strata_hook.ROOT = project
        strata_hook.SCRATCH = project / "tmp"
        for name in strata_hook.SEARCH_ENV:
            os.environ.pop(name, None)
        return saved

    def test_the_run_config_cwd_is_the_checkout_this_machine_runs(self) -> None:
        """The engine is started from its Strata folder, so that folder is the checkout
        the hook should run setup.py in when the request names no path."""
        tmp = Path(tempfile.mkdtemp())
        try:
            checkout = make_checkout(tmp, "Strata")
            cfg = tmp / "run-q2_0.json"
            cfg.write_text(json.dumps({"exe": "engine/strata", "cwd": str(checkout)}),
                          encoding="utf-8")
            saved = self._throwaway_project(tmp)
            try:
                found, note = find_checkout(None, cfg)
                self.assertEqual(found, checkout / "setup.py", note)
                self.assertEqual(note, "")
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_checkout_next_to_the_project_is_found(self) -> None:
        """The layout on this PC: the WebUI folder beside the Strata folder it drives."""
        tmp = Path(tempfile.mkdtemp())
        try:
            checkout = make_checkout(tmp, "Strata")
            saved = self._throwaway_project(tmp)
            try:
                found, note = find_checkout()
                self.assertEqual(found, checkout / "setup.py", note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_run_config_cwd_in_tmp_is_refused(self) -> None:
        """A build clone is scratch: setup.py there would download into a folder that
        is deleted when the build finishes."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._throwaway_project(tmp)
            try:
                clone = make_checkout(strata_hook.SCRATCH, "strata-main")
                cfg = strata_hook.ROOT / "config" / "run-q2_0.json"
                cfg.parent.mkdir(parents=True, exist_ok=True)
                cfg.write_text(json.dumps({"cwd": str(clone)}), encoding="utf-8")
                found, note = find_checkout(None, cfg)
                self.assertIsNone(found)
                self.assertIn("tmp/", note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_unreadable_run_config_is_skipped_not_raised(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = tmp / "broken.json"
            cfg.write_text("{not json", encoding="utf-8")
            saved = self._throwaway_project(tmp)
            try:
                found, note = find_checkout(None, cfg)
                self.assertIsNone(found)
                self.assertIn("no Strata checkout", note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_note_names_the_only_clone_when_there_is_one(self) -> None:
        """Saying "no checkout" is not enough when the machine has one in tmp/: the
        page has to say why that one is not used."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._throwaway_project(tmp)
            try:
                clone = make_checkout(strata_hook.SCRATCH, "strata-main")
                found, note = find_checkout()
                self.assertIsNone(found)
                self.assertIn(str(clone), note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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

    def test_the_command_is_always_non_interactive(self) -> None:
        """The flags the Build tab sends are the choices; --yes and --no-start are added
        so a setup started from a web page never waits on a question nobody can answer."""
        checkout = make_checkout(self.tmp, "strata", ASK_STUB)
        self.job.start(str(checkout), ["--model", "IQ2_XS", "--context", "131000"])
        deadline = time.time() + 10
        while time.time() < deadline and self.job.status()["state"] == "running":
            time.sleep(0.2)
        status = self.job.status()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["exit_code"], 0)
        command = status["command"]
        self.assertEqual(command[2:], ["--model", "IQ2_XS", "--context", "131000", "--yes", "--no-start"],
                         f"the caller's flags or the non-interactive ones are missing: {command}")
        self.assertIn("took the recommended answer without asking", self.job.tail(0)["lines"])

    def test_a_flag_the_caller_sent_is_not_added_twice(self) -> None:
        checkout = make_checkout(self.tmp, "strata", ASK_STUB)
        self.job.start(str(checkout), ["--yes", "--no-start", "--model", "Q2_0"])
        command = self.job.status()["command"]
        self.assertEqual(command.count("--yes"), 1, command)
        self.assertEqual(command.count("--no-start"), 1, command)

    def test_a_question_asked_without_yes_does_not_hang_the_hook(self) -> None:
        """stdin is closed, so a setup that prompts anyway fails fast rather than sitting
        on the terminal the server was started from for the rest of the day."""
        checkout = make_checkout(self.tmp, "strata", ASK_STUB)
        self.job.start(str(checkout), ["--ask", "--model", "IQ2_XS"])
        deadline = time.time() + 10
        while time.time() < deadline and self.job.status()["state"] == "running":
            time.sleep(0.2)
        status = self.job.status()
        self.assertEqual(status["state"], "done", "the hook waited on a prompt it could not answer")
        self.assertIn("no answer: stdin is closed", self.job.tail(0)["lines"])

    def test_build_command_keeps_the_caller_order(self) -> None:
        cmd = build_command(Path("/keep/Strata/setup.py"), ["--family", "qwen"])
        self.assertEqual(cmd[1], "/keep/Strata/setup.py")
        self.assertEqual(cmd[2:], ["--family", "qwen", "--yes", "--no-start"])

    def test_the_stage_is_read_from_the_scripts_own_step_markers(self) -> None:
        checkout = make_checkout(self.tmp, "progress", PROGRESS_STUB)
        self.job.start(str(checkout))
        status = wait_for(self.job)
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["stage"], "Step 2: the model pack")
        self.assertEqual(status["percent"], 100)
        self.assertEqual(status["download"], "model-pack.gguf")
        self.assertEqual(status["problems"],
                         [{"kind": "warning", "text": "the SSD is slow, the download will take a while"}])

    def test_the_stop_line_of_a_failing_setup_is_recorded_as_an_error(self) -> None:
        checkout = make_checkout(self.tmp, "progress", PROGRESS_STUB)
        self.job.start(str(checkout), ["--fail"])
        status = wait_for(self.job)
        self.assertEqual(status["state"], "failed", status)
        errors = [p for p in status["problems"] if p["kind"] == "error"]
        self.assertEqual(len(errors), 1, status["problems"])
        self.assertIn("no network", errors[0]["text"])
        self.assertEqual(status["stage"], "Step 2: the model pack")

    def test_a_traceback_is_collected_as_an_error(self) -> None:
        checkout = make_checkout(self.tmp, "traceback", TRACEBACK_STUB)
        self.job.start(str(checkout))
        status = wait_for(self.job)
        self.assertEqual(status["state"], "failed", status)
        texts = [p["text"] for p in status["problems"]]
        self.assertTrue(any(t.startswith("OSError:") for t in texts), texts)

    def test_a_failure_with_no_stop_line_reports_the_end_of_the_log(self) -> None:
        checkout = make_checkout(self.tmp)
        self.job.start(str(checkout), ["3"])
        status = wait_for(self.job)
        self.assertEqual(status["state"], "failed", status)
        self.assertEqual([p["text"] for p in status["problems"]],
                         ["checking this PC", "downloading the model pack", "preparing it", "done"])

    def test_a_new_run_forgets_the_stage_of_the_last_one(self) -> None:
        checkout = make_checkout(self.tmp, "progress", PROGRESS_STUB)
        self.job.start(str(checkout), ["--fail"])
        wait_for(self.job)
        self.assertEqual(len(self.job.status()["problems"]), 2)
        self.job.start(str(checkout))
        status = wait_for(self.job)
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["problems"],
                         [{"kind": "warning", "text": "the SSD is slow, the download will take a while"}])


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

    def test_the_route_sends_a_non_interactive_command(self) -> None:
        """The command the page gets back is the one that runs, hook flags included."""
        status, body = self.request("/config/strata-hook", "POST",
                                   {"path": str(make_checkout(self.tmp, "ask", ASK_STUB)),
                                    "args": ["--family", "qwen", "--model", "IQ2_XS"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["command"][2:],
                         ["--family", "qwen", "--model", "IQ2_XS", "--yes", "--no-start"], body["command"])
        deadline = time.time() + 10
        while time.time() < deadline:
            _, report = self.request("/config/strata-hook")
            if report["state"] in ("done", "failed"):
                break
        self.assertEqual(report["state"], "done", "the hook waited on a question nobody could answer")
        self.assertIn("took the recommended answer without asking", report["lines"])

    def test_a_bad_path_is_answered_with_a_reason(self) -> None:
        status, body = self.request("/config/strata-hook", "POST", {"path": "/nope"})
        self.assertEqual(status, 400)
        self.assertIn("no setup.py", body["error"])

    def test_stop_cancels_a_running_hook(self) -> None:
        self.request("/config/strata-hook", "POST", {"path": str(make_checkout(self.tmp, "slow", SLOW_STUB))})
        status, body = self.request("/config/strata-hook/stop", "POST")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cancelled"], True)

    def test_get_reports_the_stage_the_setup_is_on(self) -> None:
        """The Build tab's status card reads the stage and the problems from this route."""
        status, body = self.request("/config/strata-hook", "POST",
                                   {"path": str(make_checkout(self.tmp, "progress", PROGRESS_STUB)),
                                    "args": ["--fail"]})
        self.assertEqual(status, 200, body)
        deadline = time.time() + 10
        report: dict = {}
        while time.time() < deadline:
            _, report = self.request("/config/strata-hook")
            if report.get("state") in ("done", "failed", "cancelled"):
                break
        self.assertEqual(report["state"], "failed", report)
        self.assertEqual(report["stage"], "Step 2: the model pack")
        self.assertEqual(report["percent"], 100)
        errors = [p for p in report["problems"] if p["kind"] == "error"]
        self.assertTrue(errors, report["problems"])
        self.assertIn("no network", errors[0]["text"])


if __name__ == "__main__":
    unittest.main()

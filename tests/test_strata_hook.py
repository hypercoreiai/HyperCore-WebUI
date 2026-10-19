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

from serve import quant_store, strata_hook
from serve.strata_hook import (
    StrataHook,
    accepted_flags,
    build_command,
    find_checkout,
    strip_unaccepted,
)

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

    def test_an_explicit_clone_in_tmp_is_taken(self) -> None:
        """A build no longer deletes its clone, so a path the user names inside tmp/ is
        a checkout like any other - the packs it downloads are pinned outside tmp/.
        See ScratchRunTests for the pin."""
        scratch = ROOT / "tmp" / "strata-test"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "setup.py").write_text("print('x')", encoding="utf-8")
        try:
            found, note = find_checkout(str(scratch))
            self.assertEqual(found, scratch / "setup.py")
            self.assertEqual(note, "")
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

    def test_a_run_config_pointing_at_the_clone_still_finds_a_checkout(self) -> None:
        """A run config whose cwd is the build clone is not thrown away: the clone is
        the only Strata on a box that has never been set up by hand, and the hook runs
        its setup.py with the packs pinned outside tmp/."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._throwaway_project(tmp)
            try:
                clone = make_checkout(strata_hook.SCRATCH, "strata-main")
                cfg = strata_hook.ROOT / "config" / "run-q2_0.json"
                cfg.parent.mkdir(parents=True, exist_ok=True)
                cfg.write_text(json.dumps({"cwd": str(clone)}), encoding="utf-8")
                found, note = find_checkout(None, cfg)
                self.assertEqual(found, clone / "setup.py", note)
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

    def test_the_note_names_every_folder_the_hook_looked_in(self) -> None:
        """When there is truly no checkout, the page has to say where it looked, so the
        user can put one in a folder the hook already knows."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._throwaway_project(tmp)
            try:
                found, note = find_checkout()
                self.assertIsNone(found)
                self.assertIn("no Strata checkout", note)
                self.assertIn(str(strata_hook.ROOT / "strata"), note)
                self.assertIn(str(strata_hook.ROOT.parent / "Strata"), note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_clone_is_used_when_it_is_the_only_checkout(self) -> None:
        """The machine this project ships on: the only Strata is the clone tmp/ keeps."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._throwaway_project(tmp)
            try:
                clone = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45")
                found, note = find_checkout()
                self.assertEqual(found, clone / "setup.py", note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_checkout_the_user_keeps_beats_the_clone(self) -> None:
        """The clone is the last resort: a folder beside the project wins over one the
        next build removes."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._throwaway_project(tmp)
            try:
                kept = make_checkout(tmp, "Strata")
                clone = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45")
                found, note = find_checkout()
                self.assertEqual(found, kept / "setup.py", note)
                self.assertNotEqual(found, clone / "setup.py")
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_checkout_above_a_nested_project_is_found(self) -> None:
        """~/test_webUI/HyperCore-WebUI driving ~/Strata: the checkout is two levels up,
        not beside the project, and the search has to reach it."""
        tmp = Path(tempfile.mkdtemp())
        try:
            checkout = make_checkout(tmp, "Strata")
            saved = self._throwaway_project(tmp / "test_webUI")
            try:
                found, note = find_checkout()
                self.assertEqual(found, checkout / "setup.py", note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_newest_clone_is_the_one_used(self) -> None:
        """tmp/ can hold more than one release; the setup runs against the newest."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._throwaway_project(tmp)
            try:
                make_checkout(strata_hook.SCRATCH, "strata-v0.1.45")
                newer = make_checkout(strata_hook.SCRATCH, "strata-v0.2.0-beta")
                found, note = find_checkout()
                self.assertEqual(found, newer / "setup.py", note)
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


#: A setup that reports the data folder it was told to use. The hook pins --data-dir
#: when it runs a clone out of tmp/, and this is how a test reads back what it was given.
DATA_STUB = """#!/usr/bin/env python3
import sys
print("setup ran from " + __file__)
flags = [i for i, a in enumerate(sys.argv) if a in ("--data-dir", "--models-dir")]
print("data dir: " + (sys.argv[flags[0] + 1] if flags else "setup.py's own default"))
"""


#: A release whose setup.py declares fewer flags than the Build tab's boxes offer: the
#: choices were read from a newer release, and this is the checkout the box actually has.
OLD_RELEASE_STUB = """#!/usr/bin/env python3
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--family", choices=["qwen", "orca"])
ap.add_argument("--vision", choices=["yes", "no"])
ap.add_argument("--data-dir")
ap.add_argument("--yes", action="store_true")
ap.add_argument("--no-start", action="store_true")
a = ap.parse_args()
print("parsed: " + ", ".join(sorted(k for k, v in vars(a).items() if v)))
"""


class ScratchRunTests(unittest.TestCase):
    """A setup started from the clone in tmp/ must not put its packs in tmp/.

    These run a stub setup.py for real, so they check the command the hook builds and
    what the status card reports, not just the folder search.
    """

    def _project(self, tmp: Path) -> tuple:
        """Point the hook at a throwaway project, and clear the environment so a real
        HYPERCORE_STRATA_SRC on the box cannot make a test pass by accident."""
        saved = (strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH)
        project = tmp / "HyperCore-WebUI"
        project.mkdir(parents=True, exist_ok=True)
        strata_hook.ROOT = project
        strata_hook.SCRATCH = project / "tmp"
        strata_hook.LOG_PATH = project / "logs" / "hook.log"
        for name in strata_hook.SEARCH_ENV + strata_hook.DATA_ENV:
            os.environ.pop(name, None)
        return saved

    def test_a_clone_gets_a_data_dir_outside_tmp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                setup = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", DATA_STUB)
                cmd = build_command(setup, ["--model", "IQ2_XS"], strata_hook.durable_data_dir())
                self.assertEqual(cmd[-6:-2],
                                 ["--model", "IQ2_XS", "--data-dir", str(tmp / "Strata-data")])
                self.assertEqual(cmd[-2:], ["--yes", "--no-start"])
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_caller_that_named_its_own_data_dir_is_not_overridden(self) -> None:
        cmd = build_command(Path("/nowhere/setup.py"), ["--data-dir", "/models/here"],
                            Path("/elsewhere/Strata-data"))
        self.assertEqual(cmd.count("--data-dir"), 1)
        self.assertIn("/models/here", cmd)

    def test_a_run_from_a_clone_reports_where_the_packs_went(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", DATA_STUB)
                job = StrataHook()
                job.log_path = tmp / "hook.log"
                self.assertEqual(job.start()["state"], "running")
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                self.assertTrue(status["scratch"])
                self.assertEqual(status["data_dir"], str(tmp / "Strata-data"))
                self.assertIn(f"data dir: {tmp / 'Strata-data'}", job.tail(0)["lines"])
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_kept_checkout_is_left_with_the_default_data_dir(self) -> None:
        """A checkout the user keeps already has its own data folder beside it; the hook
        does not second-guess it."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                make_checkout(tmp, "Strata", DATA_STUB)
                job = StrataHook()
                job.log_path = tmp / "hook.log"
                self.assertEqual(job.start()["state"], "running")
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                self.assertFalse(status["scratch"])
                self.assertEqual(status["data_dir"], "")
                self.assertNotIn("--data-dir", status["command"])
            finally:
                strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_data_dir_inside_tmp_is_not_used(self) -> None:
        """An override that points back into tmp/ is worse than setup.py's own default:
        the packs would go with the clone after all."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                os.environ["HYPERCORE_STRATA_DATA"] = str(strata_hook.SCRATCH / "Strata-data")
                make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", DATA_STUB)
                job = StrataHook()
                job.log_path = tmp / "hook.log"
                self.assertEqual(job.start()["state"], "running")
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                self.assertEqual(status["data_dir"], "")
                self.assertNotIn("--data-dir", status["command"])
            finally:
                os.environ.pop("HYPERCORE_STRATA_DATA", None)
                strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH = saved
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_clone_is_refused_while_a_build_is_running(self) -> None:
        """setup.py installs and downloads into the checkout; running it while the Build
        tab is compiling that same clone would corrupt the build."""
        from serve import build_api
        tmp = Path(tempfile.mkdtemp())
        saved_state = getattr(build_api.JOB, "state", "idle")
        try:
            saved = self._project(tmp)
            try:
                clone = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", DATA_STUB)
                build_api.JOB.state = "building"
                job = StrataHook()
                job.log_path = tmp / "hook.log"
                result = job.start()
                self.assertEqual(result["state"], "idle", result)
                self.assertIn("build is using", result["error"])
                self.assertIn(str(clone), result["error"])
            finally:
                build_api.JOB.state = saved_state
                strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH = saved
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

    def test_a_flag_this_release_never_declared_is_dropped_with_its_value(self) -> None:
        """The boxes were read from a release that has image generation; the checkout on
        the box does not. setup.py would have died in argparse before doing anything."""
        setup = make_checkout(self.tmp, "oldrelease", OLD_RELEASE_STUB) / "setup.py"
        kept, dropped = strip_unaccepted(setup, ["--family", "qwen", "--images", "no",
                                                  "--vision", "no"])
        self.assertEqual(kept, ["--family", "qwen", "--vision", "no"], kept)
        self.assertEqual(dropped, ["--images", "no"], dropped)
        self.assertEqual(build_command(setup, ["--images", "no"])[2:],
                         ["--yes", "--no-start"], "the dropped pair reached the command")

    def test_a_flag_the_release_has_is_sent_as_it_was_asked(self) -> None:
        setup = ROOT / "tmp" / "strata-v0.1.45" / "setup.py"
        if not setup.is_file():
            self.skipTest("the 0.1.45 clone is not on this box")
        flags = accepted_flags(setup) or set()
        self.assertIn("--images", flags, "the clone's own setup.py declares --images")
        kept, dropped = strip_unaccepted(setup, ["--images", "no", "--image-quant", "Q4_K_M"])
        self.assertEqual((kept, dropped),
                         (["--images", "no", "--image-quant", "Q4_K_M"], []))

    def test_flag_equals_value_is_dropped_whole(self) -> None:
        setup = make_checkout(self.tmp, "equals", OLD_RELEASE_STUB) / "setup.py"
        kept, dropped = strip_unaccepted(setup, ["--images=qwen-image", "--yes"])
        self.assertEqual(kept, ["--yes"], kept)
        self.assertEqual(dropped, ["--images=qwen-image"], dropped)

    def test_a_script_whose_parser_cannot_be_read_is_sent_as_is(self) -> None:
        """A stub, a wrapper, or a release that builds its parser in a helper: nothing is
        guessed at, so the caller's flags go through untouched."""
        setup = make_checkout(self.tmp, "noparser", STUB) / "setup.py"
        self.assertIsNone(accepted_flags(setup))
        kept, dropped = strip_unaccepted(setup, ["--images", "no"])
        self.assertEqual((kept, dropped), (["--images", "no"], []))

    def test_a_dropped_flag_is_reported_and_the_run_still_finishes(self) -> None:
        checkout = make_checkout(self.tmp, "oldrun", OLD_RELEASE_STUB)
        self.job.start(str(checkout), ["--family", "qwen", "--images", "no"])
        status = wait_for(self.job)
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["exit_code"], 0, "argparse still saw a flag it cannot parse")
        self.assertEqual(status["dropped"], ["--images", "no"], status)
        lines = self.job.tail(0)["lines"]
        self.assertFalse(any("unrecognized arguments" in line for line in lines), lines)
        self.assertTrue(any("does not accept --images" in line for line in lines), lines)
        self.assertTrue(any(p["kind"] == "warning" for p in status["problems"]), status["problems"])

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
        #: The shipped run configs carry an API key, and every route that is not /health
        #: or /metrics is gated by it. The hook is tested against a keyless config so the
        #: assertions are about the hook, not the gate.
        (self.tmp / "hook-config.json").write_text(
            json.dumps({"exe": "engine/strata", "cwd": str(ROOT), "api_key": ""}),
            encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--ui-only",
             "--config", str(self.tmp / "hook-config.json"),
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

    def test_the_status_card_carries_the_clone_fields(self) -> None:
        """scratch and data_dir are what the page shows when the setup is running in the
        clone a build keeps: the packs go to data_dir, not into tmp/."""
        status, body = self.request("/config/strata-hook", "POST", {"path": str(self.checkout)})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["scratch"], body)
        self.assertEqual(body["data_dir"], "", body)
        self.assertEqual(body["checkout"], str(self.checkout), body)

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


#: A setup that behaves the way upstream setup.py does: it writes everything into the
#: folder it is run from. Binaries into build/, runtime files into data/, the run script
#: beside itself. For a clone in tmp/ that is the folder the next build deletes, and the
#: project's config/ and engine/engine_<quant>/ get nothing - which is what the harvest
#: is for. It declares no flags, so the hook sends the run exactly what the card asked for.
HARVEST_STUB = """#!/usr/bin/env python3
import sys
from pathlib import Path
here = Path(__file__).resolve().parent
(here / "build").mkdir(exist_ok=True)
(here / "build" / "strata").write_text("engine binary")
(here / "build" / "libstrata_kq.so").write_text("kernels")
(here / "data").mkdir(exist_ok=True)
(here / "data" / "expert-profile.bin").write_text("profile")
print("=== Step 1: checking this PC ===")
print("  [ok] wrote " + str(here / "build" / "strata"))
sys.exit(1 if "--fail" in sys.argv else 0)
"""

#: A setup that writes what upstream writes beside itself: its run config, named for the
#: quant it chose. When the card's model box names a model rather than a quant, or is left
#: unset because the release decides the size per model, this file is the only place the
#: quant the run used is written - and it is what the store has to be keyed by.
HARVEST_CONFIG_STUB = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
(here / "build").mkdir(exist_ok=True)
(here / "build" / "strata").write_text("engine binary")
(here / "data").mkdir(exist_ok=True)
(here / "data" / "expert-profile.bin").write_text("profile")
cfg = {"model_name": "qwen3.8-flash-next-iq2_xs",
       "exe": str(here / "build" / "strata"),
       "args": ["--pack", str(here / "data" / "packs" / "iq2_xs")]}
(here / "strata-qwen3.8-flash-next-iq2_xs.json").write_text(json.dumps(cfg))
print("=== Step 7: writing the start script ===")
print("  [ok] start script: run-iq2_xs.sh")
sys.exit(1 if "--fail" in sys.argv else 0)
"""


class HarvestTests(unittest.TestCase):
    """A setup run in the clone in tmp/ has to leave its files in the project.

    setup.py writes into its own folder. When that folder is the clone a build keeps,
    the binaries, the runtime files and the run script all stay inside tmp/, so
    HyperCore-WebUI/config/ and HyperCore-WebUI/engine/engine_<quant>/ have nothing in
    them and the Model version dropdown has nothing to point at. The harvest moves them:
    binaries into engine/engine_<quant>/, the run config in config/, the runtime files in
    engine/engine_<quant>/data/, and the store's active pointer moved to that build.
    """

    def _project(self, tmp: Path) -> tuple:
        """Point both the hook and the store at a throwaway project."""
        saved = (strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH, strata_hook.LOGS,
                 quant_store.ROOT, quant_store.ENGINE_DIR, quant_store.INDEX_PATH,
                 quant_store.CONFIG_DIR)
        project = tmp / "HyperCore-WebUI"
        project.mkdir(parents=True, exist_ok=True)
        strata_hook.ROOT = project
        strata_hook.SCRATCH = project / "tmp"
        strata_hook.LOG_PATH = project / "logs" / "hook.log"
        strata_hook.LOGS = project / "logs"
        strata_hook.LOGS.mkdir(parents=True, exist_ok=True)
        quant_store.ROOT = project
        quant_store.ENGINE_DIR = project / "engine"
        quant_store.INDEX_PATH = project / "engine" / "VERSIONS.json"
        quant_store.CONFIG_DIR = project / "config"
        for name in strata_hook.SEARCH_ENV + strata_hook.DATA_ENV:
            os.environ.pop(name, None)
        return saved

    def _restore(self, saved: tuple) -> None:
        (strata_hook.ROOT, strata_hook.SCRATCH, strata_hook.LOG_PATH, strata_hook.LOGS,
         quant_store.ROOT, quant_store.ENGINE_DIR, quant_store.INDEX_PATH,
         quant_store.CONFIG_DIR) = saved

    def test_a_setup_run_in_the_clone_stores_its_files_in_the_project(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", HARVEST_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), ["--model", "IQ2_XS"])
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                stored = quant_store.store_dir("iq2_xs")
                self.assertTrue((stored / "strata").is_file(),
                                "the engine binary has to land in engine/engine_iq2_xs/")
                self.assertEqual((stored / "strata").read_text(), "engine binary")
                self.assertTrue((stored / "libstrata_kq.so").is_file(),
                                "the K-quant kernels travel with the engine")
                self.assertTrue((quant_store.CONFIG_DIR / "run-iq2_xs.json").is_file(),
                                "the run config has to be written in config/")
                self.assertEqual(status["harvest"]["quant"], "iq2_xs", status["harvest"])
                self.assertEqual(sorted(status["harvest"]["stored"]),
                                 ["libstrata_kq.so", "strata"], status["harvest"])
                self.assertIn("stored engine/engine_iq2_xs", "\n".join(job.tail(0)["lines"]),
                              "the log has to say where the files went")
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_runtime_files_of_a_clone_setup_are_carried_into_the_store(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", HARVEST_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), ["--model", "IQ2_XS"])
                status = wait_for(job)
                carried = quant_store.data_dir("iq2_xs") / "expert-profile.bin"
                self.assertTrue(carried.is_file(), "the clone's data/ has to be carried")
                self.assertEqual(status["harvest"]["data"], 1, status["harvest"])
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_run_that_named_a_model_not_a_quant_stores_under_the_quant_it_chose(self) -> None:
        """The card's model box holds a model name for some releases; the store is keyed
        by the quant, which the run wrote in its own config inside the clone.
        """
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", HARVEST_CONFIG_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), ["--model", "Qwen3.8-Flash-Next"])
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                self.assertEqual(status["harvest"]["quant"], "iq2_xs", status["harvest"])
                self.assertTrue((quant_store.store_dir("iq2_xs") / "strata").is_file(),
                                "the binary has to land in engine/engine_iq2_xs/")
                self.assertTrue((quant_store.CONFIG_DIR / "run-iq2_xs.json").is_file(),
                                "the run config has to be written in config/")
                lines = "\n".join(job.tail(0)["lines"])
                self.assertIn("stored engine/engine_iq2_xs", lines, lines)
                self.assertIn("run config the setup wrote", lines,
                              "the log has to say where the quant came from")
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_run_with_the_model_box_left_unset_stores_under_the_quant_it_chose(self) -> None:
        """A box the release decides per model is left unset, so the command names no
        quant. The run still chose one, and its config in the clone names it.
        """
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", HARVEST_CONFIG_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), [])
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                self.assertEqual(status["harvest"]["quant"], "iq2_xs", status["harvest"])
                self.assertTrue((quant_store.store_dir("iq2_xs") / "strata").is_file())
                self.assertTrue((quant_store.CONFIG_DIR / "run-iq2_xs.json").is_file())
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_setup_run_that_names_no_quant_stores_nothing_and_says_so(self) -> None:
        """Nothing keys the store here, so the run is reported rather than guessed at."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", HARVEST_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), [])
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                self.assertIsNone(status["harvest"]["quant"], status["harvest"])
                self.assertFalse(quant_store.ENGINE_DIR.exists(),
                                 "a run with no quant named must not create a store folder")
                self.assertIn("no quant", "\n".join(job.tail(0)["lines"]))
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_kept_checkout_is_left_with_its_own_files(self) -> None:
        """A checkout the user keeps is theirs: the hook does not move what setup.py wrote."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(tmp, "Strata", HARVEST_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), ["--model", "IQ2_XS"])
                status = wait_for(job)
                self.assertEqual(status["state"], "done", status)
                self.assertFalse(status["scratch"])
                self.assertEqual(status["harvest"], {}, "a run outside tmp/ is not harvested")
                self.assertTrue((checkout / "build" / "strata").is_file())
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_failed_setup_run_stores_nothing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", HARVEST_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), ["--model", "IQ2_XS", "--fail"])
                status = wait_for(job)
                self.assertEqual(status["state"], "failed", status)
                self.assertEqual(status["harvest"], {})
                self.assertFalse(quant_store.ENGINE_DIR.exists())
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_setup_choices_are_written_to_the_config_the_harvest_writes(self) -> None:
        """The boxes on the card have to reach the run config, not just the command line."""
        tmp = Path(tempfile.mkdtemp())
        try:
            saved = self._project(tmp)
            try:
                checkout = make_checkout(strata_hook.SCRATCH, "strata-v0.1.45", HARVEST_STUB)
                job = StrataHook()
                job.log_path = strata_hook.LOG_PATH
                job.start(str(checkout), ["--model", "IQ2_XS", "--context", "262144"])
                status = wait_for(job)
                cfg = json.loads((quant_store.CONFIG_DIR / "run-iq2_xs.json")
                                 .read_text(encoding="utf-8"))
                self.assertIn("--max-context 262144", status["harvest"]["changes"],
                              status["harvest"])
                # args is the flat flag/value list the engine is run with, not a mapping.
                args = cfg["args"]
                self.assertIsInstance(args, list, cfg)
                self.assertIn("--max-context", args, cfg)
                self.assertEqual(args[args.index("--max-context") + 1], "262144", cfg)
            finally:
                self._restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

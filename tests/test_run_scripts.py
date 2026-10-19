"""tests/test_run_scripts.py - the Linux and Windows entry points stay mirrors.

PLAN_CHECKLIST 5: the project ships paired entry points (setup.sh/run.sh and
setup.bat/run.bat, tools/build_engine.sh and tools/build_engine.bat). A feature that
exists on one side and not the other is a bug the user hits on the other platform, so
the parity that matters is written down here: the same flags, the same template guard,
the same build targets and the same fallback.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class EntryPointTests(unittest.TestCase):
    def test_both_platforms_have_an_entry_point(self) -> None:
        for name in ("setup.sh", "run.sh", "setup.bat", "run.bat"):
            self.assertTrue((ROOT / name).is_file(), f"{name} is missing")

    def test_both_run_scripts_offer_the_engineless_start(self) -> None:
        for name in ("run.sh", "run.bat"):
            self.assertIn("--ui-only", read(name), f"{name} does not document the no-engine start")

    def test_both_run_scripts_forward_flags_to_the_server(self) -> None:
        sh, bat = read("run.sh"), read("run.bat")
        self.assertIn('EXTRA="$EXTRA $arg"', sh)
        self.assertIn('if "!flag:~0,1!"=="-" set ARGS=!ARGS! %%a', bat)

    def test_a_stray_name_falls_back_to_the_newest_config_on_both_sides(self) -> None:
        """The fallback is the same rule on both platforms."""
        sh, bat = read("run.sh"), read("run.bat")
        for name in (sh, bat):
            self.assertIn("using the newest", name,
                          "a name that matches nothing is treated as nothing built")
            self.assertIn("run-*.json", name, "the fallback does not look at the run configs")

    def test_neither_run_script_picks_the_template_by_accident(self) -> None:
        sh, bat = read("run.sh"), read("run.bat")
        self.assertIn("run-example", sh, "run.sh must skip the template when picking a config")
        self.assertIn("run-example", bat, "run.bat must skip the template when picking a config")
        self.assertIn("ABSOLUTE/PATH/TO", sh, "the template is detected by its placeholder paths")
        self.assertIn("ABSOLUTE/PATH/TO", bat)

    def test_the_engine_mode_follows_what_is_in_engine_on_both(self) -> None:
        sh, bat = read("run.sh"), read("run.bat")
        self.assertIn("engine/hypercore-worker", sh)
        self.assertIn("engine\\hypercore-worker.exe", bat)

    def test_a_fresh_clone_starts_the_app_on_both_platforms(self) -> None:
        """Nothing installed yet is a state to serve, not a reason to refuse.

        The Build tab is what produces the first engine and the first run config, so
        the entry point brings the page up with no engine instead of exiting; the same
        branch has to exist on both sides.
        """
        sh, bat = read("run.sh"), read("run.bat")
        for name in (sh, bat):
            self.assertIn("so the Build tab is reachable", name,
                          "the engineless start is not in the entry point")
            self.assertIn("ENGINELESS", name, "the entry point has no engineless branch")
            self.assertIn("--ui-only", name)
        self.assertNotIn("exit 1", sh, "run.sh must not refuse to start a fresh clone")


class BuildScriptTests(unittest.TestCase):
    def test_both_build_scripts_inject_the_interface_before_cmake(self) -> None:
        sh, bat = read("tools/build_engine.sh"), read("tools/build_engine.bat")
        self.assertIn("engine-src", sh)
        self.assertIn("engine-src", bat)
        self.assertIn("hypercore-interface", sh)
        self.assertIn("hypercore-interface", bat)

    def test_both_build_all_three_targets_in_one_invocation(self) -> None:
        for name in ("tools/build_engine.sh", "tools/build_engine.bat"):
            text = read(name)
            self.assertIn("strata strata-vision hypercore-worker", text,
                          f"{name} does not build the three targets together")

    def test_both_fall_back_when_the_worker_target_fails(self) -> None:
        for name in ("tools/build_engine.sh", "tools/build_engine.bat"):
            text = read(name)
            self.assertIn("the hypercore-worker target failed", text,
                         f"{name} has no worker fallback")
            self.assertIn("--target strata strata-vision", text,
                         f"{name} does not retry with the engine targets only")
            self.assertIn("BUILD.json", text, f"{name} does not record the fallback")

    def test_both_leave_nothing_in_tmp(self) -> None:
        sh, bat = read("tools/build_engine.sh"), read("tools/build_engine.bat")
        self.assertIn('rm -rf "$CLONE"', sh)
        self.assertIn("rmdir /s /q tmp\\strata-%TAG%", bat)


class FreshCloneStartTests(unittest.TestCase):
    """run.sh against a clone with nothing in it, run for real.

    A stub interpreter stands in for the server: it records the arguments run.sh built
    and answers the port check with 'not running', so the test sees the entry point's
    decisions rather than a live server.
    """

    def scratch(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for folder in ("config", "engine", "tools"):
            (d / folder).mkdir()
        shutil.copyfile(ROOT / "run.sh", d / "run.sh")
        stub = d / ".venv" / "bin" / "python"      # run.sh prefers this interpreter
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text('#!/bin/sh\n'
                        'if [ "$1" = "tools/is_running.py" ]; then exit 1; fi\n'
                        'echo "server: $*"\n', encoding="utf-8")
        stub.chmod(0o755)
        return d

    def server_call(self, d: Path, *args: str) -> tuple[int, str, str]:
        proc = subprocess.run(["sh", str(d / "run.sh"), *args], capture_output=True,
                              text=True, cwd=d, timeout=20, check=False)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("server:")), "")
        # stderr too: a decision the entry point took with a warning is still a decision.
        return proc.returncode, line, proc.stdout + proc.stderr

    def test_nothing_installed_still_starts_the_app(self) -> None:
        d = self.scratch()
        code, call, out = self.server_call(d)
        self.assertEqual(code, 0, f"run.sh refused to start: {out}")
        self.assertIn("--ui-only", call, "the fresh clone was not started engineless")
        self.assertIn("Build tab", out)
        self.assertNotIn("--engine", call, "an engine was named that is not on disk")

    def test_the_engineless_flag_is_passed_once_when_asked_for(self) -> None:
        d = self.scratch()
        code, call, out = self.server_call(d, "--ui-only")
        self.assertEqual(code, 0, out)
        self.assertEqual(call.count("--ui-only"), 1, f"duplicated flag: {call}")

    def test_an_engine_that_is_there_is_started_and_not_skipped(self) -> None:
        d = self.scratch()
        (d / "engine" / "strata").write_text("stub", encoding="utf-8")
        code, call, out = self.server_call(d)
        self.assertEqual(code, 0, out)
        self.assertIn("--engine strata", call)
        self.assertNotIn("--ui-only", call, "a present engine should not be skipped")

    def test_a_real_config_is_still_used(self) -> None:
        d = self.scratch()
        (d / "config" / "run-q2_0.json").write_text('{"model_name": "stub"}', encoding="utf-8")
        code, call, out = self.server_call(d)
        self.assertEqual(code, 0, out)
        self.assertIn("--config config/run-q2_0.json", call)
        self.assertNotIn("--ui-only", call)

    def test_a_name_that_matches_nothing_falls_back_to_the_newest_config(self) -> None:
        """`run.sh 8081` is a stray argument, not a machine with nothing built on it.

        Refusing the name and starting engineless leaves a model that is sitting in
        engine/ unstarted, which is the state the user reads as "the build did not work".
        """
        d = self.scratch()
        older = d / "config" / "run-iq2_xs.json"
        older.write_text(json.dumps({"model_name": "IQ2_S"}), encoding="utf-8")
        newest = d / "config" / "run-q2_0.json"
        newest.write_text(json.dumps({"model_name": "Q2_0"}), encoding="utf-8")
        os.utime(older, (1000, 1000))          # make the order of "newest" unambiguous
        os.utime(newest, (2000, 2000))
        (d / "config" / "run-example.json").write_text(
            json.dumps({"exe": "ABSOLUTE/PATH/TO/strata"}), encoding="utf-8")
        code, call, out = self.server_call(d, "8081")
        self.assertEqual(code, 0, f"run.sh gave up on a name it could not read: {out}")
        self.assertIn("--config config/run-q2_0.json", call,
                      "the fallback did not take the newest run config")
        self.assertNotIn("run-example.json", call, "the fallback took the template")
        self.assertIn("no config named '8081'", out + "", "the fallback said nothing about the name")
        self.assertNotIn("--ui-only", call, "a machine with a run config was started engineless")


class ScratchIsolationTests(unittest.TestCase):
    """Nothing that serves a request reads tmp/."""

    def test_no_runtime_module_walks_the_scratch_folder(self) -> None:
        runtime = ["serve/server.py", "serve/direct_engine.py", "serve/engine_supervisor.py",
                  "serve/attached_engine.py", "serve/offline_engine.py", "serve/frontend.py",
                  "serve/monitor_demo.py", "serve/telemetry.py"]
        offenders = []
        for name in runtime:
            text = (ROOT / name).read_text(encoding="utf-8")
            if 'ROOT / "tmp"' in text or "tmp/strata" in text:
                offenders.append(name)
        self.assertEqual(offenders, [], f"these runtime modules read tmp/: {offenders}")

    def test_the_build_api_is_the_only_place_that_uses_it(self) -> None:
        text = (ROOT / "serve/build_api.py").read_text(encoding="utf-8")
        self.assertIn('TMP_DIR = ROOT / "tmp"', text)
        self.assertIn("clean_tmp", text, "the scratch clone must be removed when a build finishes")


if __name__ == "__main__":
    unittest.main()

"""tests/test_run_scripts.py - the Linux and Windows entry points stay mirrors.

PLAN_CHECKLIST 5: the project ships paired entry points (setup.sh/run.sh and
setup.bat/run.bat, tools/build_engine.sh and tools/build_engine.bat). A feature that
exists on one side and not the other is a bug the user hits on the other platform, so
the parity that matters is written down here: the same flags, the same template guard,
the same build targets and the same fallback.
"""
from __future__ import annotations

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

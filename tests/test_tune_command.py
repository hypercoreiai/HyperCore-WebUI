#!/usr/bin/env python3
"""tests/test_tune_command.py - the Tune tab's form must map to flags the CLI has.

The sweep is a subprocess of optuna_tune/bayesian_tune.py, so the tab is only as
good as the argument list serve/tune_api.py builds. A flag the tuner does not have
is not a warning: argparse exits with its usage text and the tab shows an empty run
with no error the user can read. The checks below are against the real CLI - the
tuner is run with --dry-run, which prints the engine command lines and boots
nothing - so the tab and the CLI cannot drift apart silently.

Two spellings are easy to guess wrong and are checked by name:
  --cntx, not --max-context;
  --no-benchmark, because there is no --benchmark.
"""
import subprocess
import unittest

from serve import tune_api


def sample_request() -> dict:
    """The form as the tab submits it: every control set, pins included."""
    return {
        "model": "q2_0",
        "objective": "gen",
        "context": 131072,
        "trials": 3,
        "seed": 1,
        "repeat": 2,
        "sampler": "tpe",
        "mode": "joint",
        "vram_min": 256,
        "vram_max": 1536,
        "vram_step": 128,
        "pcie": 1.0,
        "spec_min_p": 0.7,
        "max_workers": 8,
        "pin": {"ple-io": "direct", "kv": "int8", "kvgrow": "1", "hostcore": "first",
                "affinity": "auto", "worker": "4", "tail": "1",
                "prefill-cap": "auto:16384"},
        "benchmark": True,
        "fresh": True,
        "dry_run": True,
    }


class TuneCommand(unittest.TestCase):
    def run_dry(self, request: dict) -> subprocess.CompletedProcess:
        cmd = tune_api.build_command(request)
        if "--dry-run" not in cmd:
            cmd.append("--dry-run")
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)

    def test_the_whole_command_is_accepted_by_the_tuner(self):
        out = self.run_dry(sample_request())
        self.assertNotIn("unrecognized arguments", out.stdout + out.stderr)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_context_is_spelled_the_way_the_cli_reads_it(self):
        cmd = tune_api.build_command({"model": "q2_0", "context": 131072})
        self.assertIn("--cntx", cmd)
        self.assertNotIn("--max-context", cmd)

    def test_benchmark_off_is_no_benchmark(self):
        # the sweep benchmarks the winner unless the tab's box is unchecked;
        # the CLI has no --benchmark, only --no-benchmark
        self.assertIn("--no-benchmark", tune_api.build_command({"model": "q2_0"}))
        self.assertNotIn("--no-benchmark",
                        tune_api.build_command({"model": "q2_0", "benchmark": True}))

    def test_objective_is_one_of_four(self):
        for objective in tune_api.OBJECTIVES:
            self.assertIn(f"--{objective}", tune_api.build_command({"model": "q2_0", "objective": objective}))
        cmd = tune_api.build_command({"model": "q2_0", "objective": "nonsense"})
        self.assertFalse(any(t in ("--gen", "--prefill", "--latency", "--balance") for t in cmd))

    def test_paths_come_from_the_run_config_and_the_tab_can_override_them(self):
        cmd = tune_api.build_command({"model": "q2_0"})
        paths = tune_api.engine_paths("q2_0")
        self.assertEqual(cmd[cmd.index("--engine") + 1], paths["engine"])
        for key, flag in (("pack", "--pack"), ("native", "--native")):
            if paths.get(key):
                self.assertEqual(cmd[cmd.index(flag) + 1], paths[key])
        cmd = tune_api.build_command({"model": "q2_0", "engine": "/tmp/strata"})
        self.assertEqual(cmd[cmd.index("--engine") + 1], "/tmp/strata")

    def test_no_flag_is_invented(self):
        """The tuner has no --expert-profile and no --load; only what --help lists."""
        cmd = tune_api.build_command({"model": "q2_0"})
        self.assertNotIn("--expert-profile", cmd)
        self.assertNotIn("--load", cmd)
        self.assertIn("--no-load", tune_api.build_command({"model": "q2_0", "fresh": True}))
        # 'example' is the shipped template (config/run-example.json is the only run
        # config in the repository); a real model's config is machine-local and may
        # not exist on the box running the test.
        self.assertIn("expert_profile", tune_api.engine_paths("example"))

    def test_the_pin_boxes_are_filled_from_the_search_space(self):
        options = tune_api.pin_options()
        self.assertEqual(sorted(options), sorted(tune_api.PINABLE))
        self.assertIn("int8", options["kv"]["choices"])
        self.assertIn("auto:16384", options["prefill-cap"]["choices"])
        self.assertEqual(options["worker"]["kind"], "int")

    def test_deliverables_land_in_config_not_the_repository_root(self):
        cmd = tune_api.build_command({"model": "q2_0"})
        self.assertEqual(cmd[-2:], ["--deliverable-dir", str(tune_api.ROOT / "config")])

    def test_unknown_keys_are_ignored(self):
        cmd = tune_api.build_command({"model": "q2_0", "whatever": 1, "trials": "5"})
        self.assertNotIn("whatever", cmd)
        self.assertEqual(cmd[cmd.index("--trials") + 1], "5")


if __name__ == "__main__":
    unittest.main()

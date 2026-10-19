#!/usr/bin/env python3
"""tests/test_direct_engine.py - the --serve handshake, against the stub engine.

The real engine's READY line is `READY <max_context> stop`: the third field is the
word "stop", not a token id. That one line is the whole reason this file exists -
reading it as an int is what made `./run.sh` say

    [strata] invalid literal for int() with base 10: 'stop'

and the token stream is the other half: the engine streams `T <id>`, and PP/RESUME/
DONE lines carry numbers too, so a parser that takes "any digit on any line" yields
the prompt progress as if it were generated tokens.
"""
import threading
import unittest

from serve.direct_engine import (
    DirectEngine,
    DirectEngineError,
    memory_short,
    missing_paths,
)

STUB = "tests/stub_engine.py"


class DirectEngineProtocol(unittest.TestCase):
    def setUp(self):
        self.engine = DirectEngine(STUB, [], startup_timeout_s=20.0)
        self.engine.start()

    def tearDown(self):
        self.engine.stop()

    def test_ready_reports_context_and_that_stop_is_honoured(self):
        self.assertEqual(self.engine.max_context, 32768)
        self.assertTrue(self.engine.can_stop)
        self.assertTrue(self.engine.alive())

    def test_info_lines_are_kept_for_the_about_and_monitor_tabs(self):
        self.assertEqual(self.engine.info.get("engine"), "0.0.0-stub")
        self.assertEqual(self.engine.info.get("expert_slots"), 0)

    def test_only_T_lines_are_tokens(self):
        # PP lines are heartbeats (None), not tokens: a parser that takes "any digit on
        # any line" would read the prompt progress as generated text.
        tokens = [t for t in self.engine.generate([1, 2, 3], 8, {}, threading.Event())]
        self.assertEqual([t for t in tokens if t is not None], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertTrue(any(t is None for t in tokens))

    def test_done_line_becomes_the_request_summary(self):
        list(self.engine.generate([1, 2, 3], 4, {}, threading.Event()))
        last = self.engine.last
        self.assertEqual(last["generated"], 4)
        self.assertEqual(last["prompt_tokens"], 3)
        self.assertEqual(last["finish"], "length")
        self.assertEqual(last["prompt_read"], 3)

    def test_a_stop_mid_request_ends_the_request_and_is_drained_to_done(self):
        cancel = threading.Event()
        gen = self.engine.generate([1, 2, 3], 8, {}, cancel)
        tokens = [next(gen)]
        cancel.set()
        tokens += list(gen)
        self.assertLess(len(tokens), 8)
        self.assertEqual(self.engine.last["finish"], "cancel")

    def test_quit_ends_the_engine_and_alive_says_so(self):
        self.engine.stop()
        self.assertFalse(self.engine.alive())
        self.assertTrue(self.engine.restart())

    def test_a_missing_engine_is_reported_not_crashed(self):
        with self.assertRaises(DirectEngineError):
            DirectEngine("tests/no_such_engine", []).start()


class RunConfigPreflight(unittest.TestCase):
    """A path the config got wrong is said as a path, not as a memory problem.

    ./run.sh with the template config produced:

        [strata] the engine exited with code 2: ... memory guard: limit 30.5 GiB ...
        the machine has no room for a second engine

    when the only fault was /ABSOLUTE/PATH/TO/... in the config. The guard line is
    printed whether or not it refused, so it cannot be matched as a keyword.
    """

    TEMPLATE = ["--pack", "/ABSOLUTE/PATH/TO/packs/q2_0",
                "--native", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00001-of-00002.gguf",
                "--ple-gguf", "/ABSOLUTE/PATH/TO/models/Q2_0/model-00002-of-00002.gguf"]

    def test_placeholder_paths_are_found_before_the_engine_is_spawned(self):
        missing = missing_paths(self.TEMPLATE)
        self.assertEqual([flag for flag, _ in missing], ["--pack", "--native", "--ple-gguf"])
        self.assertTrue(all("ABSOLUTE" in path for _, path in missing))

    def test_paths_that_exist_are_not_reported(self):
        here = ["--pack", "config", "--native", "tests/run-stub.json"]
        self.assertEqual(missing_paths(here), [])

    def test_a_control_vector_weight_is_not_part_of_the_path(self):
        missing = missing_paths(["--control-vector-scaled", "/no/such/projection.gguf:1.0"])
        self.assertEqual(missing, [("--control-vector-scaled", "/no/such/projection.gguf")])

    def test_starting_with_a_bad_path_names_the_path_and_does_not_spawn_the_engine(self):
        engine = DirectEngine("tests/stub_engine.py", self.TEMPLATE)
        with self.assertRaises(DirectEngineError) as got:
            engine.start()
        self.assertIn("--native /ABSOLUTE/PATH/TO", str(got.exception))
        self.assertIsNone(engine.proc)

    def test_a_satisfied_memory_guard_is_not_read_as_out_of_room(self):
        ok = ["strata generate: memory guard: limit 30.5 GiB (source: meminfo), "
              "current 2.3 GiB, reclaimable cache credited 0.0 GiB; available 28.3 GiB "
              "(MemAvailable 28.3 GiB), floor 2560 MiB"]
        self.assertFalse(memory_short(ok))

    def test_a_refused_memory_guard_is(self):
        full = ["strata generate: memory guard: limit 30.5 GiB (source: meminfo), "
                "current 27.1 GiB; available 3.5 GiB (MemAvailable 3.5 GiB), floor 2560 MiB",
                "strata generate: native embedding: cannot pin 260 MiB: out of memory"]
        self.assertTrue(memory_short(full))

    def test_a_missing_shard_is_blamed_on_the_config_not_on_ram(self):
        engine = DirectEngine("tests/stub_engine.py", [])
        hint = engine._hint(["strata generate: memory guard: limit 30.5 GiB, "
                             "available 28.3 GiB (MemAvailable 28.3 GiB), floor 2560 MiB",
                             "strata generate: --native /ABSOLUTE/PATH/TO/model.gguf: "
                             "missing model shard (shard 1 of 2; is the download complete?)"])
        self.assertIn("not on this machine", hint)
        self.assertNotIn("no room for a second engine", hint)


if __name__ == "__main__":
    unittest.main()

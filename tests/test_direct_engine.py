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

from serve.direct_engine import DirectEngine, DirectEngineError

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


if __name__ == "__main__":
    unittest.main()

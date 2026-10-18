#!/usr/bin/env python3
"""tests/test_turn_tail_check.py - who wins when the turn-tail check is set twice.

The end-of-turn "is the work done?" check is opt-in per model (`turn_tail_check: true`
in a run config, or `--turn-tail-check`). An install that runs several models needs one
switch that covers all of them: `HYPERCORE_NO_TURN_TAIL_CHECK`. `resolve_turn_tail` is
the whole decision, so these tests need no engine and no server.
"""
from __future__ import annotations

import unittest

from serve.server import resolve_turn_tail

OFF = {"HYPERCORE_NO_TURN_TAIL_CHECK": "1"}
NOT_OFF = {"HYPERCORE_NO_TURN_TAIL_CHECK": "0"}


class ResolutionTests(unittest.TestCase):
    def test_off_unless_something_asks_for_it(self) -> None:
        self.assertFalse(resolve_turn_tail(False, {}, env={}))
        self.assertFalse(resolve_turn_tail(False, {"turn_tail_check": False}, env={}))
        self.assertFalse(resolve_turn_tail(False, {"turn_tail_check": "true"}, env={}),
                         "a string is not the boolean the config documents")

    def test_the_flag_or_the_config_turns_it_on(self) -> None:
        self.assertTrue(resolve_turn_tail(True, {}, env={}))
        self.assertTrue(resolve_turn_tail(False, {"turn_tail_check": True}, env={}))
        self.assertTrue(resolve_turn_tail(True, {"turn_tail_check": True}, env={}))

    def test_the_install_switch_beats_both(self) -> None:
        for env in (OFF, {"HYPERCORE_NO_TURN_TAIL_CHECK": "true"},
                    {"HYPERCORE_NO_TURN_TAIL_CHECK": "  YES "}):
            self.assertFalse(resolve_turn_tail(True, {"turn_tail_check": True}, env=env),
                             f"{env} should switch a whole install off")

    def test_an_unset_or_empty_switch_changes_nothing(self) -> None:
        for env in (NOT_OFF, {"HYPERCORE_NO_TURN_TAIL_CHECK": ""}, {"OTHER": "1"}):
            self.assertTrue(resolve_turn_tail(True, {}, env=env),
                            f"{env} is not an off switch")

    def test_the_environment_is_the_default_source(self) -> None:
        import os
        before = os.environ.get("HYPERCORE_NO_TURN_TAIL_CHECK")
        os.environ["HYPERCORE_NO_TURN_TAIL_CHECK"] = "1"
        try:
            self.assertFalse(resolve_turn_tail(True, {"turn_tail_check": True}))
        finally:
            if before is None:
                del os.environ["HYPERCORE_NO_TURN_TAIL_CHECK"]
            else:
                os.environ["HYPERCORE_NO_TURN_TAIL_CHECK"] = before


if __name__ == "__main__":
    unittest.main()

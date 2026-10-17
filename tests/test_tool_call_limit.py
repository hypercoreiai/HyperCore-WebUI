#!/usr/bin/env python3
"""tests/test_tool_call_limit.py - the per-request tool call cap (mcp.max_tool_calls).

`mcp.max_rounds` caps the assistant turns that may emit calls. A turn can emit several
calls, so the round cap is not a call cap: 500 rounds is not 500 calls. These tests run
`run_with_mcp` against a stub engine and a stub hub - no GPU, no MCP server, no weights -
and assert what the chat depends on: the calls that fit in the budget run, the rest are
closed as "not run", exactly one limit event names the cap that was hit, and the turn
stops instead of looping on calls that can never run.
"""
from __future__ import annotations

import threading
import unittest
from typing import Any

from serve.mcp import DEFAULTS, settings_from
from serve.server import run_with_mcp

TOOL = "web__search"


class Call:
    """One MCP call the stub model is writing."""

    def __init__(self, i: int, name: str) -> None:
        self.id, self.name, self.arguments = f"call-{i}", name, {}


class Event:
    """The shape serve.frontend.Event has where run_with_mcp reads it: kind, call, text."""

    def __init__(self, kind: str, call: Any = None, text: str = "") -> None:
        self.kind, self.call, self.text = kind, call, text


class StubEngine:
    max_context = 8000


class StubService:
    """A model that emits `calls_per_round` MCP calls for `rounds` rounds, then none."""

    def __init__(self, calls_per_round: int = 2, rounds: int = 99) -> None:
        self.engine = StubEngine()
        self.calls_per_round, self.rounds_left = calls_per_round, rounds
        self.rounds = 0

    def prepare(self, messages, tools, kw, max_req):
        return list(range(len(messages))), False, 256

    def run(self, ids, thinking, tools, max_new, sampling, cancel):
        self.rounds += 1
        if self.rounds_left > 0:
            self.rounds_left -= 1
            for n in range(self.calls_per_round):
                yield "event", Event("tool_call", call=Call(self.rounds * 10 + n, TOOL))
        yield "done", {"completion_tokens": 4, "finish": "stop"}


class StubServer:
    name = "web"


class StubHub:
    def __init__(self, **override: Any) -> None:
        self.settings = {**DEFAULTS, **override}
        self.calls = 0

    def routes(self) -> dict[str, tuple[Any, str]]:
        # serve/mcp/routes() maps a tool name to (server, the tool it is called by).
        return {TOOL: (StubServer(), "search")}

    def call(self, name: str, arguments: dict, cancel) -> dict:
        self.calls += 1
        return {"ok": True, "text": "result", "chars": 6, "truncated": False, "ms": 10}

    def needs_compacting(self, tokens: int, context: int) -> bool:
        return False

    def needs_condensing(self, tokens: int, context: int, rounds: int) -> bool:
        return False


def turn(rounds: int = 99, **hub_settings: Any):
    """Run one stub turn and split its stream into the parts the test cares about."""
    svc, hub = StubService(rounds=rounds), StubHub(**hub_settings)
    events = list(run_with_mcp(svc, hub, [{"role": "user", "content": "go"}], [], {},
                              [0], False, 256, 4096, {}, threading.Event(), {TOOL}))
    mcp = [p for k, p in events if k == "mcp"]
    return (hub,
            [p for p in mcp if p["event"] == "limit"],
            [p for p in mcp if p["event"] == "call"],
            [p for p in mcp if p["event"] == "result" and p.get("skipped")])


class Settings(unittest.TestCase):
    def test_the_shipped_default_is_500_calls(self) -> None:
        self.assertEqual(DEFAULTS["max_rounds"], 500, "the round default changed")
        self.assertEqual(DEFAULTS["max_tool_calls"], 500, "the call default changed")

    def test_the_key_is_read_and_validated(self) -> None:
        self.assertEqual(settings_from({"mcp": {"max_tool_calls": 500}})["max_tool_calls"], 500)
        self.assertEqual(settings_from({"mcp": {"max_tool_calls": 0}})["max_tool_calls"], 0)
        for bad in ({"max_tool_calls": -1}, {"max_tool_calls": "many"}, {"max_tool_calls": None}):
            with self.assertRaises(SystemExit):
                settings_from({"mcp": bad})


class CallCap(unittest.TestCase):
    def test_the_calls_that_fit_run_and_the_rest_are_closed(self) -> None:
        hub, limit, ran, skipped = turn(max_rounds=500, max_tool_calls=3)
        self.assertEqual(len(ran), 3, f"{len(ran)} calls ran against a cap of 3")
        self.assertEqual(hub.calls, 3, "the hub ran a different number of calls than the stream reported")
        self.assertEqual(len(skipped), 1, "the call past the cap was not closed as skipped")
        self.assertEqual(len(limit), 1, "no limit event was emitted")
        self.assertEqual((limit[0]["max_tool_calls"], limit[0]["calls"], limit[0]["rounds"]), (3, 3, 2))

    def test_zero_means_no_call_cap_and_the_round_cap_still_stops_the_turn(self) -> None:
        hub, limit, _ran, _ = turn(max_rounds=2, max_tool_calls=0)
        self.assertEqual(hub.calls, 4, "the round cap did not stop the turn")
        self.assertEqual(len(limit), 1, "the round cap emitted no limit event")
        self.assertEqual((limit[0]["max_rounds"], limit[0]["rounds"]), (2, 2))
        self.assertEqual(limit[0]["calls"], 4)

    def test_a_turn_under_the_cap_emits_no_limit_event(self) -> None:
        hub, limit, ran, _ = turn(rounds=2, max_rounds=5, max_tool_calls=0)
        self.assertEqual(len(ran), 4)
        self.assertEqual(hub.calls, 4)
        self.assertEqual(limit, [])


if __name__ == "__main__":
    unittest.main()

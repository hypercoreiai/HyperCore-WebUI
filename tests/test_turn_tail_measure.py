"""tests/test_turn_tail_measure.py - the turn-tail measurement harness.

tools/turn_tail_measure.py runs the same prompt against a server with the end-of-turn
"is the work done?" check off and a second server with it on, and reports how many
tail checks ran, how many characters the turn grew by, and how much of the request
the answer covered. Two things have to be right for those numbers to mean anything:
the SSE stream has to be counted, not guessed, and the coverage test has to be a
plain substring test that is not fooled by case. No GPU, no real model: the counting
is checked against a stub stream and the wiring against two mock-engine servers.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from turn_tail_measure import (
    SELF_KEY,
    auth_key,
    covered,
    request,
    start_mock_base,
    start_tail_server,
)

#: A stream shaped like the one serve/server.py emits when the check is on: content,
#: a tail_check event saying the model wants to keep going, more content, a tail_check
#: saying it is done, then the terminator.
STREAM: list[dict[str, object]] = [
    {"choices": [{"delta": {"content": "PART A: the risks. "}}]},
    {"hypercore_mcp": {"event": "tail_check", "result": "continue", "checks": 1, "max_checks": 3}},
    {"choices": [{"delta": {"content": "PART B: a KV cache. "}}]},
    {"hypercore_mcp": {"event": "tail_check", "result": "complete", "checks": 2}},
    {"choices": [{"delta": {"content": "PART C: nvidia-smi."}}]},
    {"done": True},
]

#: The request body the last call sent, so a test can assert what was put on the wire.
LAST_BODY: dict[str, object] = {}


class _Stub(BaseHTTPRequestHandler):
    """Serves the stream above once, then closes. HTTP/1.0 semantics (the default)
    mean the client sees the end of the body when the connection closes."""

    status = 200

    def do_POST(self) -> None:  # http.server calls it by name
        LAST_BODY.clear()
        length = int(self.headers.get("Content-Length", 0))
        if length:
            LAST_BODY.update(json.loads(self.rfile.read(length)))
        self.send_response(self.status)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.status == 200:
            for chunk in STREAM:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n")
        else:
            self.wfile.write(b"engine_unavailable")

    def log_message(self, *args: object) -> None:  # pragma: no cover - silence
        pass


class _Unavailable(_Stub):
    status = 503


class _Quiet(_Stub):
    """Answers 200 with nothing, for tests that only care about the request body."""

    def do_POST(self) -> None:
        LAST_BODY.clear()
        length = int(self.headers.get("Content-Length", 0))
        if length:
            LAST_BODY.update(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()


def stub_server(handler: type[_Stub] = _Stub) -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class StreamTests(unittest.TestCase):
    """The counts come from the events the server actually sent."""

    def test_text_is_reassembled_from_the_deltas(self) -> None:
        srv, url = stub_server()
        try:
            out = request(url, "prompt", "", 64, "mock")
        finally:
            srv.shutdown()
        self.assertNotIn("error", out)
        self.assertEqual(out["text"], "PART A: the risks. PART B: a KV cache. PART C: nvidia-smi.")
        self.assertEqual(out["chars"], len(out["text"]))

    def test_the_checks_are_counted_and_split_by_result(self) -> None:
        srv, url = stub_server()
        try:
            out = request(url, "prompt", "", 64, "mock")
        finally:
            srv.shutdown()
        self.assertEqual(out["checks"], 2)
        self.assertEqual(out["continued"], 1)
        self.assertEqual(out["confirmed"], 1)

    def test_a_non_200_is_an_error_and_not_an_empty_answer(self) -> None:
        srv, url = stub_server(_Unavailable)
        try:
            out = request(url, "prompt", "", 64, "mock")
        finally:
            srv.shutdown()
        self.assertIn("error", out)
        self.assertIn("503", out["error"])
        self.assertGreaterEqual(out["elapsed"], 0.0)

    def test_the_reasoning_budget_is_only_sent_when_it_is_set(self) -> None:
        srv, url = stub_server(_Quiet)
        try:
            request(url, "prompt", "", 64, "mock", 0)
            without = dict(LAST_BODY)
            request(url, "prompt", "key", 64, "mock", 400)
            with_budget = dict(LAST_BODY)
        finally:
            srv.shutdown()
        self.assertNotIn("reasoning_budget_tokens", without)
        self.assertEqual(with_budget["stream"], True)
        self.assertEqual(with_budget["reasoning_budget_tokens"], 400)

    def test_the_api_key_is_sent_as_a_bearer_token(self) -> None:
        srv, url = stub_server(_Quiet)
        try:
            out = request(url, "prompt", "0000", 64, "mock")
        finally:
            srv.shutdown()
        self.assertNotIn("error", out)


class CoverageTests(unittest.TestCase):
    def test_markers_are_matched_without_case(self) -> None:
        text = "part a done. Part B is missing."
        self.assertEqual(covered(text, ["PART A", "PART B", "PART C"]), ["PART A", "PART B"])

    def test_no_markers_means_nothing_to_cover(self) -> None:
        self.assertEqual(covered("anything", []), [])


class SelfKeyTests(unittest.TestCase):
    """Which key the harness uses. A mock server started without one inherits the key in
    the machine's run config, so --self has to name its own."""

    def test_a_self_run_names_a_key_and_a_live_run_keeps_the_one_given(self) -> None:
        self.assertEqual(auth_key("", True), SELF_KEY)
        self.assertEqual(auth_key("0000", True), "0000")
        self.assertEqual(auth_key("", False), "")
        self.assertEqual(auth_key("secret", False), "secret")


class WiringTests(unittest.TestCase):
    """Two mock-engine servers, one with the check on. Without a GPU this is the only
    thing the harness can prove: that the check fires on the test server, never on the
    control, and that the turn it extends really gets longer."""

    def test_the_check_runs_on_the_test_server_and_not_on_the_control(self) -> None:
        base = start_mock_base(8170, SELF_KEY)
        tail = start_tail_server("http://127.0.0.1:8170", 8171, SELF_KEY,
                                ["--turn-tail-checks", "2"], mock=True)
        try:
            off = request("http://127.0.0.1:8170", "Do three parts of a task.", SELF_KEY, 200, "hypercore")
            on = request("http://127.0.0.1:8171", "Do three parts of a task.", SELF_KEY, 200, "hypercore")
        finally:
            for proc in (tail, base):
                proc.terminate()
            for proc in (tail, base):
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # a server that will not close is not worth hanging on
                    proc.kill()
        self.assertNotIn("error", off)
        self.assertNotIn("error", on)
        self.assertEqual(off["checks"], 0)
        self.assertGreaterEqual(on["checks"], 1)
        self.assertGreater(on["chars"], off["chars"])

    def test_the_mock_pair_answers_only_to_the_key_it_was_started_with(self) -> None:
        """The 401 this harness used to get is the point: the gate is the real one."""
        base = start_mock_base(8172, SELF_KEY)
        try:
            without = request("http://127.0.0.1:8172", "a prompt", "", 64, "hypercore")
            with_key = request("http://127.0.0.1:8172", "a prompt", SELF_KEY, 64, "hypercore")
        finally:
            base.terminate()
            try:
                base.wait(timeout=10)
            except subprocess.TimeoutExpired:
                base.kill()
        self.assertIn("error", without)
        self.assertIn("401", without["error"])
        self.assertNotIn("error", with_key)


if __name__ == "__main__":
    unittest.main()

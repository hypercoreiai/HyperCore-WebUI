#!/usr/bin/env python3
"""tests/test_attached_engine.py - attaching to an engine that is already running.

One engine per machine: the second WebUI does not start a second copy of the model,
it talks to the first over the OpenAI API that engine's server already speaks. This
stands in for that server with a tiny one that answers /health and streams SSE the
same way Strata does - one text piece per token, keep-alive comments between them,
and `data: [DONE]` at the end - so the adapter's parsing is exercised without a GPU.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from serve.attached_engine import AttachedEngine, AttachedEngineError

TOKENS = ["We", " need", " answer", " user", " \"", "hi", "\"."]


class FakeEngineHandler(BaseHTTPRequestHandler):
    """The stand-in server: /health, /v1/status, /unload, /load and an SSE chat."""

    state = None                      # the FakeEngineServer this handler answers for

    def log_message(self, *args):     # keep the test output clean
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self.state
        if self.path in ("/health", "/api/health"):
            self._json({"status": "ok", "max_context": 32768, "model": "fake-model",
                        "images": False, "api_key": False, "loaded": s.loaded, "service": "strata"})
        elif self.path == "/v1/status":
            self._json({"service": "hypercore", "engine": "0.1.45-fake",
                        "model": "fake-model", "loaded": s.loaded})
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        s = self.state
        if self.path == "/unload":
            s.unloads += 1
            s.loaded = False
            self._json({"status": "unloaded"})
        elif self.path == "/load":
            s.loads += 1
            s.loaded = True
            self._json({"status": "loaded"})
        elif self.path == "/v1/chat/completions":
            s.last_body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in s.tokens:
                self.wfile.write(b"data: " + json.dumps(
                    {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                ).encode() + b"\n\n")
                self.wfile.write(b": keep-alive\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._json({"error": {"message": "not found"}}, 404)


class FakeEngineServer:
    """A stand-in for a Strata server that is already up."""

    def __init__(self, tokens=TOKENS, loaded=True):
        self.tokens = list(tokens)
        self.loaded = loaded
        self.unloads = 0
        self.loads = 0
        self.last_body = None
        FakeEngineHandler.state = self
        self.httpd = HTTPServer(("127.0.0.1", 0), FakeEngineHandler)
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class ByteTokenizer:
    """One id per UTF-8 byte, as serve/server.py's fallback tokenizer does."""

    def encode(self, text, parse_special=False, plain=()):
        return list(text.encode("utf-8"))

    def decode(self, ids, errors="replace"):
        return bytes(ids).decode("utf-8", errors)


class AttachedEngineTests(unittest.TestCase):
    def setUp(self):
        self.remote = FakeEngineServer()
        self.engine = AttachedEngine(self.remote.url, tokenizer=ByteTokenizer())

    def tearDown(self):
        self.remote.close()

    def test_start_reads_the_health_of_the_engine_it_attaches_to(self):
        self.assertTrue(self.engine.start())
        self.assertEqual(self.engine.max_context, 32768)
        self.assertEqual(self.engine.model, "fake-model")
        self.assertEqual(self.engine.version, "0.1.45-fake")
        self.assertTrue(self.engine.info["attached"])
        self.assertEqual(self.engine.info["engine_url"], self.remote.url)

    def test_nothing_is_started_when_there_is_nothing_to_attach_to(self):
        engine = AttachedEngine("http://127.0.0.1:1")
        with self.assertRaises(AttachedEngineError):
            engine.start()

    def test_the_prompt_is_sent_as_text_and_the_answer_returns_as_token_ids(self):
        self.engine.start()
        ids = self.engine.generate([104, 105], 7, {}, threading.Event())
        tokens = list(ids)
        self.assertEqual(bytes(tokens).decode(), "".join(TOKENS))
        self.assertEqual(self.remote.last_body["messages"][0]["content"], "hi")
        self.assertEqual(self.engine.last["generated"], len(tokens))

    def test_sampling_is_forwarded_in_the_names_the_api_understands(self):
        self.engine.start()
        list(self.engine.generate([1], 3, {"temperature": 0.7, "top_k": 8, "seed": 42}, threading.Event()))
        body = self.remote.last_body
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["top_k"], 8)
        self.assertEqual(body["seed"], 42)
        self.assertTrue(body["stream"])

    def test_a_cancel_stops_reading_without_killing_the_remote_request(self):
        self.engine.start()
        cancel = threading.Event()
        gen = self.engine.generate([1], 7, {}, cancel)
        next(gen)
        cancel.set()
        self.assertLess(len(list(gen)), len(TOKENS))
        self.assertTrue(self.engine.alive())          # the engine belongs to the other server

    def test_unload_and_restart_are_asks_not_commands(self):
        self.engine.start()
        self.engine.unload()
        self.assertEqual(self.remote.unloads, 1)
        self.assertFalse(self.engine.alive())
        self.assertTrue(self.engine.restart())
        self.assertEqual(self.remote.loads, 1)
        self.assertTrue(self.engine.alive())

    def test_this_server_cannot_resize_an_engine_it_does_not_own(self):
        self.engine.start()
        with self.assertRaises(ValueError):
            self.engine.vram(1024)

    def test_stop_leaves_the_other_server_running(self):
        self.engine.start()
        self.engine.stop()
        self.assertFalse(self.engine.alive())
        self.assertEqual(self.remote.unloads, 0)     # we never touched its model


if __name__ == "__main__":
    unittest.main()

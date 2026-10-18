"""tests/test_direct_fallback.py - the engine still runs when the worker was not built.

serve/server.py prefers the HyperCore worker (serve/engine_supervisor.py, a framed
UNIX-socket boundary). hypercore-worker is ours, not upstream Strata's, so a machine
that only built the engine has no worker binary. In that case the server drives the
same engine over its stdin/stdout protocol (serve/direct_engine.py) instead of
refusing to start. The stub below speaks that protocol, so this needs no GPU.
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

#: A stand-in engine speaking the protocol serve/direct_engine.py expects.
STUB_ENGINE = """#!/bin/sh
echo "READY 4096 stop"
while IFS= read -r line; do
  case "$line" in
    GEN*) echo "T 1"; echo "T 2"; echo "DONE 2 0 0 0 stop";;
    STOP) ;;
    QUIT) exit 0;;
  esac
done
"""


class DirectFallbackTests(unittest.TestCase):
    STARTUP_S = 30

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        engine = self.tmp / "fake-engine.sh"
        engine.write_text(STUB_ENGINE, encoding="utf-8")
        engine.chmod(0o755)
        self.config = self.tmp / "run-fake.json"
        # The hypercore path needs a tokenizer on disk before it will start. A byte-level
        # vocabulary (every byte of the model's byte alphabet as one token) is enough for
        # the chat template to encode: the point of the test is which engine the server
        # drives, not what it says.
        sys.path.insert(0, str(ROOT / "tools"))
        from hypercore_tokenizer import BYTE_TO_UNICODE
        tok = self.tmp / "tokenizer"
        tok.mkdir()
        (tok / "vocab.json").write_text(json.dumps({BYTE_TO_UNICODE[b]: b for b in range(256)}),
                                        encoding="utf-8")
        (tok / "merges.txt").write_text("", encoding="utf-8")
        (tok / "token_type.json").write_text("[]", encoding="utf-8")
        self.config.write_text(json.dumps({
            "exe": str(engine),
            "args": ["--native", str(engine)],
            "model_name": "fake-model",
            "tokenizer": str(tok),
        }), encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        # A worker path that does not exist: this is the "worker: not built" case.
        self.log_path = self.tmp / "server.log"
        self.log_fh = open(self.log_path, "w", encoding="utf-8")  # noqa: SIM115 - held open for the subprocess, closed in tearDown
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--engine", "hypercore", "--no-attach",
             "--config", str(self.config), "--worker", str(self.tmp / "hypercore-worker"),
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(ROOT), env=os.environ, stdout=self.log_fh, stderr=subprocess.STDOUT)

    def tearDown(self) -> None:
        if getattr(self, "proc", None):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "log_fh", None):
            self.log_fh.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def output(self) -> str:
        """What the server printed so far. The log is a file, so a still-running
        server can be read without waiting for it to exit."""
        try:
            return self.log_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def test_the_server_starts_in_direct_mode_instead_of_exiting(self) -> None:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3) as r:
                    body = json.loads(r.read())
                    break
            except (urllib.error.URLError, OSError, ValueError):
                if self.proc.poll() is not None:
                    self.fail(f"the server exited instead of falling back: {self.output()}")
                time.sleep(0.5)
        else:
            self.fail(f"the server never answered /health: {self.output()}")
        self.assertTrue(body["loaded"], body)
        self.assertEqual(body["engine"]["state"], "running")

    def test_the_fallback_is_stated_in_the_server_output(self) -> None:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            out = self.output()
            if "worker: not built" in out:
                break
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        self.assertIn("worker: not built", out)
        self.assertIn("direct mode", out)

    def test_a_chat_is_answered_over_the_plain_protocol(self) -> None:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3) as r:
                    if json.loads(r.read())["loaded"]:
                        break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        else:
            self.fail(f"the engine never came up: {self.output()}")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
        self.assertIn("choices", body)
        message = body["choices"][0]["message"]
        tokens = body["usage"]["completion_tokens"]
        self.assertGreater(tokens, 0, "the engine answered nothing over the plain protocol")
        self.assertTrue(message.get("content") or message.get("reasoning_content"),
                       "the tokens came back as neither content nor reasoning")


if __name__ == "__main__":
    unittest.main()

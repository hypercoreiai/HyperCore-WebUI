"""tests/test_offline_tabs.py - the app stays up when the engine is not.

PLAN_CHECKLIST sections 1 and 2: with no engine loaded, every non-generation route
must answer from host-side state, and /health must carry the schema the page reads
to mask the chat while leaving the other tabs interactive. The server here is
started with --ui-only, so no GPU and no model are involved: this is the audit of
"which routes touch the engine" written down as a test.
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

from serve.offline_engine import UNLOADED_CODE, OfflineEngine, reason_code

#: Routes that must answer with no engine at all. A generation route is not here:
#: /v1/chat/completions is checked separately and is meant to refuse.
STATIC_ROUTES = ("/health", "/config", "/mcp", "/build/status", "/tune/status",
                 "/metrics", "/v1/status", "/prompts", "/history/status", "/", "/web/app.js")


class ReasonCodeTests(unittest.TestCase):
    """The machine-readable half of an offline engine's reason."""

    def test_each_failure_kind_gets_its_own_code(self) -> None:
        cases = {
            "started with --ui-only: no engine was asked for": "ui_only",
            "the GPU is held by a tune sweep": "gpu_busy",
            "the engine is stopped: start it again": "stopped",
            "missing model shard /models/q2_0.gguf": "no_model",
            "engine exited with code 1": UNLOADED_CODE,
        }
        for detail, code in cases.items():
            self.assertEqual(reason_code(detail), code, detail)

    def test_an_empty_reason_is_not_a_crash(self) -> None:
        self.assertEqual(reason_code(""), UNLOADED_CODE)
        self.assertEqual(reason_code(None), UNLOADED_CODE)

    def test_the_stand_in_engine_reports_its_code(self) -> None:
        eng = OfflineEngine("the GPU is held by a build")
        status = eng.status()
        self.assertEqual(status["state"], "offline")
        self.assertEqual(status["code"], "gpu_busy")
        self.assertEqual(status["reason"], "the GPU is held by a build")


class OfflineServerTests(unittest.TestCase):
    """One --ui-only server, every static route hit against it."""

    STARTUP_S = 25

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "run-offline-test.json"
        self.config.write_text(json.dumps({"model_name": "offline-test",
                                          "sampling": {"temperature": 0.2}}), encoding="utf-8")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--ui-only", "--config", str(self.config),
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(ROOT), env=os.environ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.wait_for_health()

    def tearDown(self) -> None:
        if getattr(self, "proc", None):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_for_health(self) -> dict:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3) as r:
                    return json.loads(r.read())
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        out = self.proc.stdout.read().decode(errors="replace") if self.proc.stdout else ""
        self.fail(f"the --ui-only server never answered /health:\n{out}")

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    def test_health_reports_the_offline_schema_the_page_reads(self) -> None:
        status, body = self.get("/health")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "offline")
        self.assertFalse(payload["loaded"])
        self.assertEqual(payload["engine"]["state"], "offline")
        self.assertEqual(payload["engine"]["code"], "ui_only")
        self.assertIn("reason", payload["engine"])

    def test_every_static_route_answers_without_an_engine(self) -> None:
        for route in STATIC_ROUTES:
            status, body = self.get(route)
            self.assertEqual(status, 200, f"{route} answered {status}: {body[:200]}")
            if route not in ("/", "/web/app.js"):
                json.loads(body)  # a JSON schema, not an empty or half-built payload

    def test_the_chat_is_refused_and_the_page_is_still_served(self) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("a chat answered with no engine")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            self.assertEqual(json.loads(e.read())["error"]["type"], "engine_unavailable")
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("engine-offline", body)

    def test_the_run_config_is_read_without_touching_the_engine(self) -> None:
        status, body = self.get("/config")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertIn("file", payload)
        self.assertIn("keys", payload)

    def test_a_config_write_needs_no_engine(self) -> None:
        """The Settings view writes to config/run-<model>.json with plain file access.
        Nothing here asks the engine whether it is up."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/config", method="POST",
            data=json.dumps({"set": {"sampling.temperature": 0.4}}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            self.fail(f"POST /config answered {e.code}: {e.read()[:200]}")
        self.assertIn("sampling.temperature", payload.get("changed", []))
        status, body = self.get("/config")
        self.assertEqual(status, 200, body)
        values = {k["key"]: k["value"] for k in json.loads(body)["keys"]}
        self.assertEqual(values["sampling.temperature"], 0.4)


if __name__ == "__main__":
    unittest.main()

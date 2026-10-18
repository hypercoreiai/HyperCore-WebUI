"""tests/test_version_routes.py - GET /versions and POST /versions/select.

The Model version dropdown in the header is only as good as these two routes: the
first must answer with what the machine can start even when no engine is loaded, the
second must either switch or say why it could not - never crash the server. No GPU is
involved: the server here is --ui-only, so the engine is serve/offline_engine.py and
the switch is checked for its contract, not for a model load.

    python3 -m pytest tests/test_version_routes.py -q
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from serve import quant_store


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class VersionRouteTests(unittest.TestCase):
    STARTUP_S = 25

    def setUp(self) -> None:
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--ui-only", "--no-attach",
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

    def wait_for_health(self) -> None:
        deadline = time.time() + self.STARTUP_S
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3) as r:
                    r.read()
                    return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        out = self.proc.stdout.read().decode(errors="replace") if self.proc.stdout else ""
        self.fail(f"the --ui-only server never answered /health:\n{out}")

    def request(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_get_versions_lists_every_version_the_machine_can_start(self) -> None:
        status, payload = self.request("/versions")
        self.assertEqual(status, 200)
        self.assertIn("versions", payload)
        self.assertIn("active", payload)
        self.assertEqual(payload["count"], len(payload["versions"]))
        for entry in payload["versions"]:
            for key in ("key", "label", "quant", "ready", "active"):
                self.assertIn(key, entry, f"an entry is missing {key}")

    def test_the_listing_is_the_same_the_store_builds(self) -> None:
        _, payload = self.request("/versions")
        self.assertEqual(payload["versions"], quant_store.list_versions())

    def test_an_unknown_version_is_refused_with_the_names_that_are_available(self) -> None:
        status, payload = self.request("/versions/select", {"version": "zz9_not_a_quant"})
        self.assertEqual(status, 400)
        self.assertIn("no model version zz9_not_a_quant is available", payload["error"]["message"])

    def test_a_version_with_no_name_is_a_400_not_a_crash(self) -> None:
        status, payload = self.request("/versions/select", {})
        self.assertEqual(status, 400)
        self.assertIn("version", payload["error"]["message"])

    def test_selecting_a_config_only_version_reports_the_switch_and_keeps_the_app_up(self) -> None:
        versions = quant_store.list_versions()
        if not versions:
            self.skipTest("this machine has no run config naming a quant")
        key = versions[0]["key"]
        status, payload = self.request("/versions/select", {"version": key})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "switching")
        self.assertEqual(payload["version"], key)
        self.assertIn("Load it", payload["message"])
        # the app must still answer afterwards
        self.assertEqual(self.request("/health")[0], 200)

    def test_the_page_ships_the_dropdown(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            body = r.read().decode(errors="replace")
        self.assertEqual(r.status, 200)
        self.assertIn('id="version-select"', body)


if __name__ == "__main__":
    unittest.main()

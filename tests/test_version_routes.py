"""tests/test_version_routes.py - GET /versions and POST /versions/select.

The Model version dropdown in the header is only as good as these two routes: the
first must answer with what the machine can start even when no engine is loaded, the
second must either switch or say why it could not - never crash the server. No GPU is
involved: the server here is --ui-only, so the engine is serve/offline_engine.py and
the switch is checked for its contract, not for a model load.

The server is started with an API key of its own. Without one it takes the key from
whichever run config it picks up (serve/server.py: `a.api_key or cfg.get("api_key")`),
and a run config written by the Build tab carries "0000" - so on a machine that has
built a quant the routes below answer 401 to a client that knows nothing about it.
Naming the key here makes the test the same on every machine, and the gate it walks
through is the one in serve/http_auth.py, not a copy of it.

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

#: The key this test's own server requires, and sends back. Chosen so it cannot collide
#: with a key a real config on this machine might hold.
API_KEY = "version-routes-test-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class VersionRouteTests(unittest.TestCase):
    STARTUP_S = 25

    def setUp(self) -> None:
        self.port = free_port()
        # These tests run against the real store, and selecting a version places that
        # build's binaries at engine/strata. Remember what was running so the test does
        # not leave the machine on a different engine than it started with.
        self._active_before = quant_store.index().get("active")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--ui-only", "--no-attach",
             "--host", "127.0.0.1", "--port", str(self.port), "--api-key", API_KEY],
            cwd=str(ROOT), env=os.environ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.wait_for_health()

    def tearDown(self) -> None:
        if getattr(self, "proc", None):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._active_before:
            try:
                quant_store.select(self._active_before)
            except ValueError:
                pass

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

    def request(self, path: str, body: dict | None = None, key: str | None = None) -> tuple[int, dict]:
        """One call to this test's server. `key=None` means the test's own key.

        Pass key="" to ask as a client that has no credential at all.
        """
        headers = {"Authorization": "Bearer " + (API_KEY if key is None else key)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers)
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

    def test_a_request_with_no_key_is_401_with_the_body_every_front_uses(self) -> None:
        status, payload = self.request("/versions", key="")
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": {"type": "authentication_error",
                                            "message": "missing or wrong API key"}})

    def test_the_wrong_key_is_refused_and_the_api_key_header_is_accepted(self) -> None:
        self.assertEqual(self.request("/versions", key="not-the-key")[0], 401)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/versions",
                                     headers={"x-api-key": API_KEY})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_health_is_open_so_a_monitoring_probe_can_still_use_it(self) -> None:
        # serve/http_auth.py: the gate does not cover health, and this is the route the
        # server's own startup wait depends on.
        self.assertEqual(self.request("/health", key="")[0], 200)

    def test_the_page_ships_the_dropdown(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            body = r.read().decode(errors="replace")
        self.assertEqual(r.status, 200)
        self.assertIn('id="version-select"', body)


if __name__ == "__main__":
    unittest.main()

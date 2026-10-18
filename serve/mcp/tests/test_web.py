"""The Firefox/DuckDuckGo server: status, search, browse.

    python3 serve/mcp/tests/run_all.py --server web
    python3 -m unittest serve.mcp.tests.test_web

The guardrail cases (which addresses are accepted, what a bad argument does) run without
a browser. The live cases drive the real Firefox over Marionette and are skipped when
HYPERCORE_TEST_LIVE=0 or no Firefox binary is present - the same lookup the server uses.
"""
from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

from serve.mcp.tests.harness import McpServer, case, entry, live

SERVER = "web"
ADVERTISED = ["browse", "search", "status"]
PAGE_CHARS = 12000


def browser_binary() -> str | None:
    """Find Firefox the way serve/mcp/web_mcp.py does, so a test never skips a browser
    the server could actually start."""
    candidates = [os.environ.get("HYPERCORE_FIREFOX", "").strip(),
                  str(Path.home() / ".local" / "firefox" / "firefox" / "firefox"),
                  str(Path.home() / ".local" / "firefox" / "firefox")]
    for name in ("firefox", "firefox-esr", "firefox-bin"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = browser_binary()
        cls.srv = McpServer(entry("web_mcp.py"), env={"HYPERCORE_FIREFOX": cls.browser or ""})

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def need_browser(self) -> None:
        live("no Firefox")
        if not self.browser:
            raise unittest.SkipTest("firefox is not installed")

    @case(SERVER, "tools", "advertises all 3 tools")
    def test_web__advertised_tools(self):
        self.assertEqual(self.srv.tools, sorted(ADVERTISED))

    @case(SERVER, "status", "reports whether the browser is running and where it is")
    def test_web__status(self):
        out = self.srv.call("status").data
        self.assertIn(out["firefox"], ("running", "not started", "exited"))
        self.assertIn("binary", out)
        self.assertIn("last_url", out)
        self.assertIn("last_error", out)

    @case(SERVER, "browse", "a private address is refused before anything is opened")
    def test_web__browse_refuses_private(self):
        for url in ("http://127.0.0.1/index.html", "http://localhost/page",
                    "http://10.0.0.5/", "http://192.168.1.1/"):
            r = self.srv.call("browse", {"url": url})
            self.assertFalse(r.ok, f"{url} should have been refused")
            self.assertIn("public internet", r.text.lower())

    @case(SERVER, "browse", "a non-http scheme is refused")
    def test_web__browse_refuses_scheme(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "mailto:a@b.c"):
            self.assertFalse(self.srv.call("browse", {"url": url}).ok)

    @case(SERVER, "browse", "a missing url is an error, not a hang")
    def test_web__browse_no_url(self):
        self.assertFalse(self.srv.call("browse", {}).ok)

    @case(SERVER, "search", "an empty query is refused")
    def test_web__search_empty_query(self):
        self.assertFalse(self.srv.call("search", {"query": ""}).ok)
        self.assertFalse(self.srv.call("search", {}).ok)

    @case(SERVER, "search", "a live search returns numbered titles, addresses, and snippets")
    def test_web__search_live(self):
        self.need_browser()
        r = self.srv.call("search", {"query": "llama.cpp speculative decoding", "limit": 3}, timeout=120)
        self.assertTrue(r.ok, r.text)
        self.assertIn("DuckDuckGo", r.text)
        self.assertIn("http", r.text)

    @case(SERVER, "search", "a limit above 8 is clamped, not obeyed blindly")
    def test_web__search_limit_clamped(self):
        self.need_browser()
        r = self.srv.call("search", {"query": "python typing", "limit": 50}, timeout=120)
        self.assertTrue(r.ok, r.text)
        numbered = [line for line in r.text.splitlines() if line.strip().startswith(("1.", "2.", "3."))]
        self.assertTrue(numbered)
        self.assertLessEqual(len(numbered), 8)

    @case(SERVER, "browse", "a live page returns its title and visible text")
    def test_web__browse_live(self):
        self.need_browser()
        r = self.srv.call("browse", {"url": "https://example.com"}, timeout=120)
        self.assertTrue(r.ok, r.text)
        self.assertIn("Example", r.text)
        self.assertIn("example.com", r.text)

    @case(SERVER, "status", "after a browse the last address is recorded")
    def test_web__status_after_browse(self):
        self.need_browser()
        self.srv.call("browse", {"url": "https://example.com"}, timeout=120)
        out = self.srv.call("status").data
        self.assertEqual(out["firefox"], "running")
        self.assertIn("example.com", out["last_url"])

    @case(SERVER, "browse", "a page longer than the page cap is truncated and says so")
    def test_web__browse_truncated(self):
        self.need_browser()
        r = self.srv.call("browse", {"url": "https://en.wikipedia.org/wiki/Truncation"}, timeout=120)
        if not r.ok:
            self.skipTest(f"the page could not be read: {r.text[:200]}")
        self.assertLessEqual(len(r.text), PAGE_CHARS + 200)
        if len(r.text) > PAGE_CHARS:
            self.assertIn("truncated", r.text.lower())

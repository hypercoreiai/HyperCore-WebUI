"""The stealth browser server: engine choice, guardrails, fetch, links, probe, screenshot.

    python3 serve/mcp/tests/run_all.py --server browser
    python3 -m unittest serve.mcp.tests.test_browser

Every case here runs against stub_browser.py, the stand-in engine, so the tools are
exercised on a box with no Firefox at all. The live cases drive the real engine -
invisible_playwright, Camoufox or stock Playwright - and are skipped when
HYPERCORE_TEST_LIVE=0 or no engine can be imported.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from serve.mcp.tests.harness import McpServer, case, entry, live

SERVER = "browser"
ADVERTISED = ["close", "fetch", "links", "probe", "screenshot", "status"]
STUB = str(Path(__file__).resolve().parent / "stub_browser.py")

TEXT_CLEAN = ("Example Domain\nThis domain is for use in illustrative examples in documents.")
TEXT_LONG = "alpha beta " * 400
TEXT_CHALLENGE = "Just a second... Checking your browser before accessing the site."
LINKS = [{"text": "Home", "url": "https://example.com/", "rel": ""},
         {"text": "IETF", "url": "https://www.ietf.org/", "rel": ""},
         {"text": "Home again", "url": "https://example.com/", "rel": ""},
         {"text": "Other", "url": "https://other.example.org/x", "rel": "nofollow"}]


def stub_env(shots: str) -> dict:
    """The stub engine, a fixture page, and a private screenshot directory."""
    return {"HYPERCORE_BROWSER_STUB": STUB, "HYPERCORE_BROWSER_SHOTS": shots,
           "HYPERCORE_BROWSER_TIMEOUT_MS": "5000"}


def engine_installed() -> bool:
    """Can any of the engines the server supports be imported in this interpreter?"""
    import importlib
    for name in ("invisible_playwright", "camoufox", "playwright"):
        try:
            importlib.import_module(name)
            return True
        except ImportError:
            continue
    return False


class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shots = tempfile.mkdtemp(prefix="browser-shots-")
        cls.srv = McpServer(entry("browser_mcp.py"), env=stub_env(cls.shots))

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        import shutil
        shutil.rmtree(cls.shots, ignore_errors=True)

    def fixture(self, **values: str) -> McpServer:
        """A fresh server with the stub's page set to these values."""
        env = dict(stub_env(self.shots))
        env.update({k: v for k, v in values.items() if v is not None})
        return McpServer(entry("browser_mcp.py"), env=env)

    @case(SERVER, "tools", "advertises all 6 tools")
    def test_browser__advertised_tools(self):
        self.assertEqual(self.srv.tools, sorted(ADVERTISED))

    @case(SERVER, "status", "reports every engine and whether it is installed")
    def test_browser__status_lists_engines(self):
        out = self.srv.call("status").data
        self.assertIsNotNone(out, "status did not return json")
        self.assertEqual([e["name"] for e in out["engines"]],
                         ["invisible_playwright", "camoufox", "playwright"])
        for engine in out["engines"]:
            self.assertIsInstance(engine["installed"], bool)
        self.assertEqual(out["browser"], "not started")
        self.assertEqual(out["last_url"], "")

    @case(SERVER, "status", "says how to install an engine when none is present")
    def test_browser__status_hint_when_nothing_installed(self):
        out = self.srv.call("status").data
        if any(e["installed"] for e in out["engines"]):
            self.assertEqual(out["hint"], "")
        else:
            self.assertIn("invisible-playwright", out["hint"])
            self.assertIn("camoufox", out["hint"])

    @case(SERVER, "fetch", "a private address is refused before the browser is opened")
    def test_browser__fetch_refuses_private(self):
        for url in ("http://127.0.0.1/x", "http://localhost/page", "http://10.0.0.5/",
                    "http://192.168.1.1/", "http://somehost.local/"):
            r = self.srv.call("fetch", {"url": url})
            self.assertFalse(r.ok, f"{url} should have been refused")
            self.assertIn("public internet", r.text.lower())

    @case(SERVER, "fetch", "a non-http scheme and a missing url are errors, not hangs")
    def test_browser__fetch_refuses_bad_input(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "mailto:a@b.c", ""):
            self.assertFalse(self.srv.call("fetch", {"url": url}).ok)
        self.assertFalse(self.srv.call("fetch").ok)

    @case(SERVER, "fetch", "an unknown engine name is refused with the valid list")
    def test_browser__unknown_engine(self):
        srv = McpServer(entry("browser_mcp.py"), env={"HYPERCORE_BROWSER_ENGINE": "firefox_stealth"})
        try:
            r = srv.call("fetch", {"url": "https://example.com"})
            self.assertFalse(r.ok)
            self.assertIn("invisible_playwright", r.text)
        finally:
            srv.close()

    @case(SERVER, "fetch", "the page title and text come back through the engine")
    def test_browser__fetch_returns_text(self):
        srv = self.fixture(STUB_TITLE="Example Domain", STUB_TEXT=TEXT_CLEAN)
        try:
            out = srv.call("fetch", {"url": "https://example.com/"})
            self.assertTrue(out.ok, out.text)
            self.assertIn("Example Domain", out.text)
            self.assertIn("illustrative examples", out.text)
        finally:
            srv.close()

    @case(SERVER, "fetch", "a long page is truncated and the remainder is counted")
    def test_browser__fetch_truncates(self):
        srv = self.fixture(STUB_TEXT=TEXT_LONG, HYPERCORE_BROWSER_MAX_CHARS="500")
        try:
            out = srv.call("fetch", {"url": "https://example.com/"})
            self.assertTrue(out.ok)
            self.assertIn("truncated", out.text)
            self.assertIn("more characters were not returned", out.text)
            self.assertLess(len(out.text), len(TEXT_LONG))
        finally:
            srv.close()

    @case(SERVER, "links", "anchors are returned with absolute addresses and no duplicates")
    def test_browser__links_deduplicates(self):
        srv = self.fixture(STUB_LINKS=json.dumps(LINKS))
        try:
            out = srv.call("links", {"url": "https://example.com/"}).data
            self.assertEqual(out["count"], 3)
            self.assertFalse(out["truncated"])
            self.assertEqual(out["links"][0]["url"], "https://example.com/")
            self.assertEqual(out["links"][2]["rel"], "nofollow")
        finally:
            srv.close()

    @case(SERVER, "links", "the limit is honoured and truncation is reported")
    def test_browser__links_limit(self):
        srv = self.fixture(STUB_LINKS=json.dumps(LINKS))
        try:
            out = srv.call("links", {"url": "https://example.com/", "limit": 2}).data
            self.assertEqual(out["count"], 2)
            self.assertTrue(out["truncated"])
        finally:
            srv.close()

    @case(SERVER, "probe", "a clean page reports no trace and no challenge")
    def test_browser__probe_clean(self):
        srv = self.fixture(STUB_TEXT=TEXT_CLEAN, STUB_WEBDRIVER="false", STUB_GL="Intel HD Graphics")
        try:
            out = srv.call("probe", {"url": "https://example.com/"}).data
            self.assertFalse(out["navigator"]["webdriver"])
            self.assertEqual(out["automation_traces"], {})
            self.assertEqual(out["challenge_markers"], [])
            self.assertEqual(out["verdict"], "no challenge or automation trace seen")
            self.assertIn("Intel HD Graphics", out["webgl"]["renderer"])
        finally:
            srv.close()

    @case(SERVER, "probe", "an interstitial is named as a bot challenge")
    def test_browser__probe_challenge(self):
        srv = self.fixture(STUB_TEXT=TEXT_CHALLENGE, STUB_WEBDRIVER="true",
                          STUB_TRACES=json.dumps(["__playwright__binding__"]))
        try:
            out = srv.call("probe", {"url": "https://example.com/"}).data
            self.assertTrue(out["navigator"]["webdriver"])
            self.assertIn("just a second", out["challenge_markers"])
            self.assertEqual(out["automation_traces"], {"__playwright__binding__": True})
            self.assertEqual(out["verdict"], "looks like a bot challenge")
        finally:
            srv.close()

    @case(SERVER, "screenshot", "a PNG is written to the configured directory")
    def test_browser__screenshot_writes_a_file(self):
        srv = self.fixture()
        try:
            out = srv.call("screenshot", {"url": "https://example.com/",
                                         "filename": "example page.png"}).data
            path = Path(out["path"])
            self.assertTrue(path.exists(), f"{path} was not written")
            self.assertGreater(out["bytes"], 0)
            self.assertEqual(path.suffix, ".png")
        finally:
            srv.close()

    @case(SERVER, "tools", "several calls in a row reach the same engine thread")
    def test_browser__calls_share_one_thread(self):
        """A Playwright page belongs to the thread that opened it. The stub raises when it
        is used from another thread, so this fails if a call is dispatched on its own."""
        srv = self.fixture(STUB_TEXT=TEXT_CLEAN, STUB_LINKS=json.dumps(LINKS), STUB_GL="Intel HD Graphics")
        try:
            for tool, args in (("fetch", {"url": "https://example.com/"}),
                               ("links", {"url": "https://example.com/", "limit": 3}),
                               ("probe", {"url": "https://example.com/"}),
                               ("screenshot", {"url": "https://example.com/"})):
                r = srv.call(tool, args)
                self.assertTrue(r.ok, f"{tool} failed after an earlier call: {r.text[:200]}")
        finally:
            srv.close()

    @case(SERVER, "close", "closing is reported and the next call reopens a session")
    def test_browser__close_then_reopen(self):
        srv = self.fixture(STUB_TEXT=TEXT_CLEAN)
        try:
            srv.call("fetch", {"url": "https://example.com/"})
            self.assertEqual(srv.call("status").data["browser"], "running")
            self.assertIn("closed", srv.call("close").text)
            self.assertEqual(srv.call("status").data["browser"], "closed")
            self.assertTrue(srv.call("fetch", {"url": "https://example.com/"}).ok)
        finally:
            srv.close()

    @case(SERVER, "tools", "an unknown tool name is an error, not a crash")
    def test_browser__unknown_tool(self):
        r = self.srv.call("does_not_exist", {"url": "https://example.com/"})
        self.assertFalse(r.ok)
        self.assertIn("unknown tool", r.text)

    @case(SERVER, "status", "the live engine opens a page, reads it, and reports itself")
    def test_browser__live_engine(self):
        live("no browser engine")
        if not engine_installed():
            raise unittest.SkipTest("no browser engine is installed")
        srv = McpServer(entry("browser_mcp.py"), env={"HYPERCORE_BROWSER_SHOTS": self.shots})
        try:
            out = srv.call("status", timeout=60).data
            self.assertTrue(any(e["installed"] for e in out["engines"]))
            r = srv.call("probe", {"url": "https://example.com/"}, timeout=90)
            if not r.ok:
                self.skipTest(f"the engine could not open example.com: {r.text[:200]}")
            data = r.data
            self.assertIn("verdict", data)
            self.assertFalse(data["navigator"].get("webdriver", False))
            # the same session, several tools in a row: the thread-affinity guard
            links = srv.call("links", {"url": "https://example.com/", "limit": 3}, timeout=90)
            self.assertTrue(links.ok, f"links failed after probe: {links.text[:200]}")
            self.assertGreater(links.data["count"], 0)
            shot = srv.call("screenshot", {"url": "https://example.com/"}, timeout=90)
            self.assertTrue(shot.ok, f"screenshot failed after links: {shot.text[:200]}")
            self.assertTrue(Path(shot.data["path"]).exists())
            self.assertEqual(srv.call("status").data["browser"], "running")
            self.assertIn("closed", srv.call("close").text)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()

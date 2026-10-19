"""A stand-in engine for serve/mcp/browser_mcp.py.

browser_mcp.py opens whichever engine is installed, so on a box with no Firefox its
tools can never be exercised. This stub implements the same page methods the server
calls - goto, title, inner_text, evaluate, screenshot - against a page the test
supplies, so fetch, links, probe, screenshot and close are tested end to end without
a browser, a network, or a 260 MB engine download.

    HYPERCORE_BROWSER_STUB=serve/mcp/tests/stub_browser.py python3 serve/mcp/browser_mcp.py

Environment the stub reads
  STUB_TITLE     the page title
  STUB_TEXT      the body's inner text
  STUB_LINKS     a JSON array of {text, url, rel} anchors
  STUB_WEBDRIVER true|false, what navigator.webdriver reports
  STUB_GL        a WebGL renderer string, or empty for none
  STUB_TRACES    a JSON array of automation trace names the page exposes
  STUB_DELAY_MS  milliseconds goto() spends, so a test can make a call outlive stdin
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

PNG_STUB = b"\x89PNG\r\n\x1a\n" + b"stub-image" * 6


class StubPage:
    """The four methods browser_mcp calls, answering from the fixture in the environment.

    Like a real Playwright page it is bound to the thread that opened it: every call from
    a different thread raises. That is what makes the stub catch a server that dispatches
    tool calls on one thread each, instead of on one worker.
    """

    def __init__(self) -> None:
        self.thread = threading.get_ident()
        self.url = "about:blank"
        self.title_text = os.environ.get("STUB_TITLE", "Example Domain")
        self.body_text = os.environ.get(
            "STUB_TEXT",
            "Example Domain\nThis domain is for use in illustrative examples in documents.")
        try:
            self.anchors = json.loads(os.environ.get("STUB_LINKS", "[]"))
        except ValueError:
            self.anchors = []
        self.webdriver = os.environ.get("STUB_WEBDRIVER", "false").strip().lower() in {"1", "true", "yes"}
        self.renderer = os.environ.get("STUB_GL", "")
        try:
            self.traces = {name: True for name in json.loads(os.environ.get("STUB_TRACES", "[]"))}
        except ValueError:
            self.traces = {}

    def _same_thread(self) -> None:
        if threading.get_ident() != self.thread:
            raise RuntimeError("cannot switch to a different thread (which happens to have exited)")

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int | None = None) -> None:
        self._same_thread()
        raw_delay = os.environ.get("STUB_DELAY_MS", "").strip()
        if raw_delay:
            try:
                time.sleep(max(0.0, int(raw_delay) / 1000.0))
            except ValueError:
                pass  # a malformed delay just means no delay
        self.url = url

    def title(self) -> str:
        self._same_thread()
        return self.title_text

    def inner_text(self, selector: str) -> str:
        self._same_thread()
        return self.body_text

    def set_default_timeout(self, timeout: int) -> None:
        pass

    def evaluate(self, script: str):
        self._same_thread()
        if "querySelectorAll('a[href]')" in script:
            return self.anchors
        if "navigator.webdriver" in script:
            return {"webdriver": self.webdriver, "userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
                    "platform": "Linux", "languages": ["en-US"], "screen": {"width": 1920, "height": 1080},
                    "webgl": {"vendor": "Google Inc.", "renderer": self.renderer} if self.renderer else {},
                    "traces": self.traces}
        raise ValueError(f"the stub does not answer that script: {script[:80]}")

    def screenshot(self, path: str | None = None, full_page: bool = False) -> bytes:
        self._same_thread()
        if path:
            Path(path).write_bytes(PNG_STUB)
        return PNG_STUB


class StubBrowser:
    def __init__(self) -> None:
        self._page = StubPage()
        self.closed = False

    def new_page(self) -> StubPage:
        return self._page

    def close(self) -> None:
        self.closed = True


def open_browser():
    """The entry point browser_mcp._open_stub expects: (browser, close)."""
    browser = StubBrowser()
    return browser, lambda *_args: browser.close()

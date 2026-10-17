#!/usr/bin/env python3
"""serve/mcp/browser_mcp.py - MCP server: the C++-patched stealth Firefox.

The engine is a custom-compiled, source-patched Firefox - `invisible_playwright`
(feder-cr/firefox_antidetect_patch) or Camoufox - where navigator, screen, WebGL,
canvas, fonts and the rest are set inside the browser's C++ rather than injected with
JavaScript, so a page cannot find an override to read.  Stock Playwright Firefox is
kept as a last resort so the tools still work while the engine is being installed.

Nothing browser-related is imported at start-up: `status` answers on a box with no
engine at all and says which one is missing and how to get it.  The browser is opened
on the first tool that needs it and reused after that.

    python serve/mcp/browser_mcp.py          stdio, JSON-RPC one message per line

Environment
  HYPERCORE_BROWSER_ENGINE      force one engine: invisible_playwright | camoufox | playwright
  HYPERCORE_BROWSER_STUB        a python file used as the engine instead (the test stub)
  HYPERCORE_BROWSER_HEADLESS    0 to open a window
  HYPERCORE_BROWSER_TIMEOUT_MS  page load timeout, default 30000
  HYPERCORE_BROWSER_MAX_CHARS   page text cap, default 12000
  HYPERCORE_BROWSER_SHOTS       where screenshots go, default $HOME/downloads/browser
"""
from __future__ import annotations

import importlib
import importlib.util
import ipaddress
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

SERVER = "browser"
ENGINE_ORDER = ("invisible_playwright", "camoufox", "playwright")
DEFAULT_PAGE_CHARS = 12000
DEFAULT_TIMEOUT_MS = 30000
WORKER_TIMEOUT_S = 80.0   # under the hub's timeout_s: a slow page returns an error, not a killed call
LINK_DEFAULT = 40
LINK_MAX = 60

# Phrases an anti-bot interstitial leaves in the page text.  A hit is a warning, not proof:
# a page can legitimately contain the word "captcha".
CHALLENGE_MARKERS = (
    "just a second", "just a moment", "verify you are human", "checking your browser",
    "captcha", "access denied", "enable javascript and cookies", "are you a robot",
    "attention required", "unusual traffic", "blocked this request",
)

AUTOMATION_TRACES = ("__playwright__binding__", "__playwright__", "__camoufox__",
                     "__selenium__", "__webdriver__", "__nightmare__")


def env_int(name: str, default: int) -> int:
    """An integer from the environment, falling back to the default when unset or malformed."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def page_chars() -> int:
    return env_int("HYPERCORE_BROWSER_MAX_CHARS", DEFAULT_PAGE_CHARS)


def timeout_ms() -> int:
    return env_int("HYPERCORE_BROWSER_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)


def shots_dir() -> Path:
    raw = os.environ.get("HYPERCORE_BROWSER_SHOTS", "").strip()
    return Path(raw) if raw else Path.home() / "downloads" / "browser"


def headless() -> bool:
    return os.environ.get("HYPERCORE_BROWSER_HEADLESS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _quiet(fn: Callable[..., Any], *args: Any) -> str:
    """Call something that may already be gone; return its message if it raised, else "".

    Shutting a browser down is best effort - the engine may have exited on its own - and
    a teardown error must never turn a close into a tool failure.
    """
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 - teardown: there is nothing to propagate
        return str(e)
    return ""


def public_http_url(url: str) -> str:
    """An http(s) URL whose host resolves only to public addresses, or ValueError."""
    raw = (url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https addresses can be opened")
    if parts.username or parts.password:
        raise ValueError("remove the username and password from the address")
    host = parts.hostname
    if host.lower() in ("localhost", "127.0.0.1") or host.lower().endswith((".local", ".localhost", ".internal")):
        raise ValueError(f"{host} is not a public internet address")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [literal] if literal is not None else []
    if not addresses:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise ValueError(f"could not resolve {host}: {e}") from None
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses:
        raise ValueError(f"could not resolve {host}")
    for ip in addresses:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"{host} is not a public internet address")
    return raw


def _installed(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def engine_report() -> list[dict[str, Any]]:
    """Which of the supported engines can be imported here, in preference order."""
    report = []
    for name in ENGINE_ORDER:
        try:
            module = importlib.import_module(name)
        except ImportError:
            report.append({"name": name, "installed": False})
            continue
        report.append({"name": name, "installed": True,
                      "version": str(getattr(module, "__version__", "")),
                      "entry": ENTRY[name]})
    return report


def install_hint() -> str:
    return ("no browser engine is installed. The patched Firefox needs "
            "`pip install invisible-playwright` then `python -m invisible_playwright fetch` "
            "(about 260 MB, one time); Camoufox is `pip install camoufox`; "
            "plain Playwright is `pip install playwright && python -m playwright install firefox`.")


# How each engine is opened. Kept here so status can tell the operator what to run.
ENTRY = {
    "invisible_playwright": "from invisible_playwright import InvisiblePlaywright; with InvisiblePlaywright() as browser:",
    "camoufox": "from camoufox.sync_api import Camoufox; with Camoufox(headless=True) as browser:",
    "playwright": "from playwright.sync_api import sync_playwright; sync_playwright().start().firefox.launch()",
}


def _open_stub(path: str):
    """Load a python file as an engine. It must define open_browser() -> (browser, close)."""
    spec = importlib.util.spec_from_file_location("browser_stub", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load the engine stub {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    opener = getattr(module, "open_browser", None)
    if not callable(opener):
        raise TypeError(f"the engine stub {path} has no open_browser()")
    opened = opener()
    if not isinstance(opened, tuple) or len(opened) not in (2, 3):
        raise RuntimeError(f"the engine stub {path} must return (browser, close) or (browser, close, seed)")
    browser, close = opened[0], opened[1]
    seed = opened[2] if len(opened) == 3 else None
    return browser, close, seed


def _open_engine(name: str):
    """Open one engine and return (browser, close, seed). Each engine keeps its own entry point."""
    module = importlib.import_module(name)  # already checked by _installed()
    if name == "invisible_playwright":
        holder = module.InvisiblePlaywright(headless=headless())
        return holder.__enter__, holder.__exit__, getattr(holder, "seed", None)
    if name == "camoufox":
        holder = module.sync_api.Camoufox(headless=headless())
        return holder.__enter__, holder.__exit__, None
    if name == "playwright":
        driver = module.sync_api.sync_playwright().start()
        browser = driver.firefox.launch(headless=headless())

        def close(*_args):
            _quiet(browser.close)
            driver.stop()

        return lambda: browser, close, None
    raise ValueError(f"unknown engine {name!r}; choose one of {', '.join(ENGINE_ORDER)}")


class Worker:
    """All engine work on one thread.

    Playwright's sync API is thread-affine: a page opened on one thread cannot be used
    from another, and `stdio` answers each tools/call on its own thread so a slow page
    load never blocks `status`. Every call that touches the browser therefore goes through
    this worker, which runs them in order on a single thread.
    """

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.tasks: queue.Queue = queue.Queue()
        self.start_lock = threading.Lock()

    def run(self, fn: Callable[..., Any], *args: Any, timeout: float = WORKER_TIMEOUT_S) -> Any:
        with self.start_lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
        reply: queue.Queue = queue.Queue(1)
        self.tasks.put((fn, args, reply))
        try:
            done, value = reply.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"the browser did not answer within {timeout} s") from None
        if done == "error":
            raise RuntimeError(value)
        return value

    def stop(self) -> None:
        """End the thread so the next session is opened on a fresh one."""
        thread = self.thread
        if thread is None:
            return
        self.tasks.put((None, (), None))
        thread.join(timeout=20)
        with self.start_lock:
            self.thread = None

    def _loop(self) -> None:
        while True:
            fn, args, reply = self.tasks.get()
            if fn is None:
                return
            try:
                reply.put(("ok", fn(*args)))
            except Exception as e:  # noqa: BLE001 - the caller turns it into a tool result
                reply.put(("error", str(e)))


class BrowserSession:
    """One stealth browser, opened on first use and reused. A fresh identity per launch."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.worker = Worker()
        self.browser: Any = None
        self._close: Callable[..., Any] | None = None
        self.engine: str = ""
        self.seed: Any = None
        self.page: Any = None
        self.last_url: str = ""
        self.last_error: str = ""
        self.opened_at: float = 0.0

    def running(self) -> bool:
        return self.browser is not None

    def state(self) -> str:
        if self.browser is not None:
            return "running"
        if self.engine:
            return "closed"
        return "not started"

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"engine": self.engine, "browser": self.state(), "seed": self.seed,
                    "headless": headless(), "last_url": self.last_url,
                    "last_error": self.last_error, "page_chars": page_chars(),
                    "shots_dir": str(shots_dir()), "engines": engine_report(),
                    "hint": "" if any(e["installed"] for e in engine_report()) else install_hint()}

    def _chosen(self) -> str:
        stub = os.environ.get("HYPERCORE_BROWSER_STUB", "").strip()
        if stub:
            return "stub"
        forced = os.environ.get("HYPERCORE_BROWSER_ENGINE", "").strip()
        if forced:
            if forced not in ENGINE_ORDER:
                raise ValueError(f"unknown engine {forced!r}; choose one of {', '.join(ENGINE_ORDER)}")
            if not _installed(forced):
                raise RuntimeError(f"{forced} is not installed. {install_hint()}")
            return forced
        for name in ENGINE_ORDER:
            if _installed(name):
                return name
        raise RuntimeError(install_hint())

    def _ensure(self) -> Any:
        if self.browser is not None:
            return self.page
        engine = self._chosen()
        if engine == "stub":
            browser, close, seed = _open_stub(os.environ["HYPERCORE_BROWSER_STUB"])
        else:
            enter, close, seed = _open_engine(engine)
            browser = enter()
        self.browser, self._close, self.engine, self.seed = browser, close, engine, seed
        self.page = self.browser.new_page()
        _quiet(self.page.set_default_timeout, timeout_ms())
        self.opened_at = time.monotonic()
        self.last_error = ""
        return self.page

    def _reset(self) -> None:
        browser, close = self.browser, self._close
        self.browser, self._close, self.page = None, None, None
        if close is not None:
            _quiet(close, None, None, None)
        elif browser is not None:
            _quiet(browser.close)

    def _goto(self, url: str):
        url = public_http_url(url)
        page = self._ensure()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms())
        except Exception as e:  # noqa: BLE001 - an engine failure is a tool result, not a crash
            self.last_error = str(e)
            raise RuntimeError(f"could not open {url}: {e}") from None
        final = str(getattr(page, "url", "") or "")
        if final:
            try:
                public_http_url(final)
            except ValueError:
                self.last_error = f"the page redirected to a non-public address: {final}"
                raise RuntimeError(self.last_error) from None
        self.last_url = final or url
        return page

    def fetch(self, url: str, max_chars: int | None = None) -> str:
        """The visible text of a public page, through the patched Firefox."""
        limit = max(200, min(50000, int(max_chars or page_chars())))
        page = self._goto(url)
        title = str(page.title() or "")
        try:
            body = str(page.inner_text("body") or "").strip()
        except Exception:  # noqa: BLE001 - an empty body has no inner text
            body = ""
        if not body:
            return f"{title}\n{self.last_url}\n\n(the page had no text)"
        if len(body) > limit:
            body = body[:limit] + f"\n\n[... truncated: {len(body) - limit:,} more characters were not returned]"
        return f"{title}\n{self.last_url}\n\n{body}"

    def links(self, url: str, limit: int = LINK_DEFAULT) -> dict[str, Any]:
        """Every anchor on a public page, with its absolute address."""
        limit = max(1, min(LINK_MAX, int(limit or LINK_DEFAULT)))
        page = self._goto(url)
        anchors = page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => ({ "
            "text: (a.textContent || '').trim(), url: a.href, "
            "rel: a.getAttribute('rel') || '' }))")
        rows = []
        seen: set[str] = set()
        for row in anchors if isinstance(anchors, list) else []:
            if not isinstance(row, dict):
                continue
            href = str(row.get("url") or "")
            if not href or href in seen:
                continue
            seen.add(href)
            rows.append({"text": str(row.get("text") or "")[:200], "url": href,
                        "rel": str(row.get("rel") or "")})
            if len(rows) >= limit:
                break
        return {"url": self.last_url, "count": len(rows), "truncated": len(rows) >= limit, "links": rows}

    def screenshot(self, url: str, filename: str = "") -> dict[str, Any]:
        """A full-page PNG of a public page, written under HYPERCORE_BROWSER_SHOTS."""
        page = self._goto(url)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", filename.strip()) if filename else ""
        if not name:
            host = urllib.parse.urlsplit(self.last_url).hostname or "page"
            name = f"{host}_{int(time.time())}.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        directory = shots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        page.screenshot(path=str(path), full_page=True)
        size = path.stat().st_size if path.exists() else 0
        return {"path": str(path), "bytes": size, "url": self.last_url}

    def probe(self, url: str) -> dict[str, Any]:
        """What the page can see about the browser: the check the anti-bot systems run."""
        page = self._goto(url)
        facts = page.evaluate(
            "() => { const out = {}; out.webdriver = navigator.webdriver; "
            "out.userAgent = navigator.userAgent; out.platform = navigator.platform; "
            "out.languages = Array.from(navigator.languages || []); "
            "out.screen = { width: screen.width, height: screen.height }; "
            "out.webgl = {}; "
            "try { const c = document.createElement('canvas'); "
            "const gl = c.getContext('webgl') || c.getContext('experimental-webgl'); "
            "if (gl) { const ext = gl.getExtension('WEBGL_debug_renderer_info'); "
            "out.webgl = { vendor: ext ? gl.getParameter(ext.VENDOR) : null, "
            "renderer: ext ? gl.getParameter(ext.RENDERER) : null }; } } "
            "catch (e) { out.webgl = { error: String(e) }; } "
            "out.traces = {}; "
            f"for (const key of {json.dumps(list(AUTOMATION_TRACES))}) out.traces[key] = (key in window); "
            "return out; }")
        try:
            body = str(page.inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            body = ""
        facts = facts if isinstance(facts, dict) else {}
        traces = {k: v for k, v in (facts.get("traces") or {}).items() if v}
        markers = [m for m in CHALLENGE_MARKERS if m in body]
        return {"url": self.last_url, "engine": self.engine or "not opened",
                "navigator": {k: facts.get(k) for k in ("webdriver", "userAgent", "platform", "languages", "screen")},
                "webgl": facts.get("webgl") or {},
                "automation_traces": traces,
                "challenge_markers": markers,
                "verdict": ("looks like a bot challenge" if markers or traces
                           else "no challenge or automation trace seen")}

    def close(self) -> str:
        if self.browser is None:
            return "no browser is open"
        self.worker.run(self._reset, timeout=30.0)   # close it on the thread that opened it
        self.worker.stop()
        return "the browser is closed; the next call opens a fresh one with a new fingerprint"


SESSION = BrowserSession()

TOOLS = [
    {"name": "status",
     "description": "Which stealth engine is installed, which one is running, and the last page opened.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fetch",
     "description": "Open a public page in the patched Firefox and return its title and visible text.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The http or https address."},
         "max_chars": {"type": "integer", "description": "Page text limit, default 12000."}},
         "required": ["url"]}},
    {"name": "links",
     "description": "Every anchor on a public page, with absolute addresses. Useful for following a site.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The http or https address."},
         "limit": {"type": "integer", "description": "How many links to return, 1 to 60. Default 40."}},
         "required": ["url"]}},
    {"name": "screenshot",
     "description": "A full-page PNG of a public page, saved under HYPERCORE_BROWSER_SHOTS.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The http or https address."},
         "filename": {"type": "string", "description": "Optional file name; otherwise host plus a stamp."}},
         "required": ["url"]}},
    {"name": "probe",
     "description": "What an anti-bot can see about this browser: navigator.webdriver, WebGL, "
                   "automation traces, and whether the page is a challenge interstitial.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "description": "The page to test."}},
         "required": ["url"]}},
    {"name": "close",
     "description": "Close the browser. The next call starts a new session with a new fingerprint.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def text(body: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": error}


def call(name: str, args: dict) -> dict:
    try:
        if name == "status":
            return text(json.dumps(SESSION.status(), indent=1))
        if name == "fetch":
            return text(SESSION.worker.run(SESSION.fetch, str(args.get("url") or ""), args.get("max_chars")))
        if name == "links":
            return text(json.dumps(SESSION.worker.run(SESSION.links, str(args.get("url") or ""),
                                                     int(args.get("limit") or LINK_DEFAULT)), indent=1))
        if name == "screenshot":
            return text(json.dumps(SESSION.worker.run(SESSION.screenshot, str(args.get("url") or ""),
                                                     str(args.get("filename") or "")), indent=1))
        if name == "probe":
            return text(json.dumps(SESSION.worker.run(SESSION.probe, str(args.get("url") or "")), indent=1))
        if name == "close":
            return text(SESSION.close())
    except Exception as e:  # noqa: BLE001 - the model reads the failure as a tool result
        SESSION.last_error = str(e)
        return text(str(e), error=True)
    return text(f"unknown tool {name}", error=True)


def handle(msg: dict):
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER, "title": "Stealth Firefox (invisible_playwright / Camoufox)",
                           "version": "0.1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": call(str(params.get("name") or ""),
                                            params.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def stdio():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    out = threading.Lock()

    def answer(msg):
        reply = handle(msg)
        if reply is None:
            return
        with out:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("method") == "tools/call":
                threading.Thread(target=answer, args=(msg,), daemon=True).start()
            else:
                answer(msg)
    finally:
        SESSION.close()


if __name__ == "__main__":
    stdio()

#!/usr/bin/env python3
"""serve/web_mcp.py - MCP server: Firefox for public web pages, DuckDuckGo for search.

HyperCore starts it from mcp.yaml.  The Monitor shows it as soon as it answers; its tools are `status`, `search`,
and `browse`.  Firefox is a separate headless process with an empty profile (not the user's browser), started on
the first search or browse.  Only public http(s) addresses are opened.

    python serve/web_mcp.py          stdio, JSON-RPC one message per line
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

PAGE_CHARS = 12000
SEARCH_DEFAULT = 5
SEARCH_MAX = 8


def public_http_url(url: str) -> str:
    """An http(s) URL whose host resolves only to public addresses, or ValueError."""
    raw = (url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https addresses can be opened")
    if parts.username or parts.password:
        raise ValueError("remove the username and password from the address")
    host = parts.hostname
    if host.lower() == "localhost" or host.lower().endswith((".local", ".localhost", ".internal")):
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
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            raise ValueError(f"{host} is not a public internet address")
    return raw


def unwrap_ddg(url: str) -> str:
    """DuckDuckGo's result links are redirects; the real address is the `uddg` query value."""
    parts = urllib.parse.urlsplit(url or "")
    target = urllib.parse.parse_qs(parts.query).get("uddg", [None])[0]
    return urllib.parse.unquote(target) if target else url


def firefox_bin() -> str | None:
    chosen = os.environ.get("HYPERCORE_FIREFOX", "").strip()
    candidates = [chosen] if chosen else []
    candidates.append(str(Path.home() / ".local" / "firefox" / "firefox"))
    for name in ("firefox", "firefox-esr", "firefox-bin"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def encode_packet(payload) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"{len(raw)}:".encode("ascii") + raw


def read_packet(sock: socket.socket, timeout: float):
    """One Marionette message.  The socket's own timeout bounds the wait."""
    sock.settimeout(timeout)
    buf = b""
    while b":" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError("firefox closed the connection")
        buf += chunk
        if len(buf) > 20:
            raise ValueError("firefox sent a message that is not length-prefixed")
    length = int(buf[:-1])
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise EOFError("firefox closed the connection")
        body += chunk
    return json.loads(body.decode("utf-8"))


class Firefox:
    """One headless Firefox, spoken to through Marionette.  A fresh profile, never the user's."""

    def __init__(self):
        self.proc = None
        self.sock = None
        self.profile = None
        self.msg_id = 0
        self.last_url = ""
        self.last_error = ""
        self.stderr_tail = ""
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.sock is not None

    def status(self) -> dict:
        with self.lock:
            if self.running():
                state = "running"
            elif self.proc is None:
                state = "not started"
            else:
                state = "exited"
            return {"firefox": state, "binary": firefox_bin() or "", "last_url": self.last_url,
                    "last_error": self.last_error}

    def search(self, query: str, limit: int) -> str:
        query = (query or "").strip()
        if not query:
            raise ValueError("search needs a query")
        limit = max(1, min(SEARCH_MAX, int(limit or SEARCH_DEFAULT)))
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        with self.lock:
            self._open(url)
            rows = self._script(
                "const out = [];"
                "for (const n of document.querySelectorAll('.result, .web-result')) {"
                "  const a = n.querySelector('a.result__a, a.result-link, h2 a');"
                "  if (!a) continue;"
                "  const sn = n.querySelector('.result__snippet, .result-snippet');"
                "  out.push({title: (a.innerText || '').trim(), url: a.href || '',"
                "            snippet: sn ? sn.innerText.trim() : ''});"
                f"  if (out.length >= {int(limit)}) break;"
                "}"
                "return out;")
            page = "" if isinstance(rows, list) and rows else str(
                self._script("return document.body ? document.body.innerText : '';") or "")
        lines = [f"DuckDuckGo: {query}"]
        if isinstance(rows, list) and rows:
            for i, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    continue
                link = unwrap_ddg(str(row.get("url") or ""))
                lines.append(f"\n{i}. {row.get('title') or '(no title)'}\n   {link}")
                if row.get("snippet"):
                    lines.append(f"   {row['snippet']}")
            return "\n".join(lines)
        return "\n".join(lines) + "\n\nNo structured results. Page text:\n" + page[:4000]

    def browse(self, url: str) -> str:
        url = public_http_url(url)
        with self.lock:
            self._open(url)
            final = str(self._script("return String(location.href || '');") or "")
            try:
                public_http_url(final)
            except ValueError:
                self._cmd("WebDriver:Navigate", {"url": "about:blank"})
                self.last_url = ""
                raise
            title = str(self._script("return document.title || '';") or "")
            text = str(self._script("return document.body ? document.body.innerText : '';") or "").strip()
        if len(text) > PAGE_CHARS:
            text = text[:PAGE_CHARS] + (
                f"\n\n[... truncated: the page has more text; the first {PAGE_CHARS:,} characters are shown]")
        return f"{title}\n{final or url}\n\n{text}" if text else f"{title}\n{final or url}\n\n(the page had no text)"

    def close(self):
        with self.lock:
            self._stop()

    def _stop(self):
        sock, proc, profile = self.sock, self.proc, self.profile
        self.sock = self.proc = self.profile = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, 15)
            except (OSError, ProcessLookupError):
                proc.kill()
        if profile:
            shutil.rmtree(profile, ignore_errors=True)

    def _open(self, url: str):
        self._ensure()
        try:
            if url != "about:blank":
                public_http_url(url)
            self._cmd("WebDriver:Navigate", {"url": url})
            self.last_url = url
            self.last_error = ""
        except Exception as e:
            self.last_error = str(e)
            raise

    def _ensure(self):
        if self.running():
            return
        self._stop()
        binary = firefox_bin()
        if not binary:
            raise RuntimeError("firefox is not installed (looked for firefox, firefox-esr, and HYPERCORE_FIREFOX)")
        cache = Path.home() / ".cache" / "hypercore"
        cache.mkdir(parents=True, exist_ok=True)
        self.profile = tempfile.mkdtemp(prefix="firefox-", dir=cache)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        Path(self.profile, "user.js").write_text(
            "\n".join([
                f'user_pref("marionette.port", {port});',
                'user_pref("marionette.enabled", true);',
                'user_pref("browser.shell.checkDefaultBrowser", false);',
                'user_pref("browser.startup.page", 0);',
                'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
                'user_pref("app.update.auto", false);',
                "",
            ]), encoding="utf-8")
        headless = [] if os.environ.get("HYPERCORE_FIREFOX_HEADED") == "1" else ["-headless"]
        self.stderr_tail = ""
        self.proc = subprocess.Popen(
            [binary, *headless, "-marionette", "-no-remote", "-profile", self.profile],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        threading.Thread(target=self._drain, args=(self.proc.stderr,), daemon=True).start()
        deadline = time.monotonic() + 25
        err = "firefox did not open its marionette port"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                err = f"firefox exited ({self.proc.returncode})"
                if self.stderr_tail.strip():
                    err += f": {self.stderr_tail.strip()[-300:]}"
                break
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            self._stop()
            raise RuntimeError(err)
        if self.sock is None:
            self._stop()
            raise RuntimeError(err)
        try:
            hello = read_packet(self.sock, 10)
        except (OSError, EOFError, ValueError, json.JSONDecodeError) as e:
            self._stop()
            raise RuntimeError(f"firefox marionette handshake failed: {e}") from None
        if not isinstance(hello, dict) or hello.get("applicationType") != "gecko":
            self._stop()
            raise RuntimeError(f"firefox marionette handshake was unexpected: {hello!r}"[:300])
        try:
            self._cmd("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {
                "browserName": "firefox", "acceptInsecureCerts": True, "pageLoadStrategy": "normal"}}})
        except Exception:
            self._stop()
            raise
        try:
            self._cmd("WebDriver:SetTimeouts", {"script": 20000, "pageLoad": 25000, "implicit": 0})
        except RuntimeError:
            pass
        self.last_error = ""

    def _drain(self, pipe):
        data = b""
        try:
            for chunk in iter(lambda: pipe.read(256), b""):
                data = (data + chunk)[-500:]
                self.stderr_tail = data.decode("utf-8", "replace")
        except (OSError, ValueError):
            return

    def _script(self, script: str, args=None):
        result = self._cmd("WebDriver:ExecuteScript", {"script": script, "args": args or []})
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result

    def _cmd(self, name: str, params: dict):
        self.msg_id += 1
        mid = self.msg_id
        try:
            self.sock.sendall(encode_packet([0, mid, name, params]))
            reply = read_packet(self.sock, 30)
        except (OSError, EOFError, ValueError, json.JSONDecodeError) as e:
            self._stop()
            raise RuntimeError(f"firefox connection failed during {name}: {e}") from None
        if not isinstance(reply, list) or len(reply) < 4 or reply[1] != mid:
            raise RuntimeError(f"firefox sent an unexpected reply to {name}")
        if reply[2]:
            err = reply[2]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(message or f"{name} failed")
        return reply[3]


BROWSER = Firefox()

TOOLS = [
    {"name": "status",
     "description": "Whether Firefox is running, and the last public page it opened. The Monitor shows this server.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search",
     "description": "Search the public web with DuckDuckGo, opened in Firefox. Returns titles, addresses, and snippets.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "The search words."},
         "limit": {"type": "integer", "description": "How many results to return, 1 to 8. Default 5."}},
         "required": ["query"]}},
    {"name": "browse",
     "description": "Open one public http(s) page in Firefox and return its title and visible text.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The http or https address to open."}},
         "required": ["url"]}},
]


def text(body: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": error}


def call(name: str, args: dict) -> dict:
    try:
        if name == "status":
            return text(json.dumps(BROWSER.status(), indent=1))
        if name == "search":
            return text(BROWSER.search(str(args.get("query") or ""), int(args.get("limit") or SEARCH_DEFAULT)))
        if name == "browse":
            return text(BROWSER.browse(str(args.get("url") or "")))
    except Exception as e:  # noqa: BLE001 - the model reads the failure as a tool result
        BROWSER.last_error = str(e)
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
            "serverInfo": {"name": "web", "title": "Firefox and DuckDuckGo", "version": "0.1"}}}
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
        BROWSER.close()


if __name__ == "__main__":
    stdio()

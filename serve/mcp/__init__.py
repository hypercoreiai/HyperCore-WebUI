"""serve/mcp.py - tools from MCP (Model Context Protocol) servers for the web app's chats.

The user lists servers in `mcp.yaml` at the repo root (or `--mcp-config` pointing at another yaml or at Claude
Desktop's JSON).  The run config's `"mcp_servers"` / `"mcpServers"` block is still read; a name that also appears in
the yaml takes the yaml entry.  Each one is either a program HyperCore starts (`command`, `args`, `env`, `cwd`:
the stdio transport, newline-delimited JSON-RPC 2.0 on its stdin/stdout) or an address (`url`, `headers`: the
Streamable HTTP transport, JSON-RPC POSTed, answered as JSON or as an event stream).  Both are implemented here with
the standard library only; no MCP SDK is needed.

`McpHub` owns every configured server: it starts them in the background when the server starts, offers their tools to
the model as OpenAI function tools named `<server>__<tool>` (two servers may both have a `search`), and routes a call
back to the server that has it.  A server that does not start is left out with one log line; the chat works without
it.  A server that stops later (a crash) is started again on its next call.

Tool results reach the model as text: the MCP content blocks joined, capped at `max_result_chars` (a directory tree or
a web page can be megabytes, far more than a context holds) with a note saying so.  A failed call - the tool's own
error, a crash, a timeout - becomes a result that starts with "error:", so the model can react instead of the chat
breaking.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from serve.winjob import contain

PROTOCOL = "2025-06-18"               # the MCP revision HyperCore asks for; the server's answer is used as given
DEFAULTS = {"timeout_s": 60.0, "max_result_chars": 20000, "max_rounds": 500, "start_timeout_s": 120.0,
          "condense_at": 0.0, "condense_after": 0, "condense_chars": 600,   # condense_at 0: never condense
          "compact_at": 0.0, "compact_retain_bytes": 80_000,               # compact_at 0: never compact
          "tool_allowlist": None}          # None: every tool is offered. A list narrows the tool block.


class McpError(RuntimeError):
    """A server answered with an error, stopped, or could not be reached."""


class McpTimeout(McpError):
    pass


class McpCancelled(McpError):
    """The chat was stopped while the call ran."""


class _Slot:
    """One request waiting for its response."""

    def __init__(self):
        self.done = threading.Event()
        self.msg = None
        self.error = None


def _wait(slot: _Slot, timeout: float, cancel: threading.Event | None, what: str):
    """Wait for a response, checking the chat's stop button and the deadline a few times a second."""
    deadline = time.monotonic() + timeout
    while not slot.done.wait(0.05):
        if cancel is not None and cancel.is_set():
            raise McpCancelled("stopped")
        if time.monotonic() > deadline:
            raise McpTimeout(f"{what} timed out after {timeout:g} s")
    if slot.error:
        raise slot.error
    msg = slot.msg
    if "error" in msg:
        err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
        raise McpError(f"{err.get('message') or 'error'} (code {err.get('code')})")
    return msg.get("result") or {}


# ------------------------------------------------------------------------------------------------ transports
class StdioTransport:
    """A server HyperCore starts: JSON-RPC messages one per line on its stdin and stdout; its stderr is its log."""
    kind = "stdio"

    def __init__(self, name: str, cfg: dict):
        self.name, self.cfg = name, cfg
        self.proc = None
        self.lock = threading.Lock()                    # the pending requests
        self.write_lock = threading.Lock()              # stdin; separate, so a full pipe can't block the reader
        self.pending: dict[int, _Slot] = {}
        self.next_id = 0
        self.stderr: collections.deque[str] = collections.deque(maxlen=40)       # its last log lines, for the error message when it fails
        self.protocol = PROTOCOL

    def start(self):
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        command = str(self.cfg["command"])
        # On Windows `npx`, `uvx` and friends are .cmd files that CreateProcess finds only with their extension
        exe = shutil.which(command, path=env.get("PATH")) or command
        extra = {}
        if os.name == "nt":
            extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            extra["start_new_session"] = True           # its own process group, so close() ends its children too
        try:
            self.proc = subprocess.Popen([exe, *[str(a) for a in self.cfg.get("args") or []]], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cfg.get("cwd") or None,
                                         env=env, **extra)
        except OSError as e:
            raise McpError(f"could not start {command!r}: {e}") from None
        contain(self.proc)                              # ends with the server, however it ends (Windows)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not getattr(self, "ended", False)

    @staticmethod
    def _lines(f):
        """A pipe's lines until it closes (or close() closed it under us)."""
        try:
            yield from f
        except (OSError, ValueError):
            return

    def _read_stderr(self):
        for raw in self._lines(self.proc.stderr):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr.append(line)
                if os.environ.get("HYPERCORE_DEBUG"):
                    print(f"[mcp {self.name}] {line}", flush=True)

    def _read(self):
        for raw in self._lines(self.proc.stdout):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:                           # a server that prints to stdout; the spec forbids it
                self.stderr.append(line)
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                if isinstance(m, dict):
                    self._dispatch(m)
        self.ended = True
        code = self.proc.poll()
        err = McpError(f"the server stopped{f' (exit code {code})' if code is not None else ''}{self._tail()}")
        with self.lock:
            slots, self.pending = list(self.pending.values()), {}
        for s in slots:
            s.error = err
            s.done.set()

    def _tail(self) -> str:
        return f": {self.stderr[-1][:300]}" if self.stderr else ""

    def _dispatch(self, m: dict):
        if "method" in m:
            if "id" in m:                                # a request from the server: we offer no client features
                try:
                    if m["method"] == "ping":
                        self._send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
                    else:
                        self._send({"jsonrpc": "2.0", "id": m["id"],
                                    "error": {"code": -32601, "message": f"method not supported: {m['method']}"}})
                except McpError:
                    pass
            return                                       # notifications (log messages, list changes): not used
        rid = m.get("id")
        with self.lock:
            slot = self.pending.pop(rid, None) if isinstance(rid, int) else None
        if slot is not None:                             # an answer to a call that timed out is simply dropped
            slot.msg = m
            slot.done.set()

    def _send(self, msg: dict):
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            if self.proc is None or self.proc.stdin is None:
                raise McpError(f"the server stopped{self._tail()}")
            with self.write_lock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            raise McpError(f"the server stopped{self._tail()}") from None

    def request(self, method: str, params: dict, timeout: float, cancel=None):
        if not self.alive():
            raise McpError(f"the server is not running{self._tail()}")
        slot = _Slot()
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            self.pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return _wait(slot, timeout, cancel, method)
        except (McpTimeout, McpCancelled) as e:
            with self.lock:
                self.pending.pop(rid, None)
            try:                                         # tell the server to stop working on it
                self.notify("notifications/cancelled", {"requestId": rid, "reason": str(e)})
            except McpError:
                pass
            raise

    def notify(self, method: str, params: dict | None = None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def close(self):
        """As the spec says: close its stdin, give it a moment to exit, then end it (and what it started)."""
        p = self.proc
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._kill_tree(p)
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except OSError:
                pass

    @staticmethod
    def _kill_tree(p):
        try:
            if os.name == "nt":                          # npx.cmd -> node: end the whole tree
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, check=False)
            else:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            p.kill()
        except OSError:
            pass


class HttpTransport:
    """A server at an address (Streamable HTTP): every message is a POST; the answer is JSON or an event stream that
    carries it.  The session id the server hands out at `initialize` rides on every later request."""
    kind = "http"

    def __init__(self, name: str, cfg: dict):
        self.name, self.url = name, str(cfg["url"])
        self.headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.session = None
        self.protocol = None                             # sent as MCP-Protocol-Version once negotiated
        self.next_id = 0
        self.lock = threading.Lock()
        self.broken = None

    def start(self):
        pass

    def alive(self) -> bool:
        return self.broken is None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "hypercore", **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _post(self, msg: dict, rid, timeout: float, slot: _Slot | None):
        try:
            req = urllib.request.Request(self.url, data=json.dumps(msg, ensure_ascii=False).encode("utf-8"),
                                         headers=self._headers(), method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.headers.get("Mcp-Session-Id"):
                    self.session = r.headers["Mcp-Session-Id"]
                if slot is None:                         # a notification: 202, nothing to read
                    return
                ctype = r.headers.get("Content-Type", "")
                if "text/event-stream" in ctype:
                    found = self._from_events(r, rid)
                else:
                    body = r.read()
                    found = None
                    parsed = json.loads(body) if body.strip() else None
                    for m in parsed if isinstance(parsed, list) else [parsed]:
                        if isinstance(m, dict) and m.get("id") == rid and "method" not in m:
                            found = m
                if found is None:
                    raise McpError("the server's answer did not contain a response")
                slot.msg = found
        except urllib.error.HTTPError as e:
            detail = ""
            with e:                                    # the body is a socket: read it and close it
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except OSError:
                    pass
            if e.code == 404 and self.session:           # the session expired: a restart starts a new one
                self.broken = McpError("the server ended the session")
            err = McpError(f"HTTP {e.code} {e.reason}{': ' + detail if detail else ''}")
            if slot is None:
                raise err from None
            slot.error = err
        except (OSError, ValueError, McpError) as e:
            err = e if isinstance(e, McpError) else McpError(f"could not reach {self.url}: {e}")
            if slot is None:
                raise err from None
            slot.error = err
        finally:
            if slot is not None:
                slot.done.set()

    @staticmethod
    def _from_events(r, rid):
        """Read the event stream until the event that answers `rid` (it may carry notifications first)."""
        data = []
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                try:
                    m = json.loads("\n".join(data))
                except ValueError:
                    m = None
                data = []
                for x in m if isinstance(m, list) else [m]:
                    if isinstance(x, dict) and x.get("id") == rid and "method" not in x:
                        return x
        return None

    def request(self, method: str, params: dict, timeout: float, cancel=None):
        if self.broken:
            raise self.broken
        with self.lock:
            self.next_id += 1
            rid = self.next_id
        slot = _Slot()
        # the POST runs on its own thread so the stop button and the deadline are noticed while it waits
        threading.Thread(target=self._post, args=({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                                                  rid, timeout + 5, slot), daemon=True).start()
        try:
            return _wait(slot, timeout, cancel, method)
        except (McpTimeout, McpCancelled) as e:
            threading.Thread(target=self._quiet_notify, args=("notifications/cancelled",
                                                              {"requestId": rid, "reason": str(e)}), daemon=True).start()
            raise

    def _quiet_notify(self, method, params):
        try:
            self.notify(method, params)
        except McpError:
            pass

    def notify(self, method: str, params: dict | None = None):
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})},
                   None, 10, None)

    def close(self):
        if not self.session:
            return
        try:                                             # end the session on the server (it may not allow that)
            req = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
            urllib.request.urlopen(req, timeout=3).close()
        except (OSError, ValueError):
            pass


# ------------------------------------------------------------------------------------------------ one server
class McpServer:
    """One configured server: started once (and again after it stopped), with the tools it listed."""

    def __init__(self, name: str, cfg: dict, settings: dict):
        self.name, self.cfg, self.settings = name, cfg, settings
        self.kind = "http" if cfg.get("url") else "stdio"
        self.status = "idle"             # idle -> starting -> ready | failed; ready -> stopped when it ends
        self.enabled = True              # the page can turn one server off without reloading the model
        self.error: str | None = None
        self.tools: list[dict] = []
        self.info: dict = {}
        self.transport: StdioTransport | HttpTransport | None = None
        self.lock = threading.Lock()
        self.last_start = 0.0

    def start(self) -> bool:
        with self.lock:
            return self._start()

    def arm(self) -> None:
        """Mark the server wanted. The caller starts it; this does not load the model."""
        with self.lock:
            self.enabled = True
            self.error = None
            if self.status != "ready":
                self.status = "starting"

    def disarm(self) -> None:
        """Stop this server's process and leave it out of the tool list until arm()."""
        with self.lock:
            self.enabled = False
            if self.transport is not None:
                self.transport.close()
            self.status = "disabled"

    def _start(self) -> bool:
        if not self.enabled:
            self.status = "disabled"
            return False
        if self.transport is not None:
            self.transport.close()
        self.status, self.error, self.last_start = "starting", None, time.monotonic()
        t = HttpTransport(self.name, self.cfg) if self.kind == "http" else StdioTransport(self.name, self.cfg)
        self.transport = t
        timeout = float(self.settings["start_timeout_s"])
        try:
            t.start()
            res = t.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                           "clientInfo": {"name": "hypercore", "title": "HyperCore", "version": "1"}},
                            timeout)
            t.protocol = str(res.get("protocolVersion") or PROTOCOL)
            self.info = {k: v for k, v in (res.get("serverInfo") or {}).items() if k in ("name", "title", "version")}
            self.info["protocol"] = t.protocol
            t.notify("notifications/initialized")
            tools: list = []
            cursor = None
            for _ in range(100):                         # pages; a server that never stops paging is cut off
                try:
                    page = t.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
                except McpError as e:
                    if "code -32601" in str(e) and not tools:   # a server without tools (prompts/resources only)
                        break
                    raise
                tools += [x for x in page.get("tools") or [] if isinstance(x, dict) and x.get("name")]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            if not self.enabled:                 # turned off while this start held the lock: do not stay up
                t.close()
                self.status = "disabled"
                return False
            self.tools, self.status = tools, "ready"
            names = ", ".join(x["name"] for x in tools[:8]) + (", ..." if len(tools) > 8 else "")
            print(f"[hypercore] MCP server {self.name!r}: {len(tools)} tool{'s' * (len(tools) != 1)}"
                  f"{' (' + names + ')' if tools else ''}", flush=True)
            return True
        except McpError as e:
            self.status, self.error, self.tools = "failed", str(e), []
            t.close()
            print(f"[hypercore] MCP server {self.name!r} did not start: {e}. Its tools are left out; the chat works "
                  "without them.", flush=True)
            return False

    def call(self, tool: str, arguments: dict, timeout: float, cancel=None) -> dict:
        with self.lock:
            if not self.enabled:
                raise McpError(f"the server {self.name!r} is turned off")
            if self.transport is None or not self.transport.alive():
                # it stopped since its last call (a crash, an expired session): start it again; one that failed to
                # start is tried again at most every 10 s, so a broken command does not run on every call
                if self.status == "failed" and time.monotonic() - self.last_start < 10:
                    raise McpError(f"the server is not running ({self.error or 'it stopped'})")
                print(f"[hypercore] MCP server {self.name!r} had stopped; starting it again", flush=True)
                if not self._start():
                    raise McpError(f"the server could not be started again: {self.error}")
            t = self.transport
        if t is None:
            raise McpError(f"the server {self.name!r} is not running")
        try:
            return t.request("tools/call", {"name": tool, "arguments": arguments}, timeout, cancel)
        except McpError as e:
            if not t.alive():
                self.status, self.error = "stopped", str(e)
            raise

    def close(self):
        with self.lock:
            if self.transport is not None:
                self.transport.close()
            if self.status in ("ready", "starting"):
                self.status = "stopped"


# ------------------------------------------------------------------------------------------------ the hub
def result_text(result: dict) -> str:
    """An MCP tool result as the text the model reads: the text blocks as they are, other blocks named."""
    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            res = block.get("resource") or {}
            parts.append(str(res["text"]) if "text" in res else f"[resource {res.get('uri', '')} "
                                                                  f"({res.get('mimeType', 'binary')}), not shown]")
        elif kind == "resource_link":
            parts.append(f"[resource link: {block.get('name') or ''} {block.get('uri', '')}]".replace("  ", " "))
        elif kind in ("image", "audio"):
            size = len(block.get("data") or "") * 3 // 4
            parts.append(f"[{kind} ({block.get('mimeType', '?')}, {size:,} bytes): not shown to the model]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)


CONDENSED_MARK = "condensed by HyperCore"
COMPACT_MARK = "compacted by HyperCore"
# Appended to the transcript for one extra generation. The reply replaces the
# older turns; it is not itself kept as a user message.
COMPACT_PROMPT = (
    "You are compacting this conversation into a handoff for another agent that will resume the work.\n"
    "\n"
    "Include:\n"
    "- Goal: what the user is ultimately trying to achieve\n"
    "- State: progress so far, current blockers, and decisions made\n"
    "- Context: constraints, preferences, and critical data, examples, and references needed to continue\n"
    "- Next: the specific steps that remain\n"
    "- Pitfalls: anything tried that did not work\n"
    "\n"
    "Write it so the next agent can act without re-asking the user. Be concise and well-structured.\n"
    "Do not call tools.\n"
)


def cap_result(text: str, cap: int, hint: str = "") -> str:
    """Keep the beginning and the end of a result that exceeds cap.

    A head-only cut lands inside a JSON result and drops stderr, which is at
    the end. The two kept parts together are cap characters; the note between
    them says how much of the middle was left out.
    """
    if cap <= 0 or len(text) <= cap:
        return text
    head_len = cap // 2
    tail_len = cap - head_len
    omitted = len(text) - head_len - tail_len
    note = (f"\n\n[... truncated: the tool returned {len(text):,} characters; {omitted:,} "
            f"in the middle are not shown. The beginning and the end are kept]" + hint)
    return text[:head_len] + note + text[-tail_len:]


def condense(text: str, keep: int) -> str:
    """Shorten a tool result the model has already read, before it is re-prefilled.

    The head and the tail stay verbatim - a result usually begins with what matters
    (paths, headers, the first matches) and ends with its own truncation note - and the
    middle becomes a count. Deterministic, no model call; a result that was already
    condensed is left alone.
    """
    if keep <= 0 or len(text) <= keep or CONDENSED_MARK in text:
        return text
    head, tail = text[: keep // 2], text[-(keep - keep // 2):]
    return (f"{head}\n[... {len(text) - len(head) - len(tail):,} characters of this result omitted "
            f"({CONDENSED_MARK}); its beginning and end are kept]\n{tail}")


def message_text(message: dict) -> str:
    """The text a message contributes. Image parts are skipped: a handoff cannot carry them."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text"):
                parts.append(str(part.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def is_handoff(message: dict) -> bool:
    return COMPACT_MARK in message_text(message)


def _chars(messages: list) -> int:
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        total += len(message_text(message))
        total += len(json.dumps(message.get("tool_calls") or [], ensure_ascii=False))
    return total


def _shrink_text(message: dict, cap: int) -> dict:
    """Head and tail of one oversized string. Non-strings (image arrays) are left as they are."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or cap <= 0 or len(content) <= cap:
        return message
    return {**message, "content": cap_result(content, cap)}


def retained_user_messages(messages: list, byte_budget: int) -> list[dict]:
    """The newest user texts that fit in byte_budget, oldest first.

    A handoff is not a user request, so it is not kept here. The message that
    does not fit is cut at the end: the start of that request is what remains.
    """
    if byte_budget <= 0:
        return []
    kept: list[dict] = []
    remaining = byte_budget
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user" or is_handoff(message):
            continue
        text = message_text(message).strip()
        if not text:
            continue
        if len(text) <= remaining:
            kept.append({"role": "user", "content": text})
            remaining -= len(text)
            continue
        if remaining > 0:
            kept.append({"role": "user", "content": text[:remaining]})
        break
    kept.reverse()
    return kept


def deterministic_summary(messages: list) -> str:
    """A handoff built from the transcript itself, with no model call.

    Used when the prompt is already too large to summarize with the model.
    One line from each user message, the last paragraph of each answer, and
    the opening of each tool result.
    """
    lines = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = message_text(message).strip()
        if role in ("system", "developer") or not text and role != "assistant":
            continue                        # a profile turn is a rule, not conversation to summarise
        if role == "user":
            if is_handoff(message):
                lines.append("Earlier handoff: " + " ".join(text.split())[:400])
            else:
                first = text.split("\n", 1)[0][:300]
                if first:
                    lines.append(f"User: {first}")
        elif role == "assistant":
            names = []
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                names.append(str(fn.get("name") or call.get("name") or "tool"))
            if names:
                lines.append("Called: " + ", ".join(names))
            paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
            if paragraphs:
                lines.append("Assistant: " + paragraphs[-1][:400])
        elif role == "tool" and text:
            lines.append("Tool result: " + " ".join(text.split())[:200])
    body = "\n".join(lines).strip() or "No earlier conversation."
    if len(body) > 6000:
        body = body[:3000] + "\n...\n" + body[-2500:]
    return body


def _tool_ids(messages: list) -> list[str]:
    found = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                found.append(str(call["id"]))
    return found


def shrink_messages(messages: list, cap: int) -> list | None:
    """Head-and-tail any string that exceeds cap. None when nothing was that long.

    This is the last resort for one message that is larger than the window by itself,
    where there is no older turn to fold into a handoff.
    """
    if cap <= 0:
        return None
    out, changed = [], False
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        nxt = _shrink_text(message, cap)
        changed = changed or nxt is not message
        out.append(nxt)
    return out if changed else None


def plan_compaction(messages: list, summary: str, retain_bytes: int, keep_round: bool = True) -> dict | None:
    """Replace the older part of a transcript with one handoff.

    Every system/developer turn stays, hoisted to the top: the chat template merges them into
    one system block, so a profile added mid-transcript is a rule, not history. The newest user message stays, cut only
    when it alone exceeds retain_bytes. With keep_round, the trailing tool
    round stays too, because that is the result the model is about to answer.
    Everything else is represented by `summary`. None when that would not
    make the transcript shorter.
    """
    systems: list = []
    rest: list = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") in ("system", "developer"):
            systems.append(message)          # never a candidate for the handoff
        else:
            rest.append(message)
    last_user = None
    for index in range(len(rest) - 1, -1, -1):
        if rest[index].get("role") == "user" and not is_handoff(rest[index]):
            last_user = index
            break
    round_at = len(rest)
    if keep_round:
        index = len(rest)
        while index > 0 and rest[index - 1].get("role") == "tool":
            index -= 1
        if index > 0 and rest[index - 1].get("role") == "assistant" and rest[index - 1].get("tool_calls"):
            round_at = index - 1
    tail_at: set[int] = set()
    tail: list = []
    if last_user is not None:
        tail.append(_shrink_text(rest[last_user], retain_bytes))
        tail_at.add(last_user)
    if keep_round and round_at < len(rest):
        for index in range(round_at, len(rest)):
            if index in tail_at:
                continue
            tail.append(_shrink_text(rest[index], retain_bytes))
            tail_at.add(index)
    prefix = [message for index, message in enumerate(rest) if index not in tail_at]
    if not any(message_text(message).strip() or message.get("tool_calls") for message in prefix):
        return None
    summary = (summary or "").strip() or "No earlier conversation."
    handoff = {"role": "user", "content":
               "The previous conversation was compacted. Use this summary as context:\n\n"
               f"{summary}\n\n[{COMPACT_MARK}]"}
    retained = retained_user_messages(prefix, retain_bytes)
    server = systems + retained + [handoff] + tail
    if _chars(server) + 200 >= _chars(systems + rest):
        return None
    return {"messages": server, "prefix": retained + [handoff], "omit_tool_ids": _tool_ids(prefix),
            "summary": summary}


def _continuation_hint(server: McpServer | None, tool: str) -> str:
    """For tools that can read a slice, say how to get the rest of a truncated result.
    The hub cannot re-fetch, so the pointer has to be the model's next call."""
    name = server.name if server else ""
    if (name, tool) in {("docs", "read_file"), ("home", "read")}:
        return (" This tool takes start_line/end_line: read the next range instead of repeating "
               "the same call.")
    return ""


def _clean(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name)) or "x"


# ---------------------------------------------------------------- Tier 1 merges
# Tool pairs that do the same job through two engines or two indexes. The hub offers one tool
# instead of two and routes the call to the server that should answer it.
#
# A merge is in effect only when every tool it folds up is present: a server that failed to
# start still offers its own tool, so a merge never hides a capability that is not there.
# The names the model sees for a merged tool are bare (`browse`, not `web__browse`) because
# the tool belongs to the hub, not to one server.
MERGES: tuple[dict, ...] = (
    {"name": "browse",
     "description": ("Open one public http(s) page and return its title and visible text. engine: plain is the "
                     "Firefox the web server drives (fast); stealth is the patched anti-bot browser, for pages a "
                     "bot challenge would block."),
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The http or https address to open."},
         "engine": {"type": "string", "enum": ["plain", "stealth"], "description": "Default plain."},
         "max_chars": {"type": "integer", "description": "Page text limit, only used by the stealth engine."}},
         "required": ["url"]},
     "parts": (("web", "browse"), ("browser", "fetch")),
     "route": lambda a: [("browser", "fetch", {"url": str(a.get("url") or ""),
                                               **({"max_chars": int(a["max_chars"])} if a.get("max_chars") else {})})
                         if str(a.get("engine") or "").lower() == "stealth"
                         else ("web", "browse", {"url": str(a.get("url") or "")})]},
    {"name": "status",
     "description": "Whether either browser is up, and the last page each one opened.",
     "parameters": {"type": "object", "properties": {}},
     "parts": (("web", "status"), ("browser", "status")),
     "route": lambda a: [("web", "status", {}), ("browser", "status", {})],
     "join": lambda parts: "\n\n".join(parts)},
    {"name": "ls",
     "description": ("List what is under a path: the document index view by default; all_files: true lists every "
                     "file the document sandbox allows, at the same depth."),
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A directory inside the workspace. Default its root."},
         "depth": {"type": "integer", "description": "How many levels to show, 1 to 4. Default 2."},
         "all_files": {"type": "boolean", "description": "List every file, not only documents. Default false."}}},
     "parts": (("home", "structure"), ("docs", "list_directory")),
     "route": lambda a: [("docs", "list_directory",
                          {"path": str(a.get("path") or "."), "depth": int(a.get("depth") or 1)})
                         if a.get("all_files") else
                         ("home", "structure", {"path": str(a.get("path") or "/home"),
                                               "depth": int(a.get("depth") or 2)})]},
)


def _folded(m: dict) -> set[str]:
    return {f"{_clean(s)}__{_clean(t)}" for s, t in m["parts"]}


class McpHub:
    """Every configured server, and the merged, namespaced tool list the model sees."""

    def __init__(self, servers: dict[str, dict], settings: dict | None = None):
        self.settings = {**DEFAULTS, **(settings or {})}
        self.servers = {name: McpServer(name, cfg, self.settings) for name, cfg in servers.items()}
        self.threads: list[threading.Thread] = []
        self._routes: dict[str, tuple[McpServer | None, str]] = {}
        self.collisions: set[str] = set()
        self.usage: dict[str, int] = {"calls": 0, "chars": 0, "dropped_chars": 0, "truncated": 0, "errors": 0}

    def start(self, wait: bool = False):
        """Start every server on its own thread (npx may download a package first: that must not delay the chat)."""
        self.threads = [threading.Thread(target=s.start, daemon=True, name=f"mcp-{s.name}")
                        for s in self.servers.values()]
        for t in self.threads:
            t.start()
        if wait:
            self.wait(None)

    def needs_condensing(self, prompt_tokens: int, max_context: int, rounds: int) -> bool:
        """True when the prompt about to be re-prefilled is close to the context.

        The trigger is the prompt's own size, not the number of rounds: firing on a round
        count pays a full re-prefill for headroom that was never needed (the audit in the
        chat archive measured five condensings that took a 132k prompt down to 95k while
        the window was only half full). `condense_after` is only a floor - the first rounds
        are small and the model is still reading them.
        """
        at = float(self.settings["condense_at"])
        if not at or not max_context:
            return False
        return rounds >= int(self.settings["condense_after"]) and prompt_tokens > at * int(max_context)

    def needs_compacting(self, prompt_tokens: int, max_context: int) -> bool:
        """True when the prompt about to be read is close enough to the window that the next
        tool round would be refused. `compact_at` 0 turns this off. Condensing still runs first:
        it is the cheap step. Compaction is the one that folds the older turns into a handoff.
        """
        at = float(self.settings.get("compact_at") or 0)
        if not at or not max_context or prompt_tokens <= 0:
            return False
        return prompt_tokens > at * int(max_context)

    def condense_messages(self, messages: list) -> tuple[list, int, int]:
        """Shorten the tool results already in the conversation before the next prefill.

        Returns (messages, results shortened, characters dropped). Every tool result from
        the current round is left whole: those are the ones the model is reacting to, and
        one round can contain several. The list comes back unchanged when nothing qualified.
        """
        keep = int(self.settings["condense_chars"])
        if keep <= 0:
            return messages, 0, 0
        # A trailing run of tool messages is the round just appended. When the list
        # does not end on a tool result, the single newest one is spared instead.
        spare_from = len(messages)
        i = len(messages) - 1
        while i >= 0 and isinstance(messages[i], dict) and messages[i].get("role") == "tool":
            spare_from = i
            i -= 1
        if spare_from == len(messages):
            spare_from = max((j for j, m in enumerate(messages)
                              if isinstance(m, dict) and m.get("role") == "tool"),
                             default=len(messages))
        out, dropped, count = list(messages), 0, 0
        for i, m in enumerate(out):
            if i >= spare_from or not isinstance(m, dict) or m.get("role") != "tool":
                continue
            text = m.get("content")
            if not isinstance(text, str):
                continue
            new = condense(text, keep)
            if new != text:
                dropped += len(text) - len(new)
                count += 1
                out[i] = {**m, "content": new}
        if count:
            self.usage["condensed_results"] = self.usage.get("condensed_results", 0) + count
            self.usage["condensed_chars"] = self.usage.get("condensed_chars", 0) + dropped
        return out, count, dropped

    def wait(self, timeout: float | None) -> None:
        """Wait (up to `timeout` s in all) for servers that are still starting."""
        end = None if timeout is None else time.monotonic() + timeout
        for t in self.threads:
            t.join(None if end is None else max(0.0, end - time.monotonic()))

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Turn one server on or off. Off stops its process. On starts it again in the background.
        Neither path reloads the model. The choice lives in this process; a HyperCore restart follows mcp.yaml.
        """
        server = self.servers.get(name)
        if server is None:
            raise KeyError(name)
        if enabled:
            if not (server.enabled and server.status in ("ready", "starting")):
                server.arm()
                threading.Thread(target=server.start, daemon=True, name=f"mcp-on-{server.name}").start()
        else:
            server.disarm()
        self.routes()

    def routes(self) -> dict[str, tuple[McpServer | None, str]]:
        """tool name the model sees -> (server, the tool's own name).  A server that stopped after it was ready keeps
        its tools here: a call starts it again. One the page turned off is left out, and a call does not start it."""
        out: dict[str, tuple[McpServer | None, str]] = {}
        for s in self.servers.values():
            if not s.enabled or s.status not in ("ready", "stopped"):
                continue
            for t in s.tools:
                name = f"{_clean(s.name)}__{_clean(t['name'])}"[:64]
                n = 2
                while name in out:                       # two names that differ only in cleaned characters
                    if n == 2:
                        self.collisions.add(name)
                    name = f"{name[:60]}_{n}"
                    n += 1
                out[name] = (s, t["name"])
        self._routes = out
        return out

    def _merge_active(self, m: dict) -> bool:
        """True when every tool a merge folds up is offered by a server that is here."""
        for server_name, tool in m["parts"]:
            s = self.servers.get(server_name)
            if s is None or not s.enabled or s.status not in ("ready", "stopped"):
                return False
            if not any(t.get("name") == tool for t in s.tools):
                return False
        return True

    def _merge_for(self, name: str) -> dict | None:
        for m in MERGES:
            if _clean(m["name"]) == name and self._merge_active(m):
                return m
        return None

    def surface(self) -> dict[str, dict]:
        """name -> {name, description, parameters, server, tool}: the tools the model sees, merges applied.
        A merged tool has server None and lists the names it folded up."""
        active = [m for m in MERGES if self._merge_active(m)]
        folded = {n for m in active for n in _folded(m)}
        out: dict[str, dict] = {}
        for name, (s, tool) in self.routes().items():
            if name in folded or s is None:
                continue
            t = next(x for x in s.tools if x["name"] == tool)
            schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
            out[name] = {"name": name, "description": str(t.get("description") or t.get("title") or ""),
                         "parameters": schema or {"type": "object", "properties": {}},
                         "server": s.name, "tool": tool}
        for m in active:
            name = _clean(m["name"])
            out[name] = {"name": name, "description": m["description"], "parameters": m["parameters"],
                        "server": None, "tool": name, "merged": sorted(_folded(m))}
        return out

    def allowed_names(self) -> set[str] | None:
        """The hub-level tool_allowlist as surface names, or None when every tool is offered.

        An entry is a tool name as the model sees it ("web__search", or the name a merge folds a
        server's tools into), the bare tool a server reports ("search"), or a server name, which
        stands for all of that server's tools. Names that match nothing are ignored: a tool that is
        not installed today should not stop the chat from starting.
        """
        raw = self.settings.get("tool_allowlist")
        if not raw:
            return None
        surface = self.surface()
        servers = {s.name for s in self.servers.values()}
        # A bare tool name is what a person writes in mcp.yaml. It is unambiguous only when one
        # server reports it; two servers with the same tool name (both have a `status`) are both kept.
        by_tool: dict[str, list[str]] = {}
        for name, tool in surface.items():
            by_tool.setdefault(_clean(str(tool.get("tool") or "")), []).append(name)
        out: set[str] = set()
        for name in raw:
            key = _clean(str(name))
            if key in surface:
                out.add(key)
            elif key in servers:
                out.update(n for n, t in surface.items() if t.get("server") == key)
            elif key in by_tool:
                out.update(by_tool[key])
        return out

    def template_tools(self, exclude=()) -> list[dict]:
        """The tools in the chat template's form ({name, description, parameters}); `exclude`: names the request
        brought itself (those win). A tool_allowlist narrows what is offered at all: the tool block is fixed
        prompt overhead the page cannot trim, so it is the hub that decides its size."""
        allowed = self.allowed_names()
        return [{k: t[k] for k in ("name", "description", "parameters")}
                for name, t in self.surface().items()
                if name not in exclude and (allowed is None or name in allowed)]

    def openai_tools(self) -> list[dict]:
        return [{"type": "function", "function": t} for t in self.template_tools()]

    def _run(self, server: McpServer | None, tool: str, arguments: dict,
             cancel: threading.Event | None) -> tuple[str, bool]:
        """One call to one server, errors as text the model can read."""
        if server is None:
            return f"error: there is no tool named {tool!r}", False
        try:
            res = server.call(tool, arguments, float(self.settings["timeout_s"]), cancel)
            text, ok = result_text(res), not res.get("isError")
            if not ok:
                return "error: " + (text or "the tool reported an error"), False
            return text, True
        except McpCancelled:
            raise
        except McpError as e:
            return f"error: {e}", False

    def call(self, name: str, arguments: dict, cancel: threading.Event | None = None) -> dict:
        """Run one tool -> {"ok", "text" (what the model reads, capped), "chars" (its full length), "truncated",
        "ms", "server", "tool"}.  Errors become text starting with "error:"; only McpCancelled is raised.
        A merged name (MERGES) runs its parts and joins the results as one answer."""
        t0 = time.monotonic()
        args = arguments if isinstance(arguments, dict) else {}
        merged = self._merge_for(name)
        if merged is not None:
            parts = merged["route"](args)
            results = [self._run(self.servers.get(s), tool, a, cancel) for s, tool, a in parts]
            text = "\n\n".join(t for t, _ in results) if len(results) > 1 else results[0][0]
            ok = all(o for _, o in results)
            if len(parts) == 1:
                server, tool = self.servers.get(parts[0][0]), parts[0][1]
            else:
                server, tool = None, merged["name"]
        else:
            route = self._routes.get(name) or self.routes().get(name) or (None, name)
            server, tool = route
            text, ok = self._run(server, tool, args, cancel)
        out: dict[str, object] = {"server": server.name if server else None, "tool": tool}
        cap = int(self.settings["max_result_chars"])
        full = len(text)
        if cap > 0 and full > cap:
            text = cap_result(text, cap, _continuation_hint(server, tool))
        self.usage["calls"] += 1
        self.usage["chars"] += full
        if not ok:
            self.usage["errors"] += 1
        if cap > 0 and full > cap:
            self.usage["truncated"] += 1
            self.usage["dropped_chars"] += full - cap
        out.update(ok=ok, text=text, chars=full, truncated=cap > 0 and full > cap,
                   tokens_est=full // 4,
                   ms=int((time.monotonic() - t0) * 1000))
        return out

    def status(self) -> dict:
        """GET /mcp: every server, its state and its tools (for the web app). A tool that a merge folded up is
        still listed under its server, marked with the tool that replaced it."""
        surface = self.surface()
        active = [m for m in MERGES if self._merge_active(m)]
        folded = {n: m["name"] for m in active for n in _folded(m)}
        servers = []
        for s in self.servers.values():
            names = {tool: n for n, (srv, tool) in self.routes().items() if srv is s}
            tools = []
            for t in s.tools:
                name = names.get(t["name"], t["name"])
                entry = {"name": name, "tool": t["name"],
                         "description": str(t.get("description") or "")[:300]}
                if name in folded:
                    entry["folded_into"] = _clean(folded[name])
                tools.append(entry)
            servers.append({"name": s.name, "transport": s.kind, "status": s.status, "enabled": s.enabled,
                            "error": s.error, "info": s.info, "tools": tools})
        allowed = self.allowed_names()
        offered = sorted(t["name"] for t in self.template_tools())
        return {"servers": servers, "tools": len(offered),
                "offered": offered,
                "hidden": sorted(n for n in surface if n not in (allowed or set())) if allowed else [],
                "merged": sorted(_clean(m["name"]) for m in active),
                "collisions": sorted(self.collisions),
                "usage": {**self.usage, "tokens_est": self.usage["chars"] // 4},
                "settings": {k: self.settings[k] for k in ("timeout_s", "max_result_chars", "max_rounds",
                                         "condense_at", "condense_after", "condense_chars",
                                         "compact_at", "compact_retain_bytes")},
                "tool_allowlist": sorted(allowed) if allowed is not None else None}

    def close(self):
        for s in self.servers.values():
            try:
                s.close()
            except Exception:  # noqa: BLE001, S110 - closing is best effort
                pass


# ------------------------------------------------------------------------------------------------ config
def _expand_path(value):
    """`~`, `$HOME` and `${HOME}` in a server entry resolve to the running user's home, so one
    mcp.yaml works for every profile on the machine. `$HOME` only matches when the name ends
    there, so `$HOME_SUFFIX` is left alone."""
    if not isinstance(value, str) or not value:
        return value
    home = os.environ.get("HOME") or str(Path.home())
    text = re.sub(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])", home, value)
    if text.startswith("~"):
        text = home + text[1:]
    return text


def _expand_paths(cfg: dict) -> dict:
    for key in ("command", "cwd", "url"):
        if isinstance(cfg.get(key), str):
            cfg[key] = _expand_path(cfg[key])
    if isinstance(cfg.get("args"), list):
        cfg["args"] = [_expand_path(a) for a in cfg["args"]]
    for key in ("env", "headers"):
        if isinstance(cfg.get(key), dict):
            cfg[key] = {k: _expand_path(v) for k, v in cfg[key].items()}
    return cfg


def _check_server(name, cfg, where) -> dict:
    if not isinstance(cfg, dict):
        raise SystemExit(f"[hypercore] {where}: MCP server {name!r} must be an object with \"command\" or \"url\"")
    if cfg.get("url"):
        if cfg.get("type") == "sse":
            raise SystemExit(f"[hypercore] {where}: MCP server {name!r} uses the old SSE transport (\"type\": \"sse\"); "
                             "HyperCore speaks Streamable HTTP - most servers offer it at /mcp")
        if not isinstance(cfg.get("headers") or {}, dict):
            raise SystemExit(f"[hypercore] {where}: MCP server {name!r}: \"headers\" must be an object")
        # an address is a path too: `url: $HOME/api/mcp` must resolve like `cwd` or `args` do
        return _expand_paths(cfg)
    if not isinstance(cfg.get("command"), str) or not cfg["command"].strip():
        raise SystemExit(f"[hypercore] {where}: MCP server {name!r} needs \"command\" (a program to start) or \"url\"")
    if not isinstance(cfg.get("args") or [], list):
        raise SystemExit(f"[hypercore] {where}: MCP server {name!r}: \"args\" must be a list - one argument per line "
                         "after \"args:\", each starting with '- ' (the JSON form ['a', 'b'] is not read)")
    if not isinstance(cfg.get("env") or {}, dict):
        raise SystemExit(f"[hypercore] {where}: MCP server {name!r}: \"env\" must be an object")
    return _expand_paths(cfg)


def servers_from(block, where: str) -> dict[str, dict]:
    """{"name": {...}} with the entries checked; `"disabled": true` leaves one out."""
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise SystemExit(f"[hypercore] {where}: the MCP servers must be an object {{\"name\": {{...}}}}")
    return {str(n): _check_server(n, c, where) for n, c in block.items()
            if not (isinstance(c, dict) and c.get("disabled") is True)}


def settings_from(cfg: dict) -> dict:
    """The run config's optional `"mcp"` block: timeout_s, max_result_chars, max_rounds, start_timeout_s."""
    out = {}
    for key, value in (cfg.get("mcp") or {}).items():
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if key in ("timeout_s", "start_timeout_s"):
            if not number or value <= 0:
                raise SystemExit(f"[hypercore] config mcp.{key}={value!r}: expected a number of seconds > 0")
            out[key] = float(value)
        elif key in ("max_result_chars", "max_rounds", "condense_chars", "compact_retain_bytes"):
            if not number or value != int(value) or value < 1:
                raise SystemExit(f"[hypercore] config mcp.{key}={value!r}: expected a positive integer")
            out[key] = int(value)
        elif key == "condense_after":
            if not number or value != int(value) or value < 0:
                raise SystemExit(f"[hypercore] config mcp.{key}={value!r}: expected an integer number of rounds >= 0")
            out[key] = int(value)
        elif key in ("condense_at", "compact_at"):
            if not number or not 0.0 <= float(value) <= 1.0:
                raise SystemExit(f"[hypercore] config mcp.{key}={value!r}: expected a fraction of the context "
                                 "between 0 (never) and 1")
            out[key] = float(value)
        elif key == "tool_allowlist":
            if value is None:
                continue                                   # explicit "all tools"
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise SystemExit(f"[hypercore] config mcp.{key}={value!r}: expected a list of tool names")
            out[key] = [v.strip() for v in value]
        else:
            print(f"[hypercore] config mcp.{key}={value!r}: unknown key, ignored", flush=True)
    return out


def _yaml_scalar(text: str):
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        body = text[1:-1]
        return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\") if text[0] == '"' else body.replace("''", "'")
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _strip_inline_comment(body: str) -> str:
    """Drop a trailing ` # comment`, but only outside quotes.

    `command: python3  # run it` loses the comment; `command: "echo # hi"` keeps its
    text, because the `#` there is inside a quoted value, not a comment. A `#` that is
    not preceded by a space is part of the value (`url: https://x/#anchor`).
    """
    quote = None
    for index, char in enumerate(body):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "#" and index and body[index - 1] in " \t":
            return body[:index].rstrip()
    return body


def parse_mcp_yaml(text: str):
    """The subset `mcp.yaml` uses: nested maps, lists of scalars, comments, and quoted strings."""
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw[:len(raw) - len(raw.lstrip(" \t"))]:
            raise SystemExit(f"[hypercore] mcp.yaml:{n}: tabs are not allowed; indent with spaces")
        indent = len(raw) - len(raw.lstrip(" "))
        body = _strip_inline_comment(raw.strip())
        if not body:
            continue
        lines.append((n, indent, body))

    def parse(i: int, indent: int):
        if i >= len(lines) or lines[i][1] < indent:
            return {}, i
        if lines[i][1] == indent and lines[i][2].startswith("- "):
            return parse_list(i, indent)
        obj = {}
        while i < len(lines) and lines[i][1] >= indent:
            n, ind, body = lines[i]
            if ind > indent:
                raise SystemExit(f"[hypercore] mcp.yaml:{n}: unexpected indent")
            if ind < indent or body.startswith("- "):
                break
            if ":" not in body:
                raise SystemExit(f"[hypercore] mcp.yaml:{n}: expected 'key:'")
            key, rest = body.split(":", 1)
            key, rest = key.strip(), rest.strip()
            if not key:
                raise SystemExit(f"[hypercore] mcp.yaml:{n}: empty key")
            if rest == "":
                if i + 1 < len(lines) and lines[i + 1][1] > indent:
                    val, i = parse(i + 1, lines[i + 1][1])
                else:
                    val, i = None, i + 1
            else:
                val, i = _yaml_scalar(rest), i + 1
            obj[key] = val
        return obj, i

    def parse_list(i: int, indent: int):
        out = []
        while i < len(lines) and lines[i][1] == indent and lines[i][2].startswith("- "):
            n, _, body = lines[i]
            item = body[2:].strip()
            if item == "":
                raise SystemExit(f"[hypercore] mcp.yaml:{n}: nested list items are not supported")
            if item.endswith(":") and ": " not in item:
                raise SystemExit(f"[hypercore] mcp.yaml:{n}: list items must be scalars")
            out.append(_yaml_scalar(item))
            i += 1
        return out, i

    data, i = parse(0, lines[0][1] if lines else 0)
    if i < len(lines):
        raise SystemExit(f"[hypercore] mcp.yaml:{lines[i][0]}: could not parse")
    if not isinstance(data, dict):
        raise SystemExit("[hypercore] mcp.yaml: the file must be a mapping")
    return data


def _abs_under(base: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not (base / path).exists():
        return value
    return str((base / path).resolve())


def load_mcp_yaml(path: str) -> tuple[dict, dict]:
    """`(servers, settings)` from an mcp.yaml file.  Relative command arguments and `cwd` are based on the file."""
    try:
        data = parse_mcp_yaml(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise SystemExit(f"[hypercore] {path}: {e}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"[hypercore] {path}: the file must be a mapping")
    settings = {k: data[k] for k in ("timeout_s", "start_timeout_s", "max_result_chars", "max_rounds",
                        "condense_at", "condense_after", "condense_chars",
                        "compact_at", "compact_retain_bytes", "tool_allowlist") if k in data}
    block = data.get("servers", data.get("mcp_servers", data.get("mcpServers")))
    servers = servers_from(block, path)
    base = Path(path).resolve().parent
    for cfg in servers.values():
        if isinstance(cfg.get("cwd"), str):
            cfg["cwd"] = _abs_under(base, cfg["cwd"])
        if isinstance(cfg.get("args"), list):
            cfg["args"] = [_abs_under(base, str(a)) for a in cfg["args"]]
        # An absolute command is used as given. resolve() would follow a venv python symlink to the
        # system interpreter, which then cannot see the venv's packages.
        command = cfg.get("command")
        if isinstance(command, str) and not Path(command).is_absolute() and (base / command).is_file():
            cfg["command"] = str((base / command).resolve())
    return servers, settings_from({"mcp": settings})


def hub_from_config(cfg: dict, mcp_config_path: str | None = None, yaml_path: str | None = None) -> McpHub | None:
    """The run config's `"mcp_servers"` (or `"mcpServers"`), then `--mcp-config` (JSON, or a yaml file), then
    `yaml_path` (`mcp.yaml`).  A name in a later source replaces the earlier one.  None when there are none."""
    servers = {}
    settings = settings_from(cfg)
    servers.update(servers_from(cfg.get("mcp_servers"), "config mcp_servers"))
    servers.update(servers_from(cfg.get("mcpServers"), "config mcpServers"))
    yaml_required = False
    if mcp_config_path and str(mcp_config_path).endswith((".yaml", ".yml")):
        yaml_path, mcp_config_path, yaml_required = mcp_config_path, None, True
    if mcp_config_path:
        try:
            with open(mcp_config_path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise SystemExit(f"[hypercore] --mcp-config {mcp_config_path}: {e}") from None
        block = data.get("mcpServers", data.get("mcp_servers")) if isinstance(data, dict) else None
        if block is None:
            raise SystemExit(f"[hypercore] --mcp-config {mcp_config_path}: expected {{\"mcpServers\": {{...}}}}")
        servers.update(servers_from(block, f"--mcp-config {mcp_config_path}"))
    if yaml_path:
        if Path(yaml_path).is_file():
            found, extra = load_mcp_yaml(yaml_path)
            servers.update(found)
            settings.update(extra)
        elif yaml_required:
            raise SystemExit(f"[hypercore] --mcp-config {yaml_path}: file not found")
    if not servers:
        return None
    return McpHub(servers, settings)


if __name__ == "__main__":                               # python -m serve.mcp config.json: list what a config offers
    config = {}
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8-sig") as f:
            config = json.load(f)
    hub = hub_from_config(config,
                          sys.argv[2] if len(sys.argv) > 2 else None)
    if hub is None:
        sys.exit("no MCP servers configured")
    hub.start(wait=True)
    print(json.dumps(hub.status(), indent=1))
    hub.close()

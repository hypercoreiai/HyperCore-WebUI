"""The shared client, fixtures, and report shape for the MCP tool tests.

Every module here does the same three things:

1. start its server over stdio with a private root (a temp directory), so a test run
   never touches the real /home, the real downloads folder, or a running chat;
2. call every tool that server advertises, at least once, plus the guardrails;
3. tag each assertion with @case(server, tool, name) so run_all.py can report
   pass/fail per tool rather than per file.

Run everything with:

    python3 serve/mcp/tests/run_all.py                    # every server, one report
    python3 serve/mcp/tests/run_all.py --server terminal  # one server

Set HYPERCORE_TEST_LIVE=0 (or --no-live) to skip the cases that need the network or Firefox.
The report format is documented in README.md beside this file.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]  # the repository root
OFF = {"0", "false", "no", "off"}


def live_enabled() -> bool:
    """Read the flag when it is asked for, so --no-live set by run_all still counts."""
    return os.environ.get("HYPERCORE_TEST_LIVE", "1").strip().lower() not in OFF


def case(server: str, tool: str, name: str):
    """Tag a test method so the report can group it by server and tool."""
    def decorate(fn):
        fn.case = {"server": server, "tool": tool, "name": name}
        return fn
    return decorate


def entry(name: str) -> str:
    """Absolute path of a server entry point under serve/mcp/."""
    return str(REPO / "serve" / "mcp" / name)


def require(binary: str) -> None:
    """Skip a test when a system tool it needs is not installed."""
    if not shutil.which(binary):
        raise unittest.SkipTest(f"{binary} is not installed")


def live(reason: str) -> None:
    """Skip a test when the environment is offline or a browser is missing."""
    if not live_enabled():
        raise unittest.SkipTest(f"live case skipped: {reason} (set HYPERCORE_TEST_LIVE=1 to run it)")


class McpError(RuntimeError):
    """The server did not answer, or answered with a protocol error."""


class Call:
    """One tool call's result: ok is False when the tool reported an error."""

    def __init__(self, tool: str, ok: bool, text: str, ms: int, raw: dict):
        self.tool = tool
        self.ok = ok
        self.text = text
        self.ms = ms
        self.raw = raw

    @property
    def data(self) -> Any:
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None

    def __repr__(self) -> str:
        return f"Call({self.tool}, ok={self.ok}, {len(self.text)} chars, {self.ms} ms)"


class McpServer:
    """A stdio MCP client: initialize, list tools, call tools, close."""

    def __init__(self, entry_path: str, env: dict | None = None, cwd: str | None = None,
                 start_timeout: float = 60.0):
        merged = dict(os.environ)
        merged.update({str(k): str(v) for k, v in (env or {}).items()})
        self.proc = subprocess.Popen([sys.executable, entry_path],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=merged, cwd=cwd or str(REPO), text=True, bufsize=1)
        self.lines: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()
        self.server_name = None
        self.tools: list[str] = []
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "hypercore-tool-tests", "version": "1"}}}, 1, start_timeout)
        self._send_raw({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = self._send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 2, start_timeout)
        self.tools = sorted(t["name"] for t in listed.get("result", {}).get("tools", []))

    def _pump(self) -> None:
        out = self.proc.stdout
        if out is None:
            return
        try:
            for line in out:
                line = line.strip()
                if line:
                    self.lines.put(line)
        except (ValueError, OSError):
            pass  # close() closed the pipe; the reader just stops

    def _send_raw(self, msg: dict) -> None:
        if self.proc.stdin is None:
            raise McpError("the server has no stdin to write to")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _send(self, msg: dict, expect_id, timeout: float) -> dict:
        self._send_raw(msg)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=1.0)
            except queue.Empty:
                if self.proc.poll() is not None:
                    tail = self.proc.stderr.read()[:300] if self.proc.stderr else ""
                    raise McpError(f"the server stopped (exit {self.proc.returncode}): {tail}")
                continue
            try:
                got = json.loads(line)
            except ValueError:
                continue
            if got.get("id") == expect_id:
                return got
        raise McpError(f"no reply to {msg.get('method')} within {timeout} s")

    def call(self, tool: str, arguments: dict | None = None, timeout: float = 120.0) -> Call:
        self._id += 1
        rid = self._id
        t0 = time.monotonic()
        reply = self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments or {}}}, rid, timeout)
        ms = int((time.monotonic() - t0) * 1000)
        if "error" in reply:
            return Call(tool, False, str(reply["error"].get("message", reply["error"])), ms, reply)
        content = reply.get("result", {}).get("content", [])
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        return Call(tool, not reply.get("result", {}).get("isError"), text, ms, reply)

    def close(self) -> None:
        try:
            self._send_raw({"jsonrpc": "2.0", "id": 0, "method": "shutdown"})
        except Exception:  # noqa: BLE001, S110 - closing is best effort
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self.proc.kill()
        self.proc.wait(timeout=10)


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path

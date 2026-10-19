"""The hub that the chat uses to reach MCP servers: config parsing, tool naming, calls,
the result cap, condensing, timeouts, cancellation, restart after a crash, and the two
transports (stdio and Streamable HTTP).

    python3 -m unittest serve.mcp.tests.test_hub

These tests use tests/mcp_fake_server.py, a dependency-free stdio/HTTP MCP server, so no
real server, GPU, or internet is involved: everything runs against localhost.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from serve.mcp import (
    COMPACT_MARK,
    CONDENSED_MARK,
    DEFAULTS,
    McpCancelled,
    McpHub,
    condense,
    deterministic_summary,
    hub_from_config,
    load_mcp_yaml,
    parse_mcp_yaml,
    plan_compaction,
    servers_from,
    settings_from,
)
from serve.mcp.tests.harness import REPO, case

SERVER = "hub"
FAKE = str(REPO / "tests" / "mcp_fake_server.py")
PY = sys.executable
FAKE_TOOLS = ["add", "big", "die", "echo", "fail", "sleep"]


def shipped_yaml() -> Path:
    """The MCP config this project ships, found the same way the server finds it.

    `mcp.yaml` beside the server first, then `config/mcp.yaml`. A test that hard-codes
    one of the two breaks the moment the file is moved, which is exactly what happened
    when the shipped config went into `config/`.
    """
    for candidate in (REPO / "mcp.yaml", REPO / "config" / "mcp.yaml"):
        if candidate.is_file():
            return candidate
    return REPO / "mcp.yaml"


def stdio(*args, **env) -> dict:
    return {"command": PY, "args": [FAKE, *args], "env": env}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 10.0) -> None:
    """A subprocess that is not listening yet is not a broken server; give it a moment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 1.0):
                return
        except OSError:
            time.sleep(0.2)
    raise unittest.SkipTest(f"the fixture server never listened on port {port}")


class Config(unittest.TestCase):
    """The settings block and the mcp.yaml reader."""

    @case(SERVER, "settings", "the hub defaults are filled in")
    def test_hub__defaults(self):
        self.assertEqual(McpHub({}, {}).settings["max_rounds"], DEFAULTS["max_rounds"])
        self.assertEqual(McpHub({}, {}).settings["timeout_s"], DEFAULTS["timeout_s"])
        self.assertEqual(McpHub({}, {"timeout_s": 5}).settings["timeout_s"], 5.0)

    @case(SERVER, "settings", "a bad timeout or round count is refused, not ignored")
    def test_hub__settings_validation(self):
        for bad in ({"timeout_s": 0}, {"timeout_s": "abc"}, {"max_result_chars": 0},
                    {"max_rounds": None}, {"start_timeout_s": -1}):
            with self.assertRaises(SystemExit):
                settings_from({"mcp": bad})

    @case(SERVER, "settings", "condense keys are read and validated")
    def test_hub__condense_settings(self):
        s = settings_from({"mcp": {"condense_at": 0.55, "condense_after": 2, "condense_chars": 1200}})
        self.assertEqual((s["condense_at"], s["condense_after"], s["condense_chars"]), (0.55, 2, 1200))
        with self.assertRaises(SystemExit):
            settings_from({"mcp": {"condense_at": 1.5}})
        with self.assertRaises(SystemExit):
            settings_from({"mcp": {"condense_after": -1}})

    @case(SERVER, "settings", "an unknown key is ignored, not fatal")
    def test_hub__settings_unknown_key(self):
        self.assertEqual(settings_from({"mcp": {"frobnicate": 3}}), {})

    @case(SERVER, "yaml", "mcp.yaml is parsed into servers and settings")
    def test_hub__yaml_parses_the_shipped_file(self):
        data = parse_mcp_yaml(shipped_yaml().read_text(encoding="utf-8-sig"))
        self.assertIn("web", data["servers"])
        self.assertIn("terminal", data["servers"])
        self.assertGreater(data["max_result_chars"], 0)
        self.assertEqual(data["compact_at"], 0.9)
        self.assertEqual(data["compact_retain_bytes"], 80000)
        self.assertIsInstance(data["servers"]["web"]["args"], list)

    @case(SERVER, "yaml", "comments, quoted strings, and lists are read")
    def test_hub__yaml_syntax(self):
        text = ("""# a comment\n
timeout_s: 12
max_rounds: 4   # trailing comment
servers:
  a:
    command: "python3"   # quoted
    args:
      - serve/x.py
      - '--flag=1'
    env:
      NAME: 'a value'
""")
        data = parse_mcp_yaml(text)
        self.assertEqual(data["timeout_s"], 12)
        self.assertEqual(data["max_rounds"], 4)
        self.assertEqual(data["servers"]["a"]["args"], ["serve/x.py", "--flag=1"])
        self.assertEqual(data["servers"]["a"]["env"]["NAME"], "a value")

    @case(SERVER, "yaml", "a # inside a quoted value is text, not a comment")
    def test_hub__yaml_quoted_hash(self):
        text = ("servers:\n"
                "  a:\n"
                "    command: \"echo # hi\"\n"
                "    args:\n"
                "      - 'a # b'   # a real comment\n"
                "    url: https://example.com/#anchor\n")
        data = parse_mcp_yaml(text)
        self.assertEqual(data["servers"]["a"]["command"], "echo # hi")
        self.assertEqual(data["servers"]["a"]["args"], ["a # b"])
        self.assertEqual(data["servers"]["a"]["url"], "https://example.com/#anchor")
        self.assertEqual(parse_mcp_yaml("servers:\n  a:\n    command: python3  # run it\n")
                         ["servers"]["a"]["command"], "python3")

    @case(SERVER, "servers", "~ and $HOME expand in a url and its headers, not only in a command")
    def test_hub__expands_http_paths(self):
        saved = os.environ.get("HOME")
        os.environ["HOME"] = "/home/example"
        try:
            servers = servers_from({"api": {"url": "$HOME/api/mcp",
                                           "headers": {"X-Key": "~/.token"}}}, "test")
            self.assertEqual(servers["api"]["url"], "/home/example/api/mcp")
            self.assertEqual(servers["api"]["headers"]["X-Key"], "/home/example/.token")
            started = servers_from({"srv": {"command": "python3", "args": ["$HOME/x.py"]}}, "test")
            self.assertEqual(started["srv"]["args"], ["/home/example/x.py"])
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved

    @case(SERVER, "yaml", "tabs and a missing key are clear errors")
    def test_hub__yaml_bad_syntax(self):
        with self.assertRaises(SystemExit):
            parse_mcp_yaml("servers:\n\t  a:\n    command: python3\n")
        with self.assertRaises(SystemExit):
            parse_mcp_yaml("servers:\n  x\n    command: python3\n")

    @case(SERVER, "yaml", "relative command arguments are resolved against the file's directory")
    def test_hub__yaml_relative_args(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "serve").mkdir()
            (root / "serve" / "server.py").write_text("", encoding="utf-8")
            (root / "mcp.yaml").write_text(
                "servers:\n  local:\n    command: python3\n    args:\n      - serve/server.py\n    cwd: serve\n",
                encoding="utf-8")
            servers, _ = load_mcp_yaml(str(root / "mcp.yaml"))
            self.assertEqual(servers["local"]["args"][0], str(root / "serve" / "server.py"))
            self.assertEqual(servers["local"]["cwd"], str(root / "serve"))

    @case(SERVER, "yaml", "a malformed file is an error, not a silent empty hub")
    def test_hub__yaml_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.yaml"
            path.write_text("servers:\n  x\n    command: python3\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                load_mcp_yaml(str(path))
            with self.assertRaises(SystemExit):
                load_mcp_yaml(str(Path(d) / "missing.yaml"))

    @case(SERVER, "servers", "a bad server entry is refused before anything starts")
    def test_hub__server_check(self):
        for bad in ({"command": ""}, {"args": "not a list"}, {"url": "http://x", "headers": "abc"},
                   {"url": "http://x", "type": "sse"}):
            with self.assertRaises(SystemExit):
                servers_from({"a": bad}, "test")
        self.assertEqual(servers_from({"a": {"command": "true"}}, "test"), {"a": {"command": "true"}})

    @case(SERVER, "servers", "a disabled server is left out")
    def test_hub__disabled_server(self):
        self.assertEqual(servers_from({"on": {"command": "true"},
                                      "off": {"command": "true", "disabled": True}}, "test"),
                       {"on": {"command": "true"}})

    @case(SERVER, "hub", "no servers configured means no hub")
    def test_hub__no_servers(self):
        self.assertIsNone(hub_from_config({}))

    @case(SERVER, "hub", "mcp_servers and mcpServers are both read; the yaml entry wins by name")
    def test_hub__config_and_yaml_merge(self):
        hub = hub_from_config({"mcp_servers": {"a": stdio()}, "mcpServers": {"b": stdio()}})
        self.assertEqual(sorted(hub.servers), ["a", "b"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mcp.yaml"
            path.write_text("servers:\n  a:\n    command: " + PY + "\n    args:\n      - " + FAKE +
                            "\n      - --page\n      - 2\n", encoding="utf-8")
            hub = hub_from_config({"mcp_servers": {"a": stdio()}}, yaml_path=str(path))
            self.assertEqual(len(hub.servers["a"].cfg["args"]), 3)

    @case(SERVER, "hub", "--mcp-config JSON adds servers; a bad one is an error")
    def test_hub__mcp_config_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"mcpServers": {"c": {"command": "true"}}}), encoding="utf-8")
            hub = hub_from_config({}, mcp_config_path=str(path))
            self.assertEqual(list(hub.servers), ["c"])
            bad = Path(d) / "bad.json"
            bad.write_text("[]", encoding="utf-8")
            with self.assertRaises(SystemExit):
                hub_from_config({}, mcp_config_path=str(bad))
            # a .yaml path in --mcp-config is treated as the yaml file
            yml = Path(d) / "servers.yaml"
            yml.write_text("servers:\n  d:\n    command: \"true\"\n", encoding="utf-8")
            hub = hub_from_config({}, mcp_config_path=str(yml))
            self.assertEqual(list(hub.servers), ["d"])

    @case(SERVER, "yaml", "the shipped mcp.yaml registers every server it ships")
    def test_hub__shipped_yaml(self):
        path = str(shipped_yaml())
        hub = hub_from_config({}, yaml_path=path)
        self.assertIsNotNone(hub)
        self.assertEqual(sorted(hub.servers),
                         ["assist", "browser", "docs", "home", "scrape", "terminal", "web"])
        _, settings = load_mcp_yaml(path)
        for key, value in settings.items():
            self.assertEqual(hub.settings[key], value)
        # the caps the project ships, checked against the file rather than against DEFAULTS,
        # so a change to the shipped config is caught here and not silently accepted.
        # The shipped pair is 500 rounds with 200 tool calls: a turn can never run more
        # calls than it has rounds, and the results are condensed from the 4th call at
        # 2400 characters rather than the 2nd at 1200. Changing one means changing this.
        self.assertEqual(hub.settings["max_rounds"], 500)
        self.assertEqual(hub.settings["max_tool_calls"], 200)
        self.assertLessEqual(hub.settings["max_tool_calls"], hub.settings["max_rounds"])
        self.assertEqual(hub.settings["condense_after"], 4)
        self.assertEqual(hub.settings["condense_chars"], 2400)


class Hub(unittest.TestCase):
    """A hub with three fake servers: one normal, one that pages its tool list, one that cannot start."""

    @classmethod
    def setUpClass(cls):
        cls.log = Path(tempfile.mktemp(prefix="hypercore-hub-", suffix=".jsonl", dir=tempfile.gettempdir()))
        cls.hub = McpHub({"fake": stdio(FAKE_MCP_LOG=str(cls.log)),
                          "paged": stdio("--page", "4"),
                          "broken": stdio("--crash-at-start")},
                         {"max_result_chars": 6000, "timeout_s": 60.0, "start_timeout_s": 60.0})
        cls.hub.start(wait=True)

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        cls.log.unlink(missing_ok=True)

    @case(SERVER, "hub", "a server that cannot start is reported, not fatal")
    def test_hub__broken_server_is_reported(self):
        self.assertEqual(self.hub.servers["broken"].status, "failed")
        self.assertIn("missing configuration", self.hub.servers["broken"].error)
        self.assertEqual(self.hub.servers["fake"].status, "ready")

    @case(SERVER, "hub", "tools are named server__tool, and a paged tool list is followed to the end")
    def test_hub__routes(self):
        names = sorted(self.hub.routes())
        self.assertEqual(names, [f"fake__{t}" for t in FAKE_TOOLS] + [f"paged__{t}" for t in FAKE_TOOLS])
        self.assertEqual(self.hub.routes()["fake__add"][0].name, "fake")
        self.assertEqual(self.hub.routes()["fake__add"][1], "add")

    @case(SERVER, "hub", "turning a server off drops its tools and does not restart it")
    def test_hub__disable_one_server(self):
        name = "paged"
        self.addCleanup(self._enable_paged)
        self.hub.set_enabled(name, False)
        self.assertEqual(self.hub.servers[name].status, "disabled")
        self.assertFalse(self.hub.servers[name].enabled)
        self.assertNotIn("paged__add", self.hub.routes())
        self.assertIn("fake__add", self.hub.routes())
        refused = self.hub.call("paged__add", {"a": 1, "b": 1})
        self.assertFalse(refused["ok"])
        self.assertEqual(self.hub.servers[name].status, "disabled")
        row = next(s for s in self.hub.status()["servers"] if s["name"] == name)
        self.assertFalse(row["enabled"])
        self.assertEqual(row["status"], "disabled")
        with self.assertRaises(KeyError):
            self.hub.set_enabled("no-such-server", False)
        self.hub.set_enabled(name, True)
        deadline = time.monotonic() + 10
        while self.hub.servers[name].status != "ready" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.hub.servers[name].status, "ready")
        self.assertIn("paged__add", self.hub.routes())

    def _enable_paged(self):
        server = self.hub.servers["paged"]
        if server.enabled and server.status == "ready":
            return
        self.hub.set_enabled("paged", True)
        deadline = time.monotonic() + 10
        while server.status != "ready" and time.monotonic() < deadline:
            time.sleep(0.05)

    @case(SERVER, "hub", "a call returns the tool's text, the server, and the time it took")
    def test_hub__call(self):
        r = self.hub.call("fake__add", {"a": 2, "b": 40})
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "42")
        self.assertEqual(r["server"], "fake")
        self.assertEqual(r["tool"], "add")
        self.assertFalse(r["truncated"])
        self.assertGreaterEqual(r["ms"], 0)

    @case(SERVER, "hub", "a tool's own error comes back as text, not an exception")
    def test_hub__tool_error(self):
        r = self.hub.call("fake__fail", {})
        self.assertFalse(r["ok"])
        self.assertTrue(r["text"].startswith("error:"))

    @case(SERVER, "hub", "an unknown tool name is an error")
    def test_hub__unknown_tool(self):
        r = self.hub.call("no_such_tool", {})
        self.assertFalse(r["ok"])
        self.assertIn("no tool named", r["text"])

    @case(SERVER, "hub", "a long result is capped and the cap says so")
    def test_hub__result_cap(self):
        r = self.hub.call("fake__big", {"n": 50000})
        self.assertTrue(r["ok"])
        self.assertEqual(r["chars"], 50000)
        self.assertTrue(r["truncated"])
        self.assertGreater(len(r["text"]), 6000)
        self.assertIn("truncated", r["text"])

    @case(SERVER, "hub", "a truncated result from a tool that reads ranges says how to continue")
    def test_hub__continuation_hint(self):
        root = tempfile.TemporaryDirectory()
        hub = McpHub({"docs": {"command": PY, "args": [str(REPO / "serve" / "mcp" / "docs_mcp.py")],
                               "env": {"HYPERCORE_DOCS_ROOT": root.name}}},
                      {"max_result_chars": 200, "timeout_s": 20.0})
        hub.start(wait=True)
        hub.call("docs__create_file", {"path": "scratch/big.txt", "content": "line\n" * 400})
        r = hub.call("docs__read_file", {"path": "scratch/big.txt"})
        self.assertTrue(r["truncated"])
        self.assertIn("start_line", r["text"])
        self.assertEqual(r["tokens_est"], r["chars"] // 4)
        st = hub.status()
        self.assertEqual(st["usage"]["calls"], 2)
        self.assertEqual(st["usage"]["truncated"], 1)
        self.assertGreater(st["usage"]["dropped_chars"], 0)
        self.assertEqual(st["collisions"], [])
        hub.close()
        root.cleanup()

    @case(SERVER, "hub", "two servers whose names clean to the same tool are reported, not silently merged")
    def test_hub__name_collision_is_recorded(self):
        hub = McpHub({"a.b": stdio(), "a_b": stdio()}, {"timeout_s": 5.0})
        hub.start(wait=True)
        routes = hub.routes()
        self.assertEqual(len(routes), 12)                    # 6 tools from each server, all reachable
        self.assertIn("a_b__echo", routes)
        self.assertIn("a_b__echo_2", routes)
        self.assertEqual(len(hub.collisions), 6)             # every name that collided is recorded
        self.assertTrue(all(name in routes for name in hub.collisions))
        hub.close()

    @case(SERVER, "hub", "a tool that outlives the timeout is an error text, not a hang")
    def test_hub__timeout(self):
        hub = McpHub({"fake": stdio()}, {"timeout_s": 1.0, "max_result_chars": 2000})
        hub.start(wait=True)
        r = hub.call("fake__sleep", {"seconds": 3})
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["text"])
        self.assertLess(r["ms"], 6000)
        hub.close()

    @case(SERVER, "hub", "cancelling a call raises McpCancelled and the server is told")
    def test_hub__cancel(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(McpCancelled):
            self.hub.call("fake__sleep", {"seconds": 5}, cancel)
        for _ in range(20):                       # the notification reaches the server a moment later
            with open(self.__class__.log, encoding="utf-8") as f:
                seen = [json.loads(line) for line in f]
            if any("cancelled" in e for e in seen):
                break
            time.sleep(0.1)
        self.assertTrue(any("cancelled" in e for e in seen))

    @case(SERVER, "hub", "a server that exits is restarted on the next call")
    def test_hub__restart_after_crash(self):
        r = self.hub.call("fake__die", {})
        self.assertFalse(r["ok"])
        self.assertEqual(self.hub.servers["fake"].status, "stopped")
        r = self.hub.call("fake__echo", {"text": "back"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "back")
        self.assertEqual(self.hub.servers["fake"].status, "ready")

    @case(SERVER, "hub", "status lists each server with its state and tool count")
    def test_hub__status(self):
        st = self.hub.status()
        by_name = {s["name"]: s for s in st["servers"]}
        self.assertEqual(by_name["fake"]["status"], "ready")
        self.assertEqual(by_name["broken"]["status"], "failed")
        self.assertEqual(by_name["fake"]["transport"], "stdio")
        self.assertEqual(len(by_name["fake"]["tools"]), len(FAKE_TOOLS))
        self.assertEqual(st["tools"], len(self.hub.routes()))
        self.assertIn("max_result_chars", st["settings"])

    @case(SERVER, "hub", "the tools the template sees carry a name, description, and schema")
    def test_hub__template_tools(self):
        tools = self.hub.template_tools(exclude=("fake__echo",))
        names = {t["name"] for t in tools}
        self.assertNotIn("fake__echo", names)
        self.assertIn("fake__add", names)
        add = next(t for t in tools if t["name"] == "fake__add")
        self.assertEqual(add["parameters"]["type"], "object")
        self.assertIn("a", add["parameters"]["properties"])
        self.assertTrue(all(t["description"] for t in tools))
        wrapped = self.hub.openai_tools()
        self.assertTrue(all(t["type"] == "function" and "name" in t["function"] for t in wrapped))

    @case(SERVER, "condense", "the head and tail survive and the middle becomes a count")
    def test_hub__condense(self):
        out = condense("A" * 1000 + "B" * 1000, 200)
        self.assertTrue(out.startswith("A" * 100) and out.endswith("B" * 100))
        self.assertIn("omitted", out)
        self.assertIn(CONDENSED_MARK, out)
        self.assertGreater(len(out), 200)
        self.assertEqual(condense(out, 200), out)          # already condensed: left alone

    @case(SERVER, "condense", "a result shorter than the keep is unchanged")
    def test_hub__condense_short(self):
        self.assertEqual(condense("short", 500), "short")
        self.assertEqual(condense("short", 0), "short")

    @case(SERVER, "condense", "the trigger is context pressure, not the round count")
    def test_hub__condense_trigger(self):
        hub = McpHub({}, {"condense_at": 0.55, "condense_after": 2, "condense_chars": 1200})
        self.assertFalse(hub.needs_condensing(60_000, 262_144, 8))      # half the window free: no need
        self.assertTrue(hub.needs_condensing(150_000, 262_144, 8))      # past 55% of it
        self.assertFalse(hub.needs_condensing(200_000, 262_144, 1))     # under the round floor
        self.assertFalse(hub.needs_condensing(150_000, 0, 8))           # a context we do not know
        self.assertFalse(McpHub({}).needs_condensing(200_000, 262_144, 8))   # condense_at 0: never

    @case(SERVER, "condense", "only earlier tool results are shortened; the newest stays whole")
    def test_hub__condense_messages(self):
        hub = McpHub({}, {"condense_chars": 400})
        msgs = [{"role": "user", "content": "hi"},
                {"role": "tool", "content": "A" * 2000},
                {"role": "assistant", "content": "ok"},
                {"role": "tool", "content": "B" * 2000}]
        out, count, dropped = hub.condense_messages(msgs)
        self.assertEqual((count, dropped), (1, 2000 - len(out[1]["content"])))
        self.assertIn(CONDENSED_MARK, out[1]["content"])
        self.assertEqual(out[3]["content"], "B" * 2000)                 # the one being answered to
        self.assertEqual(len(msgs[1]["content"]), 2000)                 # the caller's list is not touched
        self.assertEqual(hub.usage["condensed_results"], 1)
        again, count2, _ = hub.condense_messages(out)
        self.assertEqual((count2, len(again[1]["content"])), (0, len(out[1]["content"])))
        # one round can return several tool results; all of them stay whole
        multi = [{"role": "user", "content": "hi"},
                 {"role": "tool", "content": "A" * 2000},
                 {"role": "assistant", "content": "ok"},
                 {"role": "tool", "content": "B" * 2000},
                 {"role": "tool", "content": "C" * 2000}]
        mout, mcount, _ = hub.condense_messages(multi)
        self.assertEqual(mcount, 1)
        self.assertEqual(mout[3]["content"], "B" * 2000)
        self.assertEqual(mout[4]["content"], "C" * 2000)

    @case(SERVER, "condense", "condense_chars 0 turns the condenser off")
    def test_hub__condense_off(self):
        hub = McpHub({}, {"condense_at": 0.5, "condense_chars": 0})
        msgs = [{"role": "tool", "content": "A" * 2000}]
        self.assertEqual(hub.condense_messages(msgs), (msgs, 0, 0))
        self.assertEqual(hub.usage.get("condensed_results"), None)

    @case(SERVER, "compact", "compact_at is off unless it is set, and it reads like condense_at")
    def test_hub__compact_settings(self):
        self.assertFalse(McpHub({}).needs_compacting(200_000, 262_144))
        hub = McpHub({}, {"compact_at": 0.9})
        self.assertFalse(hub.needs_compacting(200_000, 262_144))          # under 90%
        self.assertTrue(hub.needs_compacting(240_000, 262_144))
        self.assertFalse(hub.needs_compacting(240_000, 0))
        s = settings_from({"mcp": {"compact_at": 0.9, "compact_retain_bytes": 80000}})
        self.assertEqual((s["compact_at"], s["compact_retain_bytes"]), (0.9, 80000))
        with self.assertRaises(SystemExit):
            settings_from({"mcp": {"compact_at": 1.5}})
        with self.assertRaises(SystemExit):
            settings_from({"mcp": {"compact_retain_bytes": 0}})

    @case(SERVER, "compact", "older tool output becomes a handoff; the newest round stays")
    def test_hub__plan_compaction_keeps_the_latest_round(self):
        old = "OLD " * 400
        messages = [
            {"role": "system", "content": "profile"},
            {"role": "user", "content": "build the index"},
            {"role": "assistant", "content": "searching",
             "tool_calls": [{"id": "c1", "function": {"name": "docs__read"}}]},
            {"role": "tool", "content": old},
            {"role": "assistant", "content": "The index is in place."},
            {"role": "user", "content": "now fix the bug"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c2", "function": {"name": "terminal__run"}}]},
            {"role": "tool", "content": "NEW"},
        ]
        plan = plan_compaction(messages, "Goal: fix the bug\nState: index is built", 80_000, keep_round=True)
        self.assertIsNotNone(plan)
        roles = [(m["role"], m.get("content")) for m in plan["messages"]]
        self.assertEqual(roles[0], ("system", "profile"))
        self.assertIn("build the index", roles[1][1])
        self.assertIn(COMPACT_MARK, roles[2][1])
        self.assertIn("Goal: fix the bug", roles[2][1])
        self.assertEqual(roles[3][1], "now fix the bug")
        self.assertEqual(plan["messages"][4]["tool_calls"][0]["id"], "c2")
        self.assertEqual(plan["messages"][5]["content"], "NEW")
        self.assertNotIn(old, json.dumps(plan["messages"]))
        self.assertEqual(plan["omit_tool_ids"], ["c1"])
        self.assertNotIn("now fix the bug", json.dumps(plan["prefix"]))
        self.assertIn(COMPACT_MARK, plan["prefix"][-1]["content"])
        # the newest round is what filled the window: fold it too
        folded = plan_compaction(messages, "Goal: fix the bug", 80_000, keep_round=False)
        self.assertNotIn("NEW", json.dumps(folded["messages"]))
        self.assertEqual(folded["omit_tool_ids"], ["c1", "c2"])
        self.assertIn("now fix the bug", json.dumps(folded["messages"]))

    @case(SERVER, "compact", "a profile turn is preserved wherever it sits in the transcript")
    def test_hub__plan_compaction_preserves_every_profile_turn(self):
        profile = "Never use an em dash. Keep the _meta JSON intact."
        old = "OLD " * 400
        messages = [
            {"role": "system", "content": "base rules"},
            {"role": "user", "content": "build the index"},
            {"role": "assistant", "content": "searching",
             "tool_calls": [{"id": "c1", "function": {"name": "docs__read"}}]},
            {"role": "tool", "content": old},
            {"role": "developer", "content": profile},          # the agent profile, added mid-chat
            {"role": "user", "content": "now fix the bug"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c2", "function": {"name": "terminal__run"}}]},
            {"role": "tool", "content": "NEW"},
        ]
        plan = plan_compaction(messages, "Goal: fix the bug", 80_000, keep_round=True)
        self.assertIsNotNone(plan)
        blob = json.dumps(plan["messages"])
        self.assertIn("base rules", blob)
        self.assertIn(profile, blob)                            # the profile survives verbatim
        self.assertNotIn(profile, json.dumps(plan["prefix"]))  # and is never folded into the handoff
        self.assertEqual([m["role"] for m in plan["messages"][:2]], ["system", "developer"])
        self.assertNotIn(old, blob)

    @case(SERVER, "compact", "the deterministic handoff never quotes a profile turn")
    def test_hub__deterministic_summary_skips_profile_turns(self):
        profile = "Never use an em dash."
        body = deterministic_summary([
            {"role": "system", "content": profile},
            {"role": "developer", "content": profile},
            {"role": "user", "content": "build the index"},
            {"role": "assistant", "content": "The index is in place."},
        ])
        self.assertNotIn(profile, body)
        self.assertIn("User: build the index", body)

    @case(SERVER, "compact", "a short transcript and a handoff-only prefix are left alone")
    def test_hub__plan_compaction_skips_when_nothing_would_drop(self):
        self.assertIsNone(plan_compaction([{"role": "user", "content": "hi"}], "summary", 80_000))
        handoff = {"role": "user", "content": f"already done\n\n[{COMPACT_MARK}]"}
        self.assertIsNone(plan_compaction(
            [handoff, {"role": "user", "content": "continue"}], "again", 80_000))

    @case(SERVER, "compact", "the deterministic handoff keeps the task and the last answer line")
    def test_hub__deterministic_summary(self):
        text = deterministic_summary([
            {"role": "user", "content": "build the index\nwith these constraints"},
            {"role": "assistant", "content": "first try failed\n\nThe index is in place."},
            {"role": "tool", "content": "wrote /tmp/index"},
        ])
        self.assertIn("User: build the index", text)
        self.assertIn("Assistant: The index is in place.", text)
        self.assertIn("Tool result: wrote /tmp/index", text)

    @case(SERVER, "compact", "one message larger than the window is cut at both ends")
    def test_hub__shrink_messages(self):
        from serve.mcp import shrink_messages
        text = "START" + ("x" * 1000) + "END"
        out = shrink_messages([{"role": "user", "content": text}, {"role": "user", "content": "short"}], 100)
        self.assertIsNotNone(out)
        self.assertTrue(out[0]["content"].startswith("START"))
        self.assertTrue(out[0]["content"].endswith("END"))
        self.assertLess(len(out[0]["content"]), len(text))
        self.assertEqual(out[1]["content"], "short")
        self.assertIsNone(shrink_messages([{"role": "user", "content": "short"}], 100))


    @case(SERVER, "hub", "close() stops every server and leaves nothing running")
    def test_hub__close(self):
        hub = McpHub({"fake": stdio()}, {"max_result_chars": 1000})
        hub.start(wait=True)
        hub.close()
        self.assertTrue(all(s.transport is None or not s.transport.alive() for s in hub.servers.values()))
        self.assertTrue(all(s.status == "stopped" for s in hub.servers.values()))


class HttpTransport(unittest.TestCase):
    """The same fake server, reached over Streamable HTTP instead of stdio."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        # The HTTP fake server is not a hub-managed transport: HttpTransport.close() only sends
        # a DELETE to end the session, so the process has to be tracked here and stopped in
        # tearDownClass, or every run of this class leaves a listening process behind.
        cls.proc = subprocess.Popen([PY, FAKE, "--http", str(cls.port)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        wait_for_port(cls.port)
        cls.hub = McpHub({"http": {"url": f"http://127.0.0.1:{cls.port}/mcp"}},
                         {"timeout_s": 30.0, "max_result_chars": 20000})
        cls.hub.start(wait=True)

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()

    @case(SERVER, "transport", "an address is spoken to over HTTP and its tools are listed")
    def test_hub__http_routes(self):
        self.assertEqual(self.hub.servers["http"].kind, "http")
        self.assertEqual(self.hub.servers["http"].status, "ready")
        self.assertEqual(sorted(self.hub.routes()), [f"http__{t}" for t in FAKE_TOOLS])

    @case(SERVER, "transport", "a call over HTTP returns the same text as one over stdio")
    def test_hub__http_call(self):
        r = self.hub.call("http__add", {"a": 1, "b": 2})
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "3")

    @case(SERVER, "transport", "an unreachable address is reported, not raised")
    def test_hub__http_unreachable(self):
        hub = McpHub({"gone": {"url": f"http://127.0.0.1:{free_port()}/mcp"}}, {"start_timeout_s": 10.0})
        hub.start(wait=True)
        self.assertEqual(hub.servers["gone"].status, "failed")
        self.assertEqual(hub.routes(), {})


class StubServer:
    """A server with a fixed tool list, for the merge logic. The hub only ever asks a server
    for its name, status, tools and a call result, so this is enough to exercise a merge."""

    def __init__(self, name: str, tools: list[str]):
        self.name, self.kind, self.status, self.error, self.info = name, "stub", "ready", None, {}
        self.enabled = True          # the hub reads it before it folds a merge into the surface
        self.tools = [{"name": t, "description": f"{name} {t}",
                       "inputSchema": {"type": "object", "properties": {}}} for t in tools]

    def call(self, tool: str, arguments: dict, timeout: float = 60.0, cancel=None) -> dict:
        body = json.dumps(arguments, sort_keys=True)
        return {"content": [{"type": "text", "text": f"{self.name}/{tool} {body}"}]}


def merge_hub(browser: bool = True, docs: bool = True) -> McpHub:
    hub = McpHub({})
    hub.servers = {
        "web": StubServer("web", ["status", "search", "browse"]),
        "home": StubServer("home", ["status", "search", "structure", "read", "reindex"]),
        "docs": StubServer("docs", ["create_file", "read_file", "edit_file", "write_file",
                                    "delete_file", "list_directory"]),
        "scrape": StubServer("scrape", ["extract_media", "download_file", "list_downloads"]),
    }
    if browser:
        hub.servers["browser"] = StubServer("browser", ["status", "fetch", "links", "probe",
                                                        "screenshot", "close"])
    if not docs:
        hub.servers.pop("docs")
    return hub


class Merges(unittest.TestCase):
    """Tier 1: pairs the hub folds into one tool, and the fallback when a part is missing."""

    @case(SERVER, "merge", "two browse tools and two status tools become one each")
    def test_merge__surface(self):
        surface = merge_hub().surface()
        for gone in ("web__browse", "browser__fetch", "web__status", "browser__status",
                     "home__structure", "docs__list_directory"):
            self.assertNotIn(gone, surface, f"{gone} should be folded into a merged tool")
        for present in ("browse", "status", "ls", "web__search", "browser__probe", "docs__read_file"):
            self.assertIn(present, surface, f"{present} should still be offered")

    @case(SERVER, "merge", "a merge only applies when every part of it is present")
    def test_merge__falls_back(self):
        surface = merge_hub(browser=False, docs=False).surface()
        self.assertIn("web__browse", surface)
        self.assertIn("web__status", surface)
        self.assertNotIn("browse", surface)
        self.assertNotIn("status", surface)          # the home server's status is a different tool
        self.assertIn("home__structure", surface)    # no docs server: ls is not formed
        self.assertIn("ls", merge_hub().surface())

    @case(SERVER, "merge", "browse picks the engine the caller asked for")
    def test_merge__browse_routes(self):
        hub = merge_hub()
        plain = hub.call("browse", {"url": "https://example.com"})
        self.assertTrue(plain["ok"])
        self.assertEqual(plain["text"], 'web/browse {"url": "https://example.com"}')
        self.assertEqual(plain["server"], "web")
        stealth = hub.call("browse", {"url": "https://example.com", "engine": "stealth", "max_chars": 1200})
        self.assertEqual(stealth["text"], 'browser/fetch {"max_chars": 1200, "url": "https://example.com"}')
        self.assertEqual(stealth["server"], "browser")

    @case(SERVER, "merge", "status answers with both engines in one result")
    def test_merge__status_joins(self):
        r = merge_hub().call("status", {})
        self.assertTrue(r["ok"])
        self.assertIn("web/status", r["text"])
        self.assertIn("browser/status", r["text"])
        self.assertIsNone(r["server"])
        self.assertEqual(r["tool"], "status")

    @case(SERVER, "merge", "ls goes to the index by default and to the sandbox on all_files")
    def test_merge__ls_routes(self):
        hub = merge_hub()
        self.assertEqual(hub.call("ls", {"path": "/home/ai-user0"})["text"],
                         'home/structure {"depth": 2, "path": "/home/ai-user0"}')
        r = hub.call("ls", {"path": ".", "depth": 3, "all_files": True})
        self.assertEqual(r["text"], 'docs/list_directory {"depth": 3, "path": "."}')
        self.assertEqual(r["server"], "docs")

    @case(SERVER, "merge", "one part failing is an error, not a silent half-answer")
    def test_merge__part_fails(self):
        hub = merge_hub()
        hub.servers["browser"].call = lambda tool, args, timeout=60.0, cancel=None: {
            "content": [{"type": "text", "text": "the engine is not installed"}], "isError": True}
        r = hub.call("status", {})
        self.assertFalse(r["ok"])
        self.assertIn("web/status", r["text"])
        self.assertIn("error: the engine is not installed", r["text"])

    @case(SERVER, "merge", "the Monitor reports the fold, and the count is the surface")
    def test_merge__status_endpoint(self):
        hub = merge_hub()
        out = hub.status()
        self.assertEqual(out["tools"], len(hub.surface()))
        self.assertEqual(out["merged"], ["browse", "ls", "status"])
        web = next(s for s in out["servers"] if s["name"] == "web")
        folded = {t["name"]: t.get("folded_into") for t in web["tools"]}
        self.assertEqual(folded["web__browse"], "browse")   # the server's own tool, marked as folded
        self.assertIsNone(folded["web__search"])

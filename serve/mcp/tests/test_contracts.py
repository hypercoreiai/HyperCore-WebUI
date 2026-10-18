"""The tool contract: what every MCP server in mcp.yaml must advertise and honour.

    python3 serve/mcp/tests/run_all.py --server contracts

This is the fast pass a retest needs before the deeper per-server tests: it starts each
configured server against a temporary root, reads its tools/list, and checks that every
tool has a name, a description, and a usable input schema.  No network, no browser, no
build - it runs in a second or two, and it is the check that catches a tool that has
silently disappeared or had its arguments changed.

The expected tool lists are the ones in serve/mcp/tests/README.md and in
/home/ai-agent/_Notes/MCP_Tools.md: 6 servers, 41 tools. Four of them are folded by the hub
into one tool each (see MERGES in serve/mcp/__init__.py), so the model is offered 38.
"""
from __future__ import annotations

import importlib
import inspect
import re
import tempfile
import unittest

from serve.mcp import McpHub
from serve.mcp.tests.harness import McpServer, case, entry

SERVER = "contracts"

# server -> entry point -> the tools it must offer
EXPECTED = {
    "web": ("web_mcp.py", ["status", "search", "browse"]),
    "home": ("rag_mcp.py", ["status", "search", "structure", "read", "reindex"]),
    "docs": ("docs_mcp.py", ["create_file", "read_file", "edit_file", "write_file", "delete_file",
                             "list_directory"]),
    "scrape": ("web_scrape_mcp.py", ["extract_media", "download_file", "download_page_media",
                                     "batch_download", "file_info", "list_downloads", "remove_downloads"]),
    "terminal": ("terminal_mcp.py", ["run", "pipe", "process", "repl", "symbols", "check", "install",
                                     "build", "compile", "tidy", "debug", "query", "compare", "status"]),
    "browser": ("browser_mcp.py", ["status", "fetch", "links", "probe", "screenshot", "close"]),
}

# A tool with no arguments is allowed; anything else must declare its arguments.
def schema_problems(tool: dict) -> list[str]:
    problems = []
    name = tool.get("name") or ""
    if not name:
        problems.append("no name")
    if not str(tool.get("description") or "").strip():
        problems.append(f"{name}: no description")
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        problems.append(f"{name}: inputSchema is missing or not an object")
        return problems
    if schema.get("type") != "object":
        problems.append(f"{name}: inputSchema type is {schema.get('type')!r}, expected 'object'")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        problems.append(f"{name}: properties must be an object")
        return properties and problems
    for required in schema.get("required") or []:
        if not isinstance(properties, dict) or required not in properties:
            problems.append(f"{name}: required argument {required!r} is not described")
    for key, spec in (properties or {}).items():
        if not isinstance(spec, dict) or not _declares_type(spec):
            problems.append(f"{name}: argument {key!r} has no type")
    return problems


def _declares_type(spec: dict) -> bool:
    """A plain type, or a union whose every branch is typed (optional arguments)."""
    if "type" in spec:
        return True
    branches = spec.get("anyOf") or spec.get("oneOf")
    return isinstance(branches, list) and bool(branches) and all(
        isinstance(branch, dict) and "type" in branch for branch in branches)


# the module whose functions answer each server's tools, and the tools whose function is
# not named after the tool.  A schema that advertises an argument the handler ignores, or a
# handler that accepts one the model is never told about, is a bug waiting to be found.
HANDLER_MODULE = {"terminal": "serve.terminal_mcp", "docs": "serve.docs_mcp"}
HANDLER_ALIAS = {"compile": "compile_source"}


def argument_problems(module, tool: dict) -> list[str]:
    """Arguments the schema advertises that the handler does not take, and parameters the
    handler takes that the schema never mentions."""
    name = str(tool.get("name") or "")
    fn = getattr(module, HANDLER_ALIAS.get(name, name), None)
    if not callable(fn):
        return []
    params = inspect.signature(fn).parameters
    advertised = set((tool.get("inputSchema") or {}).get("properties") or {})
    problems = []
    for prop in sorted(advertised - set(params)):
        problems.append(f"{name}: the schema advertises {prop!r}, the handler does not accept it")
    for param, spec in params.items():
        if param in ("self", "cls") or spec.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if spec.default is inspect.Parameter.empty and param not in advertised:
            problems.append(f"{name}: the handler requires {param!r}, the schema does not describe it")
    return problems


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        cls.servers: dict[str, McpServer] = {}
        cls.details: dict[str, list[dict]] = {}
        for name, (script, _) in EXPECTED.items():
            env = {"HYPERCORE_DOCS_ROOT": root, "HYPERCORE_RAG_ROOT": root, "HYPERCORE_RAG_DB": f"{root}/rag.sqlite",
                   "SCRAPE_DIR": f"{root}/downloads", "TERMINAL_ROOT": root, "TERMINAL_STATE_DIR": f"{root}/terminal"}
            cls.servers[name] = McpServer(entry(script), env=env)

    @classmethod
    def tearDownClass(cls):
        for srv in cls.servers.values():
            srv.close()
        cls.tmp.cleanup()

    @case(SERVER, "tools", "no two tools collide once the namespace is applied")
    def test_contracts__namespaced_names_are_unique(self):
        def clean(name: str) -> str:
            return re.sub(r"[^A-Za-z0-9_-]", "_", str(name)) or "x"
        names = [f"{clean(server)}__{clean(tool)}"
                 for server, srv in self.servers.items() for tool in srv.tools]
        self.assertEqual(len(names), len(set(names)), "two tools would reach the model under the same name")
        self.assertGreaterEqual(len(names), 41)

    @case(SERVER, "tools", "the six configured servers start and answer tools/list")
    def test_contracts__every_server_starts(self):
        for name, srv in self.servers.items():
            self.assertTrue(srv.tools, f"{name} advertised no tools")

    @case(SERVER, "tools", "each server offers exactly the tools it is documented to offer")
    def test_contracts__tool_lists_match(self):
        for name, (_, expected) in EXPECTED.items():
            self.assertEqual(sorted(self.servers[name].tools), sorted(expected),
                            f"{name} tools changed: {self.servers[name].tools}")
        total = sum(len(v[1]) for v in EXPECTED.values())
        self.assertEqual(total, 41)

    @case(SERVER, "tools", "the hub folds six tools into three, and the surface is what the model sees")
    def test_contracts__merged_surface(self):
        """The merge logic, checked against the tool names the servers actually advertise."""
        class Stub:
            def __init__(self, name, tools):
                self.name, self.kind, self.status, self.error, self.info = name, "stub", "ready", None, {}
                self.enabled = True   # the hub reads it before it folds a merge into the surface
                self.tools = [{"name": t, "description": f"{name} {t}",
                              "inputSchema": {"type": "object", "properties": {}}} for t in tools]

        hub = McpHub({})
        hub.servers = {name: Stub(name, tools) for name, (_, tools) in EXPECTED.items()}
        surface = hub.surface()
        self.assertEqual(len(surface), 41 - 6 + 3, "six tools folded into three merged tools")
        self.assertEqual(sorted(hub.status()["merged"]), ["browse", "ls", "status"])
        for folded in ("web__browse", "browser__fetch", "web__status", "browser__status",
                       "home__structure", "docs__list_directory"):
            self.assertNotIn(folded, surface)
        for tool in ("browse", "status", "ls", "browser__probe", "home__search", "docs__edit_file",
                     "terminal__run", "scrape__list_downloads"):
            self.assertIn(tool, surface)
            self.assertTrue(surface[tool]["description"], f"{tool} has no description")
        # with the browser server down, nothing is folded and the web tools stand on their own
        hub.servers.pop("browser")
        surface = hub.surface()
        # browse and status need the browser server; without it only ls is formed, so the web
        # tools stand on their own: 35 advertised - 2 folded into ls + 1 merged = 34
        self.assertEqual(len(surface), 35 - 2 + 1)
        self.assertIn("web__browse", surface)
        self.assertIn("web__status", surface)
        self.assertNotIn("browse", surface)
        self.assertIn("ls", surface)                    # ls needs only home and docs

    @case(SERVER, "tools", "every tool has a description and a valid input schema")
    def test_contracts__schemas(self):
        problems = []
        for name in EXPECTED:
            srv = self.servers[name]
            raw = srv._send({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}}, 99, 60)
            for tool in raw.get("result", {}).get("tools", []):
                problems += [f"{name}.{p}" for p in schema_problems(tool)]
        self.assertFalse(problems, "tool schemas are not clean:\n" + "\n".join(problems))

    @case(SERVER, "tools", "every advertised argument is one the handler actually accepts")
    def test_contracts__arguments_match_the_handler(self):
        problems = []
        for name, module_name in HANDLER_MODULE.items():
            module = importlib.import_module(module_name)
            raw = self.servers[name]._send({"jsonrpc": "2.0", "id": 100, "method": "tools/list", "params": {}},
                                            100, 60)
            for tool in raw.get("result", {}).get("tools", []):
                problems += [f"{name}.{p}" for p in argument_problems(module, tool)]
        self.assertFalse(problems, "tool schemas and handlers disagree:\n" + "\n".join(problems))

    @case(SERVER, "tools", "a tool the server does not have is an error, not a crash")
    def test_contracts__unknown_tool(self):
        for name, srv in self.servers.items():
            r = srv.call("definitely_not_a_tool", {})
            self.assertFalse(r.ok, f"{name} answered an unknown tool as if it existed")
            self.assertTrue(r.text.strip())

    @case(SERVER, "tools", "a bad argument is answered with readable text, never a crash")
    def test_contracts__bad_arguments(self):
        cases = [("terminal", "run", {"command": 7}),
                 ("docs", "read_file", {"path": None}),
                 ("home", "search", {"query": ["two", "words"]}),
                 ("scrape", "extract_media", {"url": ""}),
                 ("web", "browse", {}),
                 ("browser", "fetch", {"url": ""})]
        for name, tool, args in cases:
            r = self.servers[name].call(tool, args)
            self.assertTrue(r.text.strip(), f"{name}.{tool} answered nothing for {args}")

    @case(SERVER, "tools", "a server keeps answering after a call it could not honour")
    def test_contracts__recovers_after_a_bad_call(self):
        probe = {"terminal": "status", "docs": "list_directory", "home": "status",
                 "scrape": "status", "web": "status", "browser": "status"}
        for name, srv in self.servers.items():
            srv.call("definitely_not_a_tool", {})
            r = srv.call(probe[name], {})
            self.assertTrue(r.text.strip(), f"{name} stopped answering after an unknown tool")

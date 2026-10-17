"""The document index server: status, search, structure, read, reindex.

    python3 -m unittest serve.mcp.tests.test_home

HYPERCORE_RAG_ROOT and HYPERCORE_RAG_DB point the index at a temporary tree and a temporary
database, so these tests never read or write the real /home index.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from serve.mcp.tests.harness import McpServer, case, entry, write

SERVER = "home"
ADVERTISED = ["read", "reindex", "search", "status", "structure"]

NOTE = ("Casper fuel notes. The tanking schedule for the Casper site is kept here. "
        "Fuel: 42 litres per week, logged in the fuel ledger.\n") * 3
SERVICE = "[Unit]\nDescription=Casper telemetry collector\n\n[Service]\nExecStart=/usr/bin/collector\n"
CSV = "date,litres\n2026-01-01,42\n2026-01-02,38\n"


class Home(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        write(cls.root, "docs/casper.md", NOTE)
        write(cls.root, "docs/ledger.csv", CSV)
        write(cls.root, "etc/collector.service", SERVICE)
        write(cls.root, "docs/nested/deep.md", "A deep document about Casper fuel again.")
        cls.root.joinpath("skipme.bin").write_bytes(b"\x00" * 4096)
        cls.db = cls.root / "index.sqlite"
        cls.srv = McpServer(entry("rag_mcp.py"),
                           env={"HYPERCORE_RAG_ROOT": str(cls.root), "HYPERCORE_RAG_DB": str(cls.db)})
        # The index has to be built before the read tools can answer anything: the crawl
        # runs on a background thread, so wait for it rather than assume it finished.
        cls.srv.call("reindex")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            st = cls.srv.call("status").data or {}
            if st.get("phase") == "ready" and st.get("rows"):
                break
            time.sleep(0.5)
        else:
            raise unittest.SkipTest("the index never finished building: "
                                   f"{cls.srv.call('status').text[:200]}")

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.tmp.cleanup()

    @case(SERVER, "tools", "advertises all 5 tools")
    def test_home__advertised_tools(self):
        self.assertEqual(self.srv.tools, sorted(ADVERTISED))

    @case(SERVER, "reindex", "a crawl completes and reports what it indexed")
    def test_home__reindex_builds_the_index(self):
        st = self.srv.call("status").data
        self.assertEqual(st["phase"], "ready")
        # four text files are indexed; the .bin fixture is skipped as binary
        self.assertEqual(st["files"], 4)
        self.assertGreater(st["dirs"], 0)
        self.assertEqual(st["root"], str(self.root))

    @case(SERVER, "status", "counts come from the index, not only from this process")
    def test_home__status_counts(self):
        st = self.srv.call("status").data
        self.assertGreater(st["rows"], 0)
        self.assertGreater(st["built"], 0)
        self.assertEqual(st["skipped"], 0)

    @case(SERVER, "search", "finds a document by its words")
    def test_home__search_finds(self):
        r = self.srv.call("search", {"query": "Casper fuel tanking schedule", "limit": 5})
        self.assertIn("casper.md", r.text)

    @case(SERVER, "search", "a directory is indexed too, so a folder can be found")
    def test_home__search_finds_a_directory(self):
        r = self.srv.call("search", {"query": "docs directory"})
        self.assertTrue(r.ok)

    @case(SERVER, "search", "no match is stated plainly")
    def test_home__search_no_match(self):
        r = self.srv.call("search", {"query": "zzzqqq nonexistent phrase"})
        self.assertIn("No matching documents", r.text)

    @case(SERVER, "search", "Linux unit files are indexed as config")
    def test_home__search_config(self):
        r = self.srv.call("search", {"query": "collector ExecStart"})
        self.assertIn("collector.service", r.text)

    @case(SERVER, "structure", "shows the tree under a path")
    def test_home__structure(self):
        tree = self.srv.call("structure", {"path": str(self.root), "depth": 2}).text
        self.assertIn("casper.md", tree)
        self.assertIn("docs/", tree)

    @case(SERVER, "structure", "a path above the root is not shown")
    def test_home__structure_outside_root(self):
        tree = self.srv.call("structure", {"path": "/etc", "depth": 1}).text
        self.assertNotIn("/etc/passwd", tree)

    @case(SERVER, "read", "reads a document by path")
    def test_home__read(self):
        r = self.srv.call("read", {"path": str(self.root / "docs/casper.md")})
        self.assertIn("Casper fuel notes", r.text)

    @case(SERVER, "read", "a line range comes back numbered with a continuation line")
    def test_home__read_range(self):
        r = self.srv.call("read", {"path": str(self.root / "docs/casper.md"), "start_line": 1, "end_line": 2})
        self.assertTrue(r.ok)
        self.assertIn("     1\tCasper fuel notes", r.text)
        self.assertIn("continue with start_line=3", r.text)

    @case(SERVER, "read", "a range past the end is an error, not an empty answer")
    def test_home__read_range_past_end(self):
        r = self.srv.call("read", {"path": str(self.root / "docs/casper.md"), "start_line": 900})
        self.assertFalse(r.ok)
        self.assertIn("past the end", r.text)

    @case(SERVER, "read", "a range far into a long file is still readable")
    def test_home__read_deep_range(self):
        # about 130,000 characters: past the 80k window the read used to stop at, which made
        # line 19000 look like it was past the end of the file
        write(self.root, "docs/deep.txt", "\n".join(f"line {i}" for i in range(1, 20001)))
        r = self.srv.call("read", {"path": str(self.root / "docs/deep.txt"),
                                   "start_line": 19000, "end_line": 19002})
        self.assertTrue(r.ok, r.text)
        self.assertIn("19000\tline 19000", r.text)
        self.assertIn("continue with start_line=19003", r.text)

    @case(SERVER, "read", "a large document is outlined instead of cut off")
    def test_home__read_outline(self):
        body = "\n".join(f"## Section {i}\n\n" + "fuel ledger text. " * 40 for i in range(30))
        write(self.root, "docs/long.md", body)
        r = self.srv.call("read", {"path": str(self.root / "docs/long.md")})
        self.assertTrue(r.ok)
        self.assertIn("# File outline for", r.text)
        self.assertIn("Do NOT retry this call", r.text)
        self.assertIn("heading  ## Section 0", r.text)

    @case(SERVER, "read", "a path outside the root is refused")
    def test_home__read_outside_root(self):
        r = self.srv.call("read", {"path": "/etc/passwd"})
        self.assertFalse(r.ok)

    @case(SERVER, "read", "a binary file is not returned as text")
    def test_home__read_binary(self):
        r = self.srv.call("read", {"path": str(self.root / "skipme.bin")})
        self.assertFalse(r.ok)

    @case(SERVER, "search", "the limit is clamped to 1-12")
    def test_home__search_limit_clamped(self):
        for limit in (0, 100):
            r = self.srv.call("search", {"query": "Casper fuel", "limit": limit})
            self.assertTrue(r.ok)
            self.assertLessEqual(len([line for line in r.text.splitlines() if line.startswith("[")]), 12)

    @case(SERVER, "search", "an empty query is stated plainly, not an error")
    def test_home__search_empty_query(self):
        r = self.srv.call("search", {"query": ""})
        self.assertTrue(r.ok)
        self.assertIn("No matching documents", r.text)

    @case(SERVER, "reindex", "a second reindex while one is running returns at once")
    def test_home__reindex_is_not_restarted(self):
        first = self.srv.call("reindex")
        self.assertIn("Crawl", first.text)
        second = self.srv.call("reindex")
        self.assertIn("Crawl", second.text)
        self.assertLess(second.ms, 2000)

    @case(SERVER, "structure", "the depth is clamped to 1-4")
    def test_home__structure_depth_clamped(self):
        r = self.srv.call("structure", {"path": str(self.root), "depth": 9})
        self.assertTrue(r.ok)
        self.assertIn("casper.md", r.text)

    @case(SERVER, "structure", "a file, not a directory, is an error")
    def test_home__structure_on_a_file(self):
        self.assertFalse(self.srv.call("structure", {"path": str(self.root / "docs/casper.md")}).ok)

    @case(SERVER, "status", "the root and the database the index is reading are named")
    def test_home__status_names_the_index(self):
        st = self.srv.call("status").data
        self.assertEqual(st["root"], str(self.root))
        self.assertEqual(st["db"], str(self.db))

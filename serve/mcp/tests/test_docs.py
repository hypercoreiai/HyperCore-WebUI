"""The document server: create_file, read_file, edit_file, write_file, delete_file, list_directory.

    python3 -m unittest serve.mcp.tests.test_docs

HYPERCORE_DOCS_ROOT points at a temporary directory, so these tests never touch /home.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from serve.mcp.tests.harness import McpServer, case, entry, write

SERVER = "docs"
ADVERTISED = ["create_file", "delete_file", "edit_file", "list_directory", "read_file", "write_file"]
EVALS = Path(__file__).resolve().parent / "evals"


class Docs(unittest.TestCase):
    srv: McpServer          # set in setUpClass; declared so the typed helper below can use it

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.srv = McpServer(entry("docs_mcp.py"), env={"HYPERCORE_DOCS_ROOT": str(cls.root)})

    def make(self, relative: str, content: str) -> None:
        """Create a file through the server, so this session owns it and an edit needs no force."""
        r = self.srv.call("create_file", {"path": relative, "content": content})
        self.assertTrue(r.ok, r.text)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.tmp.cleanup()

    @case(SERVER, "tools", "advertises all 6 tools")
    def test_docs__advertised_tools(self):
        self.assertEqual(self.srv.tools, sorted(ADVERTISED))

    @case(SERVER, "create_file", "creates a document and reports the path")
    def test_docs__create(self):
        r = self.srv.call("create_file", {"path": "notes/plan.md", "content": "# Plan\n\n- one\n"})
        self.assertTrue(r.ok)
        self.assertIn("Successfully created", r.text)
        self.assertTrue((self.root / "notes/plan.md").is_file())

    @case(SERVER, "create_file", "a second create of the same path is an error, not an overwrite")
    def test_docs__create_duplicate(self):
        self.srv.call("create_file", {"path": "notes/dupe.txt", "content": "first"})
        r = self.srv.call("create_file", {"path": "notes/dupe.txt", "content": "second"})
        self.assertIn("already exists", r.text)
        self.assertEqual((self.root / "notes/dupe.txt").read_text(), "first")

    @case(SERVER, "create_file", "a path above the root is refused")
    def test_docs__create_outside_root(self):
        r = self.srv.call("create_file", {"path": "../outside.txt", "content": "x"})
        self.assertIn("outside the workspace root", r.text)

    @case(SERVER, "create_file", "a credential is not a document, but it stays readable")
    def test_docs__create_secret(self):
        r = self.srv.call("create_file", {"path": ".ssh/id_rsa", "content": "secret"})
        self.assertIn("Security Error", r.text)
        self.assertFalse((self.root / ".ssh").exists())
        # Reading one is still allowed: the shell can cat a key file, so the document tools can.
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_rsa").write_text("secret", encoding="utf-8")
        got = self.srv.call("read_file", {"path": ".ssh/id_rsa"})
        self.assertIn("secret", got.text)

    @case(SERVER, "create_file", "nested directories are created on the way")
    def test_docs__create_nested(self):
        self.srv.call("create_file", {"path": "a/b/c/deep.txt", "content": "deep"})
        self.assertTrue((self.root / "a/b/c/deep.txt").is_file())

    @case(SERVER, "read_file", "returns the text of a document")
    def test_docs__read(self):
        r = self.srv.call("read_file", {"path": "notes/plan.md"})
        self.assertTrue(r.ok)
        self.assertIn("- one", r.text)

    @case(SERVER, "read_file", "a missing file is an error")
    def test_docs__read_missing(self):
        r = self.srv.call("read_file", {"path": "notes/nope.md"})
        self.assertIn("not found", r.text.lower())

    @case(SERVER, "read_file", "a binary file is refused rather than returned as mojibake")
    def test_docs__read_binary(self):
        write(self.root, "img.png", "\x89PNG\r\n\x1a\n" + "\x00" * 100)
        r = self.srv.call("read_file", {"path": "img.png"})
        self.assertIn("only text documents", r.text)

    @case(SERVER, "write_file", "replaces the content of an existing document")
    def test_docs__write(self):
        self.srv.call("create_file", {"path": "notes/edit.txt", "content": "before"})
        r = self.srv.call("write_file", {"path": "notes/edit.txt", "content": "after"})
        self.assertTrue(r.ok)
        self.assertEqual((self.root / "notes/edit.txt").read_text(), "after")

    @case(SERVER, "write_file", "does not create a file that is not there")
    def test_docs__write_missing(self):
        r = self.srv.call("write_file", {"path": "notes/newfile.txt", "content": "x"})
        self.assertIn("not found", r.text.lower())
        self.assertIn("create_file", r.text)
        self.assertFalse((self.root / "notes/newfile.txt").exists())

    @case(SERVER, "list_directory", "lists entries with their type and size")
    def test_docs__list(self):
        self.srv.call("create_file", {"path": "dir/one.txt", "content": "hello"})
        r = self.srv.call("list_directory", {"path": "dir"})
        rows = r.data["entries"]
        self.assertEqual([e["name"] for e in rows], ["one.txt"])
        self.assertEqual(rows[0]["type"], "file")
        self.assertEqual(rows[0]["bytes"], 5)

    @case(SERVER, "list_directory", "directories are marked as directories")
    def test_docs__list_dirs(self):
        self.srv.call("create_file", {"path": "parent/child/file.txt", "content": "x"})
        rows = self.srv.call("list_directory", {"path": "parent"}).data["entries"]
        self.assertEqual(rows[0]["type"], "dir")

    @case(SERVER, "list_directory", "the default path is the root")
    def test_docs__list_root(self):
        rows = self.srv.call("list_directory", {}).data["entries"]
        self.assertTrue(any(e["name"] == "notes" for e in rows))

    @case(SERVER, "delete_file", "deletes a document and reports the bytes removed")
    def test_docs__delete(self):
        self.srv.call("create_file", {"path": "gone.txt", "content": "x" * 128})
        r = self.srv.call("delete_file", {"path": "gone.txt"})
        self.assertTrue(r.ok)
        self.assertIn("128 bytes", r.text)
        self.assertFalse((self.root / "gone.txt").exists())

    @case(SERVER, "delete_file", "deleting a missing file is an error")
    def test_docs__delete_missing(self):
        r = self.srv.call("delete_file", {"path": "never existed.txt"})
        self.assertIn("not found", r.text.lower())

    @case(SERVER, "create_file", "a document larger than the cap is refused")
    def test_docs__too_large(self):
        r = self.srv.call("create_file", {"path": "huge.txt", "content": "x" * (1_500_001)})
        self.assertIn("1500000", r.text)

    @case(SERVER, "create_file", "a path that escapes the root through a symlink is refused")
    def test_docs__create_symlink_escape(self):
        link = self.root / "escape"
        if not link.exists():
            link.symlink_to(Path("/etc"))
        r = self.srv.call("create_file", {"path": "escape/passwd.txt", "content": "x"})
        self.assertIn("outside the workspace root", r.text)
        self.assertFalse((Path("/etc") / "passwd.txt").exists())

    @case(SERVER, "create_file", "a non-text name is refused")
    def test_docs__create_binary_name(self):
        r = self.srv.call("create_file", {"path": "figure.zip", "content": "x"})
        self.assertIn("only text documents", r.text)
        self.assertFalse((self.root / "figure.zip").exists())

    @case(SERVER, "create_file", "the root itself is not a document")
    def test_docs__create_at_root(self):
        r = self.srv.call("create_file", {"path": "", "content": "x"})
        self.assertIn("only text documents", r.text)

    @case(SERVER, "create_file", "non-ASCII text is written and read back unchanged")
    def test_docs__create_unicode(self):
        text = "Café — 日本語 ✓ na\u00efve\n"
        self.srv.call("create_file", {"path": "unicode.md", "content": text})
        r = self.srv.call("read_file", {"path": "unicode.md"})
        self.assertEqual(r.text, text)

    @case(SERVER, "read_file", "a directory is not a document")
    def test_docs__read_directory(self):
        r = self.srv.call("read_file", {"path": "notes"})
        self.assertIn("not found", r.text.lower())

    @case(SERVER, "read_file", "a document over the cap is read with the rest named")
    def test_docs__read_cap(self):
        write(self.root, "long.txt", "y" * (1_500_000 + 120))
        r = self.srv.call("read_file", {"path": "long.txt"})
        self.assertIn("showing 20000 of 1500120 characters", r.text)
        self.assertIn("Do NOT repeat this call", r.text)
        self.assertLessEqual(len(r.text), 20_400)

    @case(SERVER, "read_file", "a range comes back numbered with a continuation line")
    def test_docs__read_range(self):
        write(self.root, "range.txt", "\n".join(f"l{i}" for i in range(1, 121)))
        r = self.srv.call("read_file", {"path": "range.txt", "start_line": 3, "end_line": 4})
        self.assertTrue(r.ok)
        self.assertIn("     3\tl3", r.text)
        self.assertIn("continue with start_line=5", r.text)

    @case(SERVER, "edit_file", "replaces matched text and fails cleanly when it does not match")
    def test_docs__edit_file(self):
        self.make("edit.txt", "alpha\nbeta\n")
        r = self.srv.call("edit_file", {"path": "edit.txt",
                                        "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]})
        self.assertTrue(r.ok)
        self.assertIn("1 edit(s) applied", r.text)
        self.assertEqual((self.root / "edit.txt").read_text(encoding="utf-8"), "ALPHA\nbeta\n")
        bad = self.srv.call("edit_file", {"path": "edit.txt", "edits": [{"old_text": "zzz", "new_text": "x"}]})
        self.assertIn("not found", bad.text)
        self.assertFalse((self.root / "edit.txt").read_text(encoding="utf-8").startswith("x"))

    @case(SERVER, "edit_file", "an ambiguous old_text names the lines and writes nothing")
    def test_docs__edit_ambiguous(self):
        original = "def a():\n    return 1\n\ndef b():\n    return 1\n"
        self.make("ambig.py", original)
        r = self.srv.call("edit_file", {"path": "ambig.py",
                                        "edits": [{"old_text": "return 1", "new_text": "return 2"}]})
        self.assertIn("matches 2 places", r.text)
        self.assertIn("lines 2, 5", r.text)
        self.assertEqual((self.root / "ambig.py").read_text(encoding="utf-8"), original)
        r = self.srv.call("edit_file", {"path": "ambig.py",
                                        "edits": [{"old_text": "return 1", "new_text": "return 2", "line": 5}]})
        self.assertIn("1 edit(s) applied", r.text)
        self.assertEqual((self.root / "ambig.py").read_text(encoding="utf-8"),
                         "def a():\n    return 1\n\ndef b():\n    return 2\n")

    @case(SERVER, "edit_file", "a bad edit in a group leaves the file untouched")
    def test_docs__edit_atomic(self):
        self.make("atomic.txt", "one\ntwo\n")
        r = self.srv.call("edit_file", {"path": "atomic.txt", "edits": [
            {"old_text": "one", "new_text": "ONE"}, {"old_text": "nope", "new_text": "NOPE"}]})
        self.assertIn("not found", r.text)
        self.assertEqual((self.root / "atomic.txt").read_text(encoding="utf-8"), "one\ntwo\n")

    @case(SERVER, "edit_file", "edits given as a JSON string, or with a copied line-number prefix, still apply")
    def test_docs__edit_coercion(self):
        self.make("coerce.py", "alpha\nbeta\n")
        r = self.srv.call("edit_file", {"path": "coerce.py",
                                        "edits": '[{"old_text": "alpha", "new_text": "ALPHA"}]'})
        self.assertIn("1 edit(s) applied", r.text)
        r = self.srv.call("edit_file", {"path": "coerce.py",
                                        "edits": [{"old_text": "     2\tbeta", "new_text": "BETA"}]})
        self.assertIn("line-number prefix was removed", r.text)
        self.assertEqual((self.root / "coerce.py").read_text(encoding="utf-8"), "ALPHA\nBETA\n")

    @case(SERVER, "edit_file", "a file that is not valid UTF-8 keeps the bytes the model could not read")
    def test_docs__edit_non_utf8(self):
        (self.root / "latin.txt").write_bytes(b"caf\xc3\xa9 notes\nsecond line\n\x80\x81 stray bytes\n")
        r = self.srv.call("edit_file", {"path": "latin.txt", "force": True,
                                        "edits": [{"old_text": "second line", "new_text": "SECOND LINE"}]})
        self.assertIn("1 edit(s) applied", r.text)
        self.assertEqual((self.root / "latin.txt").read_bytes(),
                         b"caf\xc3\xa9 notes\nSECOND LINE\n\x80\x81 stray bytes\n")

    @case(SERVER, "read_file", "unreadable bytes are shown and named, not silently rewritten")
    def test_docs__read_non_utf8(self):
        (self.root / "odd.txt").write_bytes(b"readable line\n\x80\x81 not utf-8\n")
        r = self.srv.call("read_file", {"path": "odd.txt"})
        self.assertIn("readable line", r.text)
        self.assertIn("not valid UTF-8", r.text)

    @case(SERVER, "edit_file", "an empty old_text and a missing file are both refused")
    def test_docs__edit_bad_input(self):
        r = self.srv.call("edit_file", {"path": "notes/plan.md", "edits": [{"old_text": "", "new_text": "x"}]})
        self.assertIn("old_text cannot be empty", r.text)
        r = self.srv.call("edit_file", {"path": "never.txt", "edits": [{"old_text": "a", "new_text": "b"}]})
        self.assertIn("not found", r.text)
        self.assertIn("create_file", r.text)

    @case(SERVER, "read_file", "a large source file comes back as an outline with line numbers")
    def test_docs__read_outline(self):
        write(self.root, "big.py", "\n".join(f"def f{i}(a):\n    return a + {i}\n" for i in range(800)))
        r = self.srv.call("read_file", {"path": "big.py"})
        self.assertIn("# File outline for big.py", r.text)
        self.assertIn("Do NOT retry this call", r.text)
        self.assertIn("    1  def  def f0(a)", r.text)
        self.assertIn("more are not shown", r.text)

    @case(SERVER, "read_file", "the outline line count agrees with the range reader")
    def test_docs__outline_and_range_agree(self):
        write(self.root, "count.py", "\n".join(f"v{i} = {i}" for i in range(2000)) + "\n")
        outline = self.srv.call("read_file", {"path": "count.py"}).text
        total = int(re.search(r"(\d+) lines", outline).group(1))
        last = self.srv.call("read_file", {"path": "count.py", "start_line": total}).text
        self.assertIn(f"{total}\tv{total - 1} =", last)
        self.assertNotIn("continue with start_line", last)

    @case(SERVER, "read_file", "a range past the end is an error, not an empty answer")
    def test_docs__read_range_past_end(self):
        r = self.srv.call("read_file", {"path": "range.txt", "start_line": 500})
        self.assertIn("past the end", r.text)

    @case(SERVER, "read_file", "a range longer than the cap names the line to continue from")
    def test_docs__read_range_cap(self):
        write(self.root, "wide.txt", "\n".join("x" * 200 for _ in range(200)))
        r = self.srv.call("read_file", {"path": "wide.txt", "start_line": 1, "end_line": 200})
        self.assertIn("the range is longer than 20000 characters", r.text)
        self.assertIn("Continue with start_line=", r.text)

    @case(SERVER, "read_file", "a 0 start line and an end below the start are clamped")
    def test_docs__read_range_clamped(self):
        r = self.srv.call("read_file", {"path": "range.txt", "start_line": 0, "end_line": 1})
        self.assertIn("     1\tl1", r.text)
        r = self.srv.call("read_file", {"path": "range.txt", "start_line": 5, "end_line": 2})
        self.assertIn("     5\tl5", r.text)

    @case(SERVER, "read_file", "a range inside a symbol says which symbol it came from")
    def test_docs__read_range_names_the_symbol(self):
        write(self.root, "sym.py", "import os\n\n\nclass Hub:\n    def start(self):\n        self.ready = True\n        self.count = 0\n")
        r = self.srv.call("read_file", {"path": "sym.py", "start_line": 6, "end_line": 7})
        self.assertIn("are inside def start", r.text)
        r = self.srv.call("read_file", {"path": "sym.py", "start_line": 1, "end_line": 1})
        self.assertNotIn("are inside", r.text)

    @case(SERVER, "edit_file", "the golden-diff fixtures produce the expected file")
    def test_docs__edit_evals(self):
        fixtures = sorted(p for p in EVALS.iterdir() if p.is_dir())
        self.assertGreaterEqual(len(fixtures), 4)
        for fixture in fixtures:
            name = f"evals/{fixture.name}.txt"
            write(self.root, name, (fixture / "before.txt").read_text(encoding="utf-8"))
            edits = json.loads((fixture / "edits.json").read_text(encoding="utf-8"))
            r = self.srv.call("edit_file", {"path": name, "edits": edits, "force": True})
            self.assertTrue(r.ok, f"{fixture.name}: {r.text}")
            got = (self.root / name).read_text(encoding="utf-8")
            self.assertEqual(got, (fixture / "after.txt").read_text(encoding="utf-8"), fixture.name)

    @case(SERVER, "edit_file", "a file this session did not create is edited on the first call")
    def test_docs__edit_needs_force(self):
        write(self.root, "user_notes.txt", "alpha\nbeta\n")
        r = self.srv.call("edit_file", {"path": "user_notes.txt",
                                        "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]})
        self.assertIn("1 edit(s) applied", r.text)
        self.assertEqual((self.root / "user_notes.txt").read_text(encoding="utf-8"), "ALPHA\nbeta\n")

    @case(SERVER, "delete_file", "a file this session did not create is deleted on the first call")
    def test_docs__delete_needs_force(self):
        write(self.root, "keep.txt", "the user's file")
        r = self.srv.call("delete_file", {"path": "keep.txt"})
        self.assertIn("bytes removed", r.text)
        self.assertFalse((self.root / "keep.txt").exists())

    @case(SERVER, "delete_file", "a file created here is deleted without force")
    def test_docs__delete_own_file(self):
        self.srv.call("create_file", {"path": "scratch/x.md", "content": "x"})
        r = self.srv.call("delete_file", {"path": "scratch/x.md"})
        self.assertIn("bytes removed", r.text)
        self.assertFalse((self.root / "scratch" / "x.md").exists())

    @case(SERVER, "write_file", "overwriting a file this session did not create proceeds")
    def test_docs__write_needs_force(self):
        write(self.root, "notes/other.md", "old")
        r = self.srv.call("write_file", {"path": "notes/other.md", "content": "new"})
        self.assertIn("saved", r.text)
        self.assertEqual((self.root / "notes" / "other.md").read_text(encoding="utf-8"), "new")

    @case(SERVER, "write_file", "the session state file the agent maintains is writable without force")
    def test_docs__state_file_is_allowed(self):
        write(self.root, ".mcp_test_state.json", '{"loop": 1}')
        r = self.srv.call("write_file", {"path": ".mcp_test_state.json", "content": '{"loop": 2}'})
        self.assertIn("saved", r.text)
        self.assertEqual((self.root / ".mcp_test_state.json").read_text(encoding="utf-8"), '{"loop": 2}')

    @case(SERVER, "list_directory", "a directory with more than 200 entries is cut short and says so")
    def test_docs__list_truncated(self):
        for i in range(210):
            write(self.root, f"many/f{i}.txt", "x")
        rows = self.srv.call("list_directory", {"path": "many"}).data["entries"]
        self.assertEqual(len(rows), 201)
        self.assertEqual(rows[-1]["type"], "truncated")

    @case(SERVER, "list_directory", "a path above the root is refused")
    def test_docs__list_outside_root(self):
        r = self.srv.call("list_directory", {"path": "/etc"})
        self.assertIn("outside the workspace root", r.text)

    @case(SERVER, "delete_file", "a directory is not a document to delete")
    def test_docs__delete_directory(self):
        r = self.srv.call("delete_file", {"path": "notes"})
        self.assertIn("not found", r.text.lower())
        self.assertTrue((self.root / "notes").is_dir())

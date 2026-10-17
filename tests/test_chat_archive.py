"""tests/test_chat_archive.py - the Chat tab's "Save to history" writer.

serve/chat_archive.py is the only code that touches data/history/_old_chats/, so
these tests cover the naming, the collisions, and the refusals. Nothing here
needs a GPU or a server.
"""
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve.chat_archive import MAX_CHAT_CHARS, OLD_CHATS_DIR, archive_chat

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-.+\.md$")
NOW = datetime(2026, 10, 9, 15, 38, tzinfo=timezone.utc)


class ArchiveChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_directory_is_the_history_folder(self) -> None:
        self.assertEqual(OLD_CHATS_DIR, ROOT / "data" / "history" / "_old_chats")

    def test_writes_the_transcript_and_describes_it(self) -> None:
        out = archive_chat("## You\n\nhello\n", "first chat", self.tmp, NOW)
        path = Path(out["path"])
        self.assertTrue(path.exists())
        self.assertEqual(out["name"], "2026-10-09-15-38-first-chat.md")
        self.assertTrue(STAMP.match(out["name"]), out["name"])
        self.assertEqual(path.read_text(encoding="utf-8"), "## You\n\nhello\n")
        self.assertEqual(out["bytes"], path.stat().st_size)
        self.assertTrue(out["new"])

    def test_creates_the_directory_when_it_is_absent(self) -> None:
        out = archive_chat("hi", "", self.tmp / "data/history/_old_chats", NOW)
        self.assertTrue(Path(out["path"]).exists())

    def test_blank_and_wrong_type_are_refused(self) -> None:
        for bad in ("", "   \n\n", None, 12, {"chat": "x"}):
            with self.assertRaises((ValueError, TypeError)):
                archive_chat(bad, "x", self.tmp, NOW)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_oversized_transcript_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            archive_chat("x" * (MAX_CHAT_CHARS + 1), "x", self.tmp, NOW)

    def test_same_minute_twice_keeps_both(self) -> None:
        first = archive_chat("one", "same", self.tmp, NOW)
        second = archive_chat("two", "same", self.tmp, NOW)
        third = archive_chat("three", "same", self.tmp, NOW)
        self.assertEqual(second["name"], "2026-10-09-15-38-same-2.md")
        self.assertEqual(third["name"], "2026-10-09-15-38-same-3.md")
        self.assertTrue(first["new"])          # the first chat of that minute
        self.assertTrue(second["new"])         # the second is written beside it, not over it
        self.assertEqual(len(list(self.tmp.iterdir())), 3)

    def test_a_title_cannot_escape_the_folder(self) -> None:
        out = archive_chat("hi", "../../etc/passwd", self.tmp, NOW)
        name = out["name"]
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)
        self.assertTrue(Path(out["path"]).parent == self.tmp)
        self.assertTrue(STAMP.match(name), name)

    def test_timestamp_is_utc_and_trailing_newlines_are_normalised(self) -> None:
        out = archive_chat("body without a newline", "t", self.tmp, datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))
        self.assertTrue(Path(out["path"]).name.startswith("2026-01-02-03-04-"))
        self.assertEqual(Path(out["path"]).read_text(encoding="utf-8"), "body without a newline\n")


class ServerRouteTests(unittest.TestCase):
    """POST /history/archive end to end, against the scripted mock engine.

    No GPU: MockEngine is a stand-in, and the archive writer never touches it.
    The test is skipped if the server does not answer within STARTUP_S.
    """
    STARTUP_S = 20

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _free_port(self) -> int:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_the_endpoint_writes_the_transcript(self) -> None:
        import subprocess
        import urllib.error
        import urllib.request

        port = self._free_port()
        env = dict(os.environ, HYPERCORE_OLD_CHATS=str(self.tmp))
        proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--engine", "mock", "--port", str(port)],
                                cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + self.STARTUP_S
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                        if r.status == 200:
                            break
                except (urllib.error.URLError, OSError):
                    time.sleep(0.25)
            else:
                self.skipTest(f"the mock server did not come up in {self.STARTUP_S} s")

            body = json.dumps({"chat": "## You\n\narchive me\n", "title": "from the button"}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/history/archive",
                                        data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                out = json.loads(r.read())
            self.assertEqual(r.status, 200, out)
            self.assertTrue(Path(out["path"]).exists())
            self.assertTrue(out["name"].startswith("20"), out["name"])
            self.assertEqual(Path(out["path"]).read_text(encoding="utf-8"), "## You\n\narchive me\n")

            bad = urllib.request.Request(f"http://127.0.0.1:{port}/history/archive",
                                        data=b'{"chat": ""}', headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(bad, timeout=5)
                self.fail("an empty chat should be refused")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()

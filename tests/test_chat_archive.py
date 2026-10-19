"""tests/test_chat_archive.py - the Chat tab's "Save to history" writer.

serve/chat_archive.py is the only code that touches data/history/_old_chats/, so
these tests cover the naming, the collisions, the refusals, and the distillation
hook that runs tools/build_history.py behind an archive. Nothing here needs a GPU
or a real model.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve import chat_archive
from serve.chat_archive import MAX_CHAT_CHARS, OLD_CHATS_DIR, archive_chat

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-.+\.md$")
NOW = datetime(2026, 10, 9, 15, 38, tzinfo=timezone.utc)

#: The key the test's own mock server requires. serve/server.py takes its key from the
#: command line or, failing that, from the run config it picks up - and a run config the
#: Build tab wrote has "0000" in it. Naming the key on the command line keeps these
#: tests the same on a machine that has built a quant and one that has not.


API_KEY = "archive-test-key"


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
        self.port = 0

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _free_port(self) -> int:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _call(self, path: str, body: bytes | None = None, key: str | None = API_KEY) -> tuple[int, dict]:
        """One request to this test's server. key=None sends no credential at all."""
        import urllib.error
        import urllib.request
        headers = {"Authorization": "Bearer " + key} if key else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_the_endpoint_writes_the_transcript(self) -> None:
        import subprocess
        import urllib.error
        import urllib.request

        port = self._free_port()
        stub = self.tmp / "distil_stub.py"
        stub.write_text("print('distil ok: stub builder')\n", encoding="utf-8")
        env = dict(os.environ, HYPERCORE_OLD_CHATS=str(self.tmp),
                   HYPERCORE_DISTIL_SCRIPT=str(stub))
        proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--engine", "mock",
                                "--port", str(port), "--api-key", API_KEY],
                                cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.port = port
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
            status, out = self._call("/history/archive", body)
            self.assertEqual(status, 200, out)
            self.assertTrue(Path(out["path"]).exists())
            self.assertTrue(out["name"].startswith("20"), out["name"])
            self.assertEqual(Path(out["path"]).read_text(encoding="utf-8"), "## You\n\narchive me\n")
            # the archive is written, the rebuild is started behind the response
            self.assertIn("distil", out, "the archive response says nothing about the rebuild")
            self.assertTrue(out["distil"].get("queued"), out["distil"])

            code, status = self._call("/history/distil")
            self.assertEqual(code, 200, status)
            self.assertTrue(status["auto"], "the status does not report the automatic pass")
            deadline = time.time() + 15
            while time.time() < deadline and status["running"]:
                time.sleep(0.2)
                _, status = self._call("/history/distil")
            self.assertFalse(status["running"], "the rebuild never finished")
            self.assertTrue(status["last"]["ok"], status["last"])
            self.assertIn("distil ok", status["last"]["output"])

            # an empty chat is a 400, and a request with no key never reaches the writer
            code, _ = self._call("/history/archive", b'{"chat": ""}')
            self.assertEqual(code, 400, "an empty chat should be refused")
            code, body = self._call("/history/archive", b'{"chat": "keyless"}', key=None)
            self.assertEqual(code, 401, body)
            self.assertEqual(body["error"]["type"], "authentication_error", body)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


    def test_the_history_status_reports_the_folder(self) -> None:
        port = self._free_port()
        stub = self.tmp / "distil_stub.py"
        stub.write_text("print('distil ok')\n", encoding="utf-8")
        env = dict(os.environ, HYPERCORE_OLD_CHATS=str(self.tmp), HYPERCORE_DISTIL_SCRIPT=str(stub))
        proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--engine", "mock",
                                 "--port", str(port), "--api-key", API_KEY], cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.port = port
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
                self.fail("the mock server did not come up")

            archive_chat("## You\n\nstatus check\n", "status", Path(self.tmp), NOW)
            code, status = self._call("/history/status")
            self.assertEqual(code, 200, status)
            self.assertEqual(status["transcripts"], 1, status)
            self.assertIn("topics", status)
            self.assertIn("notes", status)
            self.assertTrue(status["newest"].startswith("20"), status["newest"])
            self.assertTrue(status["distil"]["auto"], "the status does not report the automatic pass")
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_an_empty_history_reports_zero_instead_of_failing(self) -> None:
        empty = Path(tempfile.mkdtemp())
        old, hist = chat_archive.OLD_CHATS_DIR, chat_archive.HIST
        chat_archive.OLD_CHATS_DIR, chat_archive.HIST = empty, empty
        try:
            status = chat_archive.history_status()
            self.assertEqual(status["transcripts"], 0, status)
            self.assertEqual(status["summaries"], 0, status)
            self.assertIsNone(status["newest"], "an empty folder has no newest transcript")
            self.assertIsNone(status["distilled"], "there is no index to read")
        finally:
            chat_archive.OLD_CHATS_DIR, chat_archive.HIST = old, hist
            import shutil
            shutil.rmtree(empty, ignore_errors=True)

    def test_a_helper_file_in_the_history_folder_is_not_a_transcript(self) -> None:
        folder = Path(tempfile.mkdtemp())
        (folder / "2026-10-17-23-12-real.md").write_text("## You\n\nreal chat\n", encoding="utf-8")
        (folder / "distil_stub.py").write_text("print('not a chat')\n", encoding="utf-8")
        (folder / "notes.txt").write_text("scratch\n", encoding="utf-8")
        old, hist = chat_archive.OLD_CHATS_DIR, chat_archive.HIST
        chat_archive.OLD_CHATS_DIR, chat_archive.HIST = folder, folder
        try:
            status = chat_archive.history_status()
            self.assertEqual(status["transcripts"], 1, status)
            self.assertEqual(status["newest"], "2026-10-17-23-12-real.md", status)
        finally:
            chat_archive.OLD_CHATS_DIR, chat_archive.HIST = old, hist
            import shutil
            shutil.rmtree(folder, ignore_errors=True)


class DistilHookTests(unittest.TestCase):
    """The standing task behind the archive button: distil, but never inside the request."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = self.tmp / "distil_stub.py"
        self.stub.write_text("print('distil ok: 3 transcripts')\n", encoding="utf-8")
        self._script = chat_archive.DISTIL_SCRIPT
        self._auto = chat_archive.AUTO_DISTIL
        chat_archive.DISTIL_SCRIPT = self.stub
        self._settle()

    def tearDown(self) -> None:
        self._settle()
        chat_archive.DISTIL_SCRIPT = self._script
        chat_archive.AUTO_DISTIL = self._auto
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _settle(self) -> None:
        for _ in range(120):
            if not chat_archive.distil_status()["running"]:
                return
            time.sleep(0.1)
        self.fail("a queued rebuild never finished")

    def test_the_builder_the_hook_runs_by_default_is_in_the_repository(self) -> None:
        self.assertTrue((ROOT / "tools" / "build_history.py").is_file())
        self.assertTrue(chat_archive.AUTO_DISTIL, "the automatic pass should be on by default")

    def test_run_builder_runs_it_and_reports_the_result(self) -> None:
        out = chat_archive.run_builder()
        self.assertTrue(out["ok"], out)
        self.assertIn("distil ok", out["output"])
        self.assertIsNone(out["error"])
        self.assertGreaterEqual(out["seconds"], 0.0)
        self.assertEqual(out["script"], str(self.stub))
        self.assertTrue(out["started"].startswith("20"), out["started"])

    def test_a_missing_builder_is_a_result_not_an_exception(self) -> None:
        chat_archive.DISTIL_SCRIPT = self.tmp / "not-there.py"
        out = chat_archive.run_builder()
        self.assertFalse(out["ok"])
        self.assertIn("not at", out["error"])

    def test_two_archives_do_not_start_two_rebuilds(self) -> None:
        slow = self.tmp / "slow.py"
        slow.write_text("import time\nprint('sleeping')\ntime.sleep(2)\n", encoding="utf-8")
        chat_archive.DISTIL_SCRIPT = slow
        first = chat_archive.queue_distil()
        self.assertTrue(first["queued"], first)
        second = chat_archive.queue_distil()
        self.assertFalse(second["queued"], "a second rebuild started while one was running")
        self.assertIn("already running", second["reason"])
        self._settle()
        last = chat_archive.distil_status()["last"]
        self.assertTrue(last and last["ok"], last)

    def test_auto_distil_off_leaves_the_archive_as_a_plain_save(self) -> None:
        chat_archive.AUTO_DISTIL = False
        out = chat_archive.queue_distil()
        self.assertFalse(out["queued"])
        self.assertIn("HYPERCORE_AUTO_DISTIL", out["reason"])
        self.assertFalse(chat_archive.distil_status()["running"])

    def test_archiving_queues_the_rebuild(self) -> None:
        written = archive_chat("## You\n\narchive me\n", "hook", self.tmp, NOW)
        self.assertTrue(Path(written["path"]).exists())
        queued = chat_archive.queue_distil()
        self.assertTrue(queued["queued"], queued)
        self._settle()
        self.assertTrue(chat_archive.distil_status()["last"]["ok"])


if __name__ == "__main__":
    unittest.main()

"""The download server: extract_media, download_file, download_page_media,
batch_download, file_info, list_downloads, remove_downloads.

    python3 -m unittest serve.mcp.tests.test_scrape

The server is pointed at SCRAPE_DIR set to a temporary folder, and the pages it fetches
come from a local fixture server rather than the internet, so the results are the same
every run. Only one case (a real public page) needs the network and is skipped when
HYPERCORE_TEST_LIVE=0.
"""
from __future__ import annotations

import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from serve.mcp.tests.harness import McpServer, case, entry, live

SERVER = "scrape"
ADVERTISED = ["batch_download", "download_file", "download_page_media", "extract_media",
              "file_info", "list_downloads", "remove_downloads"]

PNG = b""
try:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "navy").save(buf, format="PNG")
    PNG = buf.getvalue()
except Exception:  # noqa: BLE001 - without Pillow the fixture is a stub PNG
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
           b"\x00\x00\x00\x1f\x01\x0c\x00\x00\x00\x00IDATx\x9cc\x00\x01\x00\x00\x05\x00\x09"
           b"\r\x02\x00\x00\x00\x00\x00\x00\x00\x00IEND\xaeB`\x82")

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect width="9"/></svg>'
TEXT = b"the fetched text body, for the docs tools to read\n"
BIG = b"x" * (3 * 1024 * 1024)

PAGE = b"""<!doctype html>
<html><head>
<style>body{background-image:url("/bg.png")}</style>
</head><body>
<h1>Fixture page</h1>
<img src="/img.png" alt="a png" width="40" height="30">
<img src="/tiny.png" width="6" height="6">
<img src="/gone.png">
<a href="/doc.txt">text</a>
<iframe src="https://player.vimeo.com/video/110256895"></iframe>
<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
<video poster="/poster.png"><source src="/clip.mp4" type="video/mp4"></video>
<audio src="/clip.mp3"></audio>
<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">a plain link, not media</a>
</body></html>
"""


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        body, ctype = {
            "/page.html": (PAGE, "text/html"),
            "/img.png": (PNG, "image/png"),
            "/tiny.png": (PNG, "image/png"),
            "/bg.png": (PNG, "image/png"),
            "/poster.png": (PNG, "image/png"),
            "/doc.txt": (TEXT, "text/plain"),
            "/big.bin": (BIG, "application/octet-stream"),
        }.get(self.path, (b"", None))
        if ctype is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):   # the client read what it needed and hung up
            pass

    def log_message(self, *args):  # keep the test output clean
        pass


class Scrape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.http_thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"
        cls.srv = McpServer(entry("web_scrape_mcp.py"), env={
            "SCRAPE_DIR": str(cls.dir),
            "SCRAPE_BLOCK_PRIVATE": "0",
            "SCRAPE_MAX_FILE_MB": "5",
            "SCRAPE_TIMEOUT_S": "20",
        })
        cls.strict = McpServer(entry("web_scrape_mcp.py"), env={"SCRAPE_DIR": str(cls.dir / "strict")})

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.strict.close()
        cls.http.shutdown()
        cls.http.server_close()
        cls.tmp.cleanup()

    @case(SERVER, "tools", "advertises all 8 tools")
    def test_scrape__advertised_tools(self):
        self.assertEqual(self.srv.tools, sorted(ADVERTISED))

    # ---- status -------------------------------------------------------------
    @case(SERVER, "list_downloads", "the listing carries the folder, the count, and the size limit")
    def test_scrape__list_header(self):
        out = self.srv.call("list_downloads", {"limit": 50}).data
        self.assertEqual(out["download_dir"], str(self.dir))
        self.assertEqual(out["max_file_mb"], 5)
        self.assertFalse(out["private_ips_blocked"])
        self.assertEqual(out["files_in_folder"], out["total"])   # no filter: the header and the list agree

    @case(SERVER, "list_downloads", "the default server blocks private addresses")
    def test_scrape__list_header_default_blocks_private(self):
        out = self.strict.call("list_downloads", {}).data
        self.assertTrue(out["private_ips_blocked"])

    # ---- extract_media ------------------------------------------------------
    @case(SERVER, "extract_media", "finds img tags, CSS backgrounds, and video embeds")
    def test_scrape__extract_media(self):
        out = self.srv.call("extract_media", {"url": self.base + "/page.html", "max_results": 20}).data
        urls = [m["url"] for m in out["media"]]
        self.assertIn(self.base + "/img.png", urls)
        self.assertIn("https://player.vimeo.com/video/110256895", urls)
        self.assertTrue(any("youtube" in u for u in urls))
        self.assertIn(self.base + "/clip.mp3", urls)                      # audio
        self.assertNotIn(self.base + "/bg.png", urls)                     # inline CSS is not a background source

    @case(SERVER, "extract_media", "media_type filters what is returned")
    def test_scrape__extract_media_type(self):
        out = self.srv.call("extract_media", {"url": self.base + "/page.html", "media_type": "image"}).data
        self.assertTrue(out["media"])
        self.assertTrue(all(m["type"] == "image" for m in out["media"]))

    @case(SERVER, "extract_media", "min_width drops the small images")
    def test_scrape__extract_media_min_width(self):
        out = self.srv.call("extract_media", {"url": self.base + "/page.html", "min_width": 20}).data
        self.assertFalse(any(m["url"].endswith("tiny.png") for m in out["media"]))

    @case(SERVER, "extract_media", "a page with no media says so")
    def test_scrape__extract_media_none(self):
        out = self.srv.call("extract_media", {"url": self.base + "/doc.txt"}).data
        self.assertEqual(out["total_found"], 0)

    @case(SERVER, "extract_media", "a private address is refused by the default server")
    def test_scrape__extract_media_private_refused(self):
        r = self.strict.call("extract_media", {"url": self.base + "/page.html"})
        self.assertFalse(r.ok)
        self.assertIn("public internet", r.text.lower())

    # ---- download_file ------------------------------------------------------
    @case(SERVER, "download_file", "downloads a file and reports its path, size and type")
    def test_scrape__download_file(self):
        r = self.srv.call("download_file", {"url": self.base + "/doc.txt", "filename": "doc.txt",
                                           "subfolder": "fx"})
        self.assertTrue(r.ok)
        out = r.data
        self.assertTrue((self.dir / "fx/doc.txt").is_file())
        self.assertEqual(out["bytes"], len(TEXT))
        self.assertEqual(out["mime_type"], "text/plain")

    @case(SERVER, "download_file", "a 404 is reported as an error, not a crash")
    def test_scrape__download_missing(self):
        r = self.srv.call("download_file", {"url": self.base + "/gone.png"})
        self.assertFalse(r.ok)
        self.assertIn("404", r.text)

    @case(SERVER, "download_file", "max_mb refuses an oversized file")
    def test_scrape__download_too_big(self):
        r = self.srv.call("download_file", {"url": self.base + "/big.bin", "max_mb": 1})
        self.assertFalse(r.ok)
        self.assertIn("MB", r.text)

    @case(SERVER, "download_file", "a non-http scheme is refused")
    def test_scrape__download_scheme(self):
        r = self.srv.call("download_file", {"url": "ftp://example.com/x"})
        self.assertFalse(r.ok)

    @case(SERVER, "download_file", "overwrite=false reports the file as skipped, not overwritten")
    def test_scrape__download_no_overwrite(self):
        self.srv.call("download_file", {"url": self.base + "/doc.txt", "filename": "keep.txt",
                                        "subfolder": "fx"})
        r = self.srv.call("download_file", {"url": self.base + "/img.png", "filename": "keep.txt",
                                           "subfolder": "fx", "overwrite": False})
        self.assertTrue(r.ok)
        self.assertTrue(r.data["skipped"])
        self.assertEqual((self.dir / "fx/keep.txt").read_bytes(), TEXT)

    # ---- download_page_media ----------------------------------------------
    @case(SERVER, "download_page_media", "downloads the page's media into its own subfolder")
    def test_scrape__download_page_media(self):
        r = self.srv.call("download_page_media", {"url": self.base + "/page.html",
                                                 "media_type": "image", "max_items": 3,
                                                 "subfolder": "media"})
        self.assertTrue(r.ok)
        out = r.data
        self.assertGreater(out["downloaded"], 0)
        files = list((self.dir / "media").iterdir())
        self.assertTrue(files)
        self.assertTrue(all(f.stat().st_size > 0 for f in files))

    # ---- batch_download -----------------------------------------------------
    @case(SERVER, "batch_download", "downloads several addresses and counts the failures")
    def test_scrape__batch_download(self):
        r = self.srv.call("batch_download", {"urls": [self.base + "/doc.txt", self.base + "/img.png",
                                                     self.base + "/nope"],
                                            "subfolder": "batch"}).data
        self.assertEqual(r["downloaded"], 2)
        self.assertEqual(r["failed"], 1)
        self.assertTrue((self.dir / "batch/doc.txt").is_file())

    @case(SERVER, "batch_download", "an empty list is an error")
    def test_scrape__batch_empty(self):
        self.assertFalse(self.srv.call("batch_download", {"urls": []}).ok)

    # ---- file_info ----------------------------------------------------------
    @case(SERVER, "file_info", "reports size, type, and image dimensions")
    def test_scrape__file_info(self):
        self.srv.call("download_file", {"url": self.base + "/img.png", "filename": "pic.png",
                                        "subfolder": "fx"})
        out = self.srv.call("file_info", {"path": "fx/pic.png"}).data
        self.assertEqual(out["bytes"], len(PNG))
        self.assertEqual(out["mime_type"], "image/png")
        self.assertEqual(out["dimensions"], {"width": 40, "height": 30})

    @case(SERVER, "file_info", "a path that was not downloaded is an error")
    def test_scrape__file_info_missing(self):
        self.assertFalse(self.srv.call("file_info", {"path": "fx/never.png"}).ok)

    # ---- list_downloads -----------------------------------------------------
    @case(SERVER, "list_downloads", "lists files with size, type and modified time")
    def test_scrape__list(self):
        out = self.srv.call("list_downloads", {"limit": 50}).data
        self.assertGreater(out["total"], 3)
        row = out["files"][0]
        for key in ("path", "bytes", "mime", "modified"):
            self.assertIn(key, row)

    @case(SERVER, "list_downloads", "subfolder and mime filters narrow the list")
    def test_scrape__list_filters(self):
        out = self.srv.call("list_downloads", {"subfolder": "fx", "mime_type": "image/*"}).data
        self.assertTrue(out["files"])
        self.assertTrue(all(f["mime"].startswith("image/") for f in out["files"]))
        self.assertTrue(all("fx/" in f["path"] for f in out["files"]))

    @case(SERVER, "list_downloads", "sort by size puts the largest first")
    def test_scrape__list_sort(self):
        out = self.srv.call("list_downloads", {"sort": "size", "limit": 5}).data
        sizes = [f["bytes"] for f in out["files"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    # ---- remove_downloads ---------------------------------------------------
    @case(SERVER, "remove_downloads", "deletes a file and reports the bytes removed")
    def test_scrape__remove_file(self):
        self.srv.call("download_file", {"url": self.base + "/doc.txt", "filename": "gone.txt",
                                        "subfolder": "trash"})
        out = self.srv.call("remove_downloads", {"path": "trash/gone.txt"}).data
        self.assertEqual(out["removed"], 1)
        self.assertEqual(out["bytes"], len(TEXT))
        self.assertFalse((self.dir / "trash/gone.txt").exists())

    @case(SERVER, "remove_downloads", "deletes a whole subfolder")
    def test_scrape__remove_folder(self):
        self.srv.call("batch_download", {"urls": [self.base + "/doc.txt", self.base + "/img.png"],
                                         "subfolder": "whole"})
        out = self.srv.call("remove_downloads", {"path": "whole"}).data
        self.assertEqual(out["removed"], 2)
        self.assertFalse((self.dir / "whole").exists())

    @case(SERVER, "remove_downloads", "a path outside the download folder is refused")
    def test_scrape__remove_outside(self):
        r = self.srv.call("remove_downloads", {"path": "../outside"})
        self.assertFalse(r.ok)

    # ---- one case against a real page ---------------------------------------
    @case(SERVER, "extract_media", "a real public page yields its media")
    def test_scrape__live_page(self):
        live("no network")
        r = self.srv.call("extract_media", {"url": "https://www.w3.org/standards/", "max_results": 5},
                         timeout=60)
        self.assertTrue(r.ok)
        self.assertGreater(r.data["total_found"], 0)

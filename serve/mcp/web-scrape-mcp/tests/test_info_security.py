import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from webscrape.config import Settings
from webscrape import service

from conftest import send_bytes


def test_file_info_jpeg_dimensions(settings: Settings, tmp_path: Path):
    path = tmp_path / "pic.jpg"
    Image.new("RGB", (1200, 800), color="orange").save(path, format="JPEG")
    result = service.get_downloaded_file_info(settings, str(path))
    assert result["status"] == "ok"
    assert result["mime_type"] == "image/jpeg"
    assert result["dimensions"] == {"width": 1200, "height": 800}
    assert result["extension"] == ".jpg"


def test_file_info_mp4_uses_ffprobe(settings: Settings, tmp_path: Path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not a real mp4")
    fake = {
        "streams": [{"width": 1920, "height": 1080, "codec_type": "video"}],
        "format": {"duration": "3.5"},
    }

    class Proc:
        returncode = 0
        stdout = json.dumps(fake)

    with patch("webscrape.media.shutil.which", return_value="/usr/bin/ffprobe"):
        with patch("webscrape.media.subprocess.run", return_value=Proc()):
            result = service.get_downloaded_file_info(settings, str(path))
    assert result["status"] == "ok"
    assert result["dimensions"] == {"width": 1920, "height": 1080}
    assert result["duration_sec"] == 3.5


def test_list_downloads_sort_and_count(settings: Settings, tmp_path: Path):
    for i in range(5):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x" * (i + 1), encoding="utf-8")
    listed = service.list_downloads(settings, sort="name", limit=100)
    assert listed["status"] == "ok"
    assert listed["total"] == 5
    assert listed["truncated"] is False
    names = [row["path"] for row in listed["files"]]
    assert names == ["f0.txt", "f1.txt", "f2.txt", "f3.txt", "f4.txt"]
    sized = service.list_downloads(settings, sort="size", limit=2)
    assert sized["truncated"] is True
    assert sized["files"][0]["path"] == "f4.txt"


def test_timeout(settings: Settings, http_server):
    settings.timeout_sec = 1
    server, base = http_server

    def stall(handler):
        import time

        time.sleep(8)
        send_bytes(handler, b"late", "text/plain")

    server.routes["/stall"] = stall
    result = asyncio.run(service.download_file(settings, url=f"{base}/stall"))
    assert result["status"] == "error"
    assert result["code"] == "TIMEOUT"


def test_ssrf_blocks_loopback(tmp_path: Path):
    settings = Settings(allowed_dir=tmp_path, block_private_ips=True)
    result = asyncio.run(service.download_file(settings, url="http://127.0.0.1:8080/secret"))
    assert result["status"] == "error"
    assert result["code"] == "ACCESS_DENIED"


def test_retry_429_then_success(settings: Settings, http_server, jpeg: bytes):
    server, base = http_server
    hits = {"n": 0}

    def flaky(handler):
        hits["n"] += 1
        if hits["n"] <= 2:
            handler.send_response(429)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Content-Length", "4")
            handler.end_headers()
            handler.wfile.write(b"slow")
            return
        send_bytes(handler, jpeg, "image/jpeg")

    server.routes["/flaky.jpg"] = flaky
    result = asyncio.run(service.download_file(settings, url=f"{base}/flaky.jpg"))
    assert result["status"] == "ok"
    assert hits["n"] == 3
    assert Path(result["file_path"]).read_bytes() == jpeg

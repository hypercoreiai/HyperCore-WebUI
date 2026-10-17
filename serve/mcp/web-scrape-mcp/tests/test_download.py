import asyncio
from pathlib import Path

from webscrape.config import Settings
from webscrape import service
from webscrape.sandbox import sanitize_filename
from webscrape.errors import ACCESS_DENIED, ScrapeError

from conftest import send_bytes, send_redirect, send_status, jpeg_bytes


def test_download_jpeg(settings: Settings, http_server, jpeg: bytes):
    server, base = http_server
    server.routes["/cat.jpg"] = lambda h: send_bytes(h, jpeg, "image/jpeg")
    result = asyncio.run(
        service.download_file(settings, url=f"{base}/cat.jpg", filename="cat.jpg")
    )
    assert result["status"] == "ok"
    path = Path(result["file_path"])
    assert path.is_file()
    assert path.read_bytes() == jpeg
    assert result["bytes"] == len(jpeg)
    assert result["mime_type"] == "image/jpeg"
    assert result["http_status"] == 200


def test_file_too_large_no_file(settings: Settings, http_server, jpeg: bytes):
    server, base = http_server

    def huge(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(600 * 1024 * 1024))
        handler.end_headers()
        handler.wfile.write(b"x" * 1024)

    server.routes["/huge.bin"] = huge
    result = asyncio.run(
        service.download_file(settings, url=f"{base}/huge.bin", max_size_mb=100)
    )
    assert result["status"] == "error"
    assert result["code"] == "FILE_TOO_LARGE"
    assert list(settings.allowed_dir.glob("*")) == []


def test_http_404_no_file(settings: Settings, http_server):
    server, base = http_server
    server.routes["/missing"] = lambda h: send_status(h, 404, b"nope")
    result = asyncio.run(service.download_file(settings, url=f"{base}/missing"))
    assert result["status"] == "error"
    assert result["code"] == "HTTP_404"
    assert list(settings.allowed_dir.glob("*")) == []


def test_path_traversal_denied(settings: Settings):
    try:
        sanitize_filename("../../etc/passwd")
        raised = False
    except ScrapeError as exc:
        raised = True
        assert exc.code == ACCESS_DENIED
    assert raised
    result = asyncio.run(
        service.download_file(
            settings, url="https://example.com/x", filename="../../etc/passwd"
        )
    )
    assert result["status"] == "error"
    assert result["code"] == "ACCESS_DENIED"


def test_redirect_chain_five_hops(settings: Settings, http_server, jpeg: bytes):
    server, base = http_server
    server.routes["/r1"] = lambda h: send_redirect(h, "/r2")
    server.routes["/r2"] = lambda h: send_redirect(h, "/r3")
    server.routes["/r3"] = lambda h: send_redirect(h, "/r4")
    server.routes["/r4"] = lambda h: send_redirect(h, "/r5")
    server.routes["/r5"] = lambda h: send_redirect(h, "/final.jpg")
    server.routes["/final.jpg"] = lambda h: send_bytes(h, jpeg, "image/jpeg")
    result = asyncio.run(service.download_file(settings, url=f"{base}/r1"))
    assert result["status"] == "ok"
    assert result["final_url"].endswith("/final.jpg")
    assert Path(result["file_path"]).read_bytes() == jpeg


def test_generated_filename_when_url_has_no_name(settings: Settings, http_server, jpeg: bytes):
    server, base = http_server
    server.routes["/"] = lambda h: send_bytes(h, jpeg, "image/jpeg")
    result = asyncio.run(service.download_file(settings, url=f"{base}/"))
    assert result["status"] == "ok"
    name = Path(result["file_path"]).name
    assert "_" in name
    assert name.endswith(".jpg")

import asyncio
import time

from webscrape import service
from webscrape.config import Settings

from conftest import jpeg_bytes, png_bytes, send_bytes, send_status


def test_download_page_media_max_items(settings: Settings, http_server):
    server, base = http_server
    j1, j2, j3 = jpeg_bytes(color="red"), jpeg_bytes(color="green"), jpeg_bytes(color="blue")
    html = f"""<html><body>
    <img src="{base}/a.jpg">
    <img src="{base}/b.jpg">
    <img src="{base}/c.jpg">
    <video><source src="{base}/clip.mp4" type="video/mp4"></video>
    </body></html>"""
    server.routes["/page"] = lambda h: send_bytes(h, html.encode(), "text/html")
    server.routes["/a.jpg"] = lambda h: send_bytes(h, j1, "image/jpeg")
    server.routes["/b.jpg"] = lambda h: send_bytes(h, j2, "image/jpeg")
    server.routes["/c.jpg"] = lambda h: send_bytes(h, j3, "image/jpeg")
    server.routes["/clip.mp4"] = lambda h: send_bytes(h, b"fake-mp4-bytes", "video/mp4")

    result = asyncio.run(
        service.download_page_media(
            settings, url=f"{base}/page", max_items=4, subfolder="gallery"
        )
    )
    assert result["status"] == "ok"
    assert result["downloaded_count"] == 4
    assert result["failed_count"] == 0
    files = list((settings.allowed_dir / "gallery").iterdir())
    assert len([p for p in files if p.is_file() and not p.name.startswith(".tmp_")]) == 4

    limited = asyncio.run(
        service.download_page_media(
            settings, url=f"{base}/page", max_items=2, subfolder="two"
        )
    )
    assert limited["downloaded_count"] == 2
    two = list((settings.allowed_dir / "two").glob("*"))
    assert len(two) == 2


def test_batch_partial_failure(settings: Settings, http_server, jpeg: bytes):
    server, base = http_server
    png = png_bytes()
    server.routes["/1.jpg"] = lambda h: send_bytes(h, jpeg, "image/jpeg")
    server.routes["/2.jpg"] = lambda h: send_bytes(h, jpeg, "image/jpeg")
    server.routes["/3.png"] = lambda h: send_bytes(h, png, "image/png")
    server.routes["/4.jpg"] = lambda h: send_bytes(h, jpeg, "image/jpeg")
    server.routes["/missing"] = lambda h: send_status(h, 404)

    result = asyncio.run(
        service.batch_download(
            settings,
            urls=[
                f"{base}/1.jpg",
                f"{base}/2.jpg",
                f"{base}/3.png",
                f"{base}/4.jpg",
                f"{base}/missing",
            ],
        )
    )
    assert result["status"] == "ok"
    assert result["succeeded"] == 4
    assert result["failed"] == 1
    assert any(row.get("code") == "HTTP_404" for row in result["results"])


def test_batch_concurrency_cap(settings: Settings, http_server, jpeg: bytes):
    settings.concurrent = 2
    server, base = http_server

    def slow(handler):
        server.note_start()
        time.sleep(0.25)
        try:
            send_bytes(handler, jpeg, "image/jpeg")
        finally:
            server.note_end()

    for i in range(5):
        server.routes[f"/{i}.jpg"] = slow
    result = asyncio.run(
        service.batch_download(settings, urls=[f"{base}/{i}.jpg" for i in range(5)])
    )
    assert result["succeeded"] == 5
    assert server.max_active <= 2
    assert server.max_active >= 1

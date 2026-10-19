import asyncio

from webscrape import service
from webscrape.config import Settings
from webscrape.extract import extract_media

from conftest import send_bytes

GALLERY_HTML = """<!doctype html>
<html><body>
<img src="/photo.jpg" alt="Punch the macaque" width="800" height="600">
<img src="/photo.jpg" alt="duplicate">
<img src="rel/cat.png">
<video poster="/poster.jpg">
  <source src="/clip.mp4" type="video/mp4">
</video>
<audio src="/song.mp3"></audio>
<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
<div style="background-image: url('/bg.jpg')"></div>
<style>.hero { background: url("./hero.png"); }</style>
</body></html>
"""


def test_extract_media_tags_and_youtube():
    page = "https://example.com/gallery"
    result = extract_media(GALLERY_HTML, page, include_bg=True, max_results=50)
    assert result["status"] == "ok"
    types = {item["type"] for item in result["media"]}
    assert "image" in types
    assert "video" in types
    assert "audio" in types
    urls = [item["url"] for item in result["media"]]
    assert urls.count("https://example.com/photo.jpg") == 1
    assert "https://example.com/rel/cat.png" in urls
    assert "https://example.com/clip.mp4" in urls
    assert "https://example.com/song.mp3" in urls
    assert "https://example.com/bg.jpg" in urls
    assert "https://example.com/hero.png" in urls
    yt = [item for item in result["media"] if item.get("tag") == "iframe"]
    assert yt
    assert yt[0]["type"] == "video"
    assert "dQw4w9WgXcQ" in yt[0]["poster"]


def test_extract_media_relative_and_dedup():
    html = '<img src="a.jpg"><img src="./a.jpg"><img src="/a.jpg">'
    result = extract_media(html, "https://cdn.example.com/dir/page")
    urls = [item["url"] for item in result["media"]]
    assert urls == ["https://cdn.example.com/dir/a.jpg", "https://cdn.example.com/a.jpg"]


def test_extract_media_over_http(settings: Settings, http_server, jpeg: bytes):
    server, base = http_server
    html = GALLERY_HTML.encode()
    server.routes["/gallery"] = lambda h: send_bytes(h, html, "text/html")
    result = asyncio.run(service.extract_media_from_url(settings, url=f"{base}/gallery"))
    assert result["status"] == "ok"
    assert result["total_found"] >= 4

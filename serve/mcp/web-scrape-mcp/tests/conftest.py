"""Local HTTP server for download/extract tests."""

from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image
from webscrape.config import Settings


def jpeg_bytes(width: int = 32, height: int = 24, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="JPEG")
    return buf.getvalue()


def png_bytes(width: int = 16, height: int = 16) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="blue").save(buf, format="PNG")
    return buf.getvalue()


class TestHTTPServer(ThreadingHTTPServer):
    def __init__(self, routes: dict):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes = routes
        self.hits: dict[str, int] = {}
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def note_start(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def note_end(self) -> None:
        with self._lock:
            self.active -= 1

    def hit(self, path: str) -> int:
        with self._lock:
            self.hits[path] = self.hits.get(path, 0) + 1
            return self.hits[path]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        server: TestHTTPServer = self.server  # type: ignore[assignment]
        path = self.path.split("?", 1)[0]
        action = server.routes.get(path) or server.routes.get("*")
        if action is None:
            self.send_error(404)
            return
        server.hit(path)
        action(self)


@pytest.fixture
def jpeg() -> bytes:
    return jpeg_bytes()


@pytest.fixture
def png() -> bytes:
    return png_bytes()


@pytest.fixture
def http_server():
    routes: dict = {}
    server = TestHTTPServer(routes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        yield server, base
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        allowed_dir=tmp_path,
        max_file_size_mb=500,
        timeout_sec=5,
        concurrent=3,
        block_private_ips=False,
        retry_count=2,
        max_redirects=5,
        ssl_verify=True,
        strict_mime=False,
    )


def send_bytes(handler: BaseHTTPRequestHandler, body: bytes, content_type: str, extra: dict | None = None) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if extra:
        for key, value in extra.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_redirect(handler: BaseHTTPRequestHandler, location: str, code: int = 302) -> None:
    handler.send_response(code)
    handler.send_header("Location", location)
    handler.end_headers()


def send_status(handler: BaseHTTPRequestHandler, code: int, body: bytes = b"err") -> None:
    handler.send_response(code)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# silence unused import of time in fixture module consumers
_ = time

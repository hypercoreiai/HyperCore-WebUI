"""Structured error codes from the web-scrape spec."""

from __future__ import annotations

from typing import Any


FILE_TOO_LARGE = "FILE_TOO_LARGE"
TIMEOUT = "TIMEOUT"
ACCESS_DENIED = "ACCESS_DENIED"
HTTP_404 = "HTTP_404"
HTTP_403 = "HTTP_403"
HTTP_429 = "HTTP_429"
DNS_FAIL = "DNS_FAIL"
TLS_ERROR = "TLS_ERROR"
MAX_REDIRECTS = "MAX_REDIRECTS"
INVALID_URL = "INVALID_URL"
UNSUPPORTED_MIME = "UNSUPPORTED_MIME"
UNKNOWN = "UNKNOWN"


class ScrapeError(Exception):
    def __init__(self, code: str, message: str, url: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.url = url

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "message": self.message,
        }
        if self.url:
            payload["url"] = self.url
        return payload


def http_code(status: int) -> str:
    if status == 404:
        return HTTP_404
    if status == 403:
        return HTTP_403
    if status == 429:
        return HTTP_429
    return f"HTTP_{status}"


def error_dict(code: str, message: str, url: str = "") -> dict[str, Any]:
    return ScrapeError(code, message, url).to_dict()

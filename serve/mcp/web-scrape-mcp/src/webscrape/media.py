"""MIME sniffing and image/audio/video metadata."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import filetype

from webscrape.errors import UNSUPPORTED_MIME, ScrapeError

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".weba",
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/json": ".json",
}


def normalize_mime(value: str | None) -> str:
    if not value:
        return ""
    return value.split(";")[0].strip().lower()


def sniff_mime(path: Path) -> str:
    kind = filetype.guess(str(path))
    if kind is not None:
        return kind.mime
    return ""


def mime_from_header_or_file(header: str | None, path: Path) -> str:
    header_mime = normalize_mime(header)
    sniffed = sniff_mime(path)
    if not header_mime or header_mime in {"application/octet-stream", "binary/octet-stream"}:
        return sniffed or header_mime or "application/octet-stream"
    return header_mime


def check_strict_mime(header: str | None, path: Path) -> str:
    header_mime = normalize_mime(header)
    sniffed = sniff_mime(path)
    if header_mime in {"", "application/octet-stream", "binary/octet-stream"}:
        return sniffed or header_mime or "application/octet-stream"
    if sniffed and header_mime != sniffed:
        raise ScrapeError(
            UNSUPPORTED_MIME,
            f"magic bytes ({sniffed}) do not match Content-Type ({header_mime})",
        )
    return header_mime


def extension_for(mime: str, url_or_name: str = "") -> str:
    mime = normalize_mime(mime)
    if mime in _MIME_EXT:
        return _MIME_EXT[mime]
    suffix = Path(url_or_name.split("?")[0]).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    return ""


def image_dimensions(path: Path) -> dict[str, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
        return {"width": int(width), "height": int(height)}
    except Exception:
        return None


def ffprobe_info(path: Path) -> dict[str, Any] | None:
    binary = shutil.which("ffprobe")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [
                binary,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    duration = None
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None
    width = height = None
    for stream in streams:
        if stream.get("width") and stream.get("height"):
            width = int(stream["width"])
            height = int(stream["height"])
            break
    info: dict[str, Any] = {}
    if width and height:
        info["width"] = width
        info["height"] = height
    if duration is not None:
        info["duration_sec"] = duration
    return info or None

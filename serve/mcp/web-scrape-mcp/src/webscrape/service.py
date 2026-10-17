"""Tool implementations for the web-scrape MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from webscrape.config import Settings
from webscrape.errors import ACCESS_DENIED, INVALID_URL, ScrapeError, error_dict
from webscrape.extract import extract_media, hostname_stamp
from webscrape.httputil import choose_filename, download_to_path, fetch_text
from webscrape.media import extension_for, ffprobe_info, image_dimensions, mime_from_header_or_file
from webscrape.sandbox import (
    ensure_allowed_dir,
    resolve_in_allowed,
    sanitize_filename,
    subfolder_in_allowed,
)

log = logging.getLogger("webscrape")


def _parse_headers(value: dict[str, str] | str | None) -> dict[str, str] | None:
    if value is None or value == "" or value == {}:
        return None
    if isinstance(value, str):
        parsed = json.loads(value)
        return {str(k): str(v) for k, v in parsed.items()}
    return {str(k): str(v) for k, v in value.items()}


def _parse_str_list(value: list[str] | str | None) -> list[str]:
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            parsed = json.loads(text)
            return [str(x) for x in parsed]
        return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    return [str(x) for x in value]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _guess_mime(path: Path) -> str:
    sniffed = mime_from_header_or_file(None, path)
    if sniffed and sniffed != "application/octet-stream":
        return sniffed
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or sniffed or "application/octet-stream"


def _download_url_for_item(item: dict[str, Any]) -> str:
    poster = item.get("poster")
    tag = item.get("tag")
    url = item.get("url") or ""
    if tag == "iframe" and poster:
        return str(poster)
    return str(url)


async def download_file(
    settings: Settings,
    url: str,
    filename: str = "",
    headers: dict[str, str] | str | None = None,
    overwrite: bool = True,
    max_size_mb: float = -1,
) -> dict[str, Any]:
    try:
        extra = _parse_headers(headers)
        cap = settings.max_file_size_bytes
        if max_size_mb is not None and float(max_size_mb) > 0:
            cap = int(float(max_size_mb) * 1024 * 1024)
        if filename:
            sanitize_filename(filename)
        return await download_to_path(
            settings,
            url,
            folder=settings.allowed_dir,
            requested_filename=filename,
            headers=extra,
            max_bytes=cap,
            overwrite=overwrite,
        )
    except ScrapeError as exc:
        return exc.to_dict() | {"url": exc.url or url}


async def extract_media_from_url(
    settings: Settings,
    url: str,
    media_type: str = "all",
    include_bg: bool = False,
    max_results: int = 50,
    min_width: int = -1,
    min_height: int = -1,
    headers: dict[str, str] | str | None = None,
) -> dict[str, Any]:
    try:
        extra = _parse_headers(headers)
        final_url, html = await fetch_text(settings, url, extra)
        return extract_media(
            html,
            final_url,
            media_type=media_type or "all",
            include_bg=include_bg,
            max_results=max(0, int(max_results)),
            min_width=None if min_width is None or int(min_width) < 0 else int(min_width),
            min_height=None if min_height is None or int(min_height) < 0 else int(min_height),
        )
    except ScrapeError as exc:
        return exc.to_dict() | {"url": exc.url or url}


async def download_page_media(
    settings: Settings,
    url: str,
    media_type: str = "all",
    max_items: int = 5,
    subfolder: str = "",
    headers: dict[str, str] | str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    extracted = await extract_media_from_url(
        settings,
        url,
        media_type=media_type,
        include_bg=False,
        max_results=max(int(max_items) * 4, 50),
        headers=headers,
    )
    if extracted.get("status") != "ok":
        return extracted
    extra = _parse_headers(headers)
    folder_name = subfolder.strip() or hostname_stamp(url, int(time.time()))
    try:
        folder = subfolder_in_allowed(settings.allowed_dir, folder_name)
        folder.mkdir(parents=True, exist_ok=True)
    except ScrapeError as exc:
        return exc.to_dict() | {"url": url}

    media = list(extracted.get("media") or [])
    to_fetch = media[: max(0, int(max_items))]
    downloaded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(settings.concurrent)

    async def one(item: dict[str, Any]) -> None:
        source = _download_url_for_item(item)
        try:
            dest_name = choose_filename(source)
            dest = folder / dest_name
            result = await download_to_path(
                settings,
                source,
                dest=dest,
                headers=extra,
                overwrite=overwrite,
            )
            downloaded.append(
                {
                    "source_url": source,
                    "local_path": result["file_path"],
                    "bytes": result["bytes"],
                    "mime_type": result["mime_type"],
                }
            )
        except ScrapeError as exc:
            failed.append({"source_url": source, "code": exc.code, "message": exc.message})

    async def guarded(item: dict[str, Any]) -> None:
        async with sem:
            await one(item)

    await asyncio.gather(*(guarded(item) for item in to_fetch))
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "status": "ok",
        "page_url": extracted.get("page_url", url),
        "downloaded": downloaded,
        "failed": failed,
        "total_found": extracted.get("total_found", len(media)),
        "downloaded_count": len(downloaded),
        "failed_count": len(failed),
        "duration_ms": duration_ms,
    }


async def batch_download(
    settings: Settings,
    urls: list[str] | str,
    filenames: list[str] | str | None = None,
    subfolder: str = "",
    headers: dict[str, str] | str | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    url_list = _parse_str_list(urls)
    if not url_list or len(url_list) > 50:
        return error_dict(INVALID_URL, "urls must contain 1..50 absolute URLs")
    names = _parse_str_list(filenames)
    extra = _parse_headers(headers)
    try:
        folder = (
            subfolder_in_allowed(settings.allowed_dir, subfolder)
            if subfolder
            else ensure_allowed_dir(settings.allowed_dir)
        )
        folder.mkdir(parents=True, exist_ok=True)
    except ScrapeError as exc:
        return exc.to_dict()

    sem = asyncio.Semaphore(settings.concurrent)
    results: list[dict[str, Any] | None] = [None] * len(url_list)

    async def one(index: int, target: str) -> None:
        requested = names[index] if index < len(names) else ""
        try:
            dest = folder / choose_filename(target, requested=requested)
            async with sem:
                result = await download_to_path(
                    settings,
                    target,
                    dest=dest,
                    headers=extra,
                    overwrite=overwrite,
                )
            results[index] = {
                "url": target,
                "file_path": result["file_path"],
                "bytes": result["bytes"],
                "mime_type": result["mime_type"],
            }
        except ScrapeError as exc:
            results[index] = {
                "url": target,
                "code": exc.code,
                "message": exc.message,
            }

    await asyncio.gather(*(one(i, u) for i, u in enumerate(url_list)))
    finalized = [row for row in results if row is not None]
    succeeded = sum(1 for row in finalized if "file_path" in row)
    failed = len(finalized) - succeeded
    return {
        "status": "ok",
        "results": finalized,
        "succeeded": succeeded,
        "failed": failed,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def get_downloaded_file_info(settings: Settings, file_path: str) -> dict[str, Any]:
    try:
        path = resolve_in_allowed(settings.allowed_dir, file_path, must_exist=True)
        if not path.is_file():
            raise ScrapeError(ACCESS_DENIED, "path is not a file under ALLOWED_DIR")
    except ScrapeError as exc:
        return exc.to_dict()
    stat = path.stat()
    mime = _guess_mime(path)
    suffix = path.suffix.lower()
    payload: dict[str, Any] = {
        "status": "ok",
        "file_path": str(path),
        "bytes": stat.st_size,
        "mime_type": mime,
        "extension": suffix or extension_for(mime, path.name),
        "created": _iso(stat.st_ctime),
        "modified": _iso(stat.st_mtime),
    }
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
    video_exts = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".ogv"}
    audio_exts = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".weba"}
    if mime.startswith("image/") or suffix in image_exts:
        dims = image_dimensions(path)
        if dims:
            payload["dimensions"] = dims
    if mime.startswith("video/") or mime.startswith("audio/") or suffix in video_exts | audio_exts:
        info = ffprobe_info(path)
        if info:
            dims = {k: info[k] for k in ("width", "height") if k in info}
            if dims:
                payload["dimensions"] = dims
            if "duration_sec" in info:
                payload["duration_sec"] = info["duration_sec"]
    return payload


def list_downloads(
    settings: Settings,
    subfolder: str = "",
    mime_type: str = "",
    sort: str = "modified_desc",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    try:
        root = (
            subfolder_in_allowed(settings.allowed_dir, subfolder)
            if subfolder
            else ensure_allowed_dir(settings.allowed_dir)
        )
        if not root.exists():
            return {
                "status": "ok",
                "root": str(settings.allowed_dir),
                "files": [],
                "total": 0,
                "truncated": False,
            }
    except ScrapeError as exc:
        return exc.to_dict()

    allowed = ensure_allowed_dir(settings.allowed_dir)
    rows: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith(".tmp_"):
            continue
        try:
            path.relative_to(allowed)
        except ValueError:
            continue
        mime = _guess_mime(path)
        if mime_type:
            if mime_type.endswith("/*"):
                prefix = mime_type[:-1]
                if not mime.startswith(prefix):
                    continue
            elif mime != mime_type:
                continue
        stat = path.stat()
        rows.append(
            {
                "path": str(path.relative_to(allowed)).replace("\\", "/"),
                "bytes": stat.st_size,
                "mime": mime,
                "modified": _iso(stat.st_mtime),
                "_mtime": stat.st_mtime,
                "_name": path.name,
            }
        )

    key = sort or "modified_desc"
    if key == "modified_asc":
        rows.sort(key=lambda r: r["_mtime"])
    elif key == "name":
        rows.sort(key=lambda r: r["_name"].lower())
    elif key == "size":
        rows.sort(key=lambda r: r["bytes"], reverse=True)
    else:
        rows.sort(key=lambda r: r["_mtime"], reverse=True)

    for row in rows:
        row.pop("_mtime", None)
        row.pop("_name", None)

    total = len(rows)
    start = max(0, int(offset))
    cap = max(0, int(limit))
    sliced = rows[start : start + cap] if cap else rows[start:]
    return {
        "status": "ok",
        "root": str(allowed),
        "files": sliced,
        "total": total,
        "truncated": start + len(sliced) < total,
    }

"""Streaming HTTP downloads with retries, redirects, and SSRF checks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import ssl
import time
import uuid
from email.utils import decode_rfc2231
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import aiofiles
import httpx

from webscrape.config import Settings
from webscrape.errors import (
    ACCESS_DENIED,
    DNS_FAIL,
    FILE_TOO_LARGE,
    MAX_REDIRECTS,
    TIMEOUT,
    TLS_ERROR,
    UNKNOWN,
    ScrapeError,
    http_code,
)
from webscrape.media import check_strict_mime, extension_for, mime_from_header_or_file
from webscrape.sandbox import check_url_ssrf, resolve_in_allowed, sanitize_filename

log = logging.getLogger("webscrape")

_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*([^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)', re.IGNORECASE)


def _backoff_seconds(attempt: int) -> float:
    base = 1.0 * (2**attempt)
    jitter = random.uniform(0.8, 1.2)
    return base * jitter


def _filename_from_disposition(header: str | None) -> str:
    if not header:
        return ""
    star = _FILENAME_STAR_RE.search(header)
    if star:
        raw = star.group(1).strip().strip('"')
        try:
            encoding, _lang, value = decode_rfc2231(raw)
            if encoding:
                return unquote(value, encoding=encoding)
            return unquote(raw)
        except Exception:
            return unquote(raw.split("''", 1)[-1])
    match = _FILENAME_RE.search(header)
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip().strip('"')


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = Path(unquote(path)).name
    if not name or name.endswith("/"):
        return ""
    return sanitize_filename(name)


def generated_filename(url: str, mime: str = "") -> str:
    stamp = int(time.time())
    hash8 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    ext = extension_for(mime, url)
    return f"{stamp}_{hash8}{ext}"


def choose_filename(
    url: str,
    *,
    requested: str = "",
    content_disposition: str | None = None,
    mime: str = "",
) -> str:
    if requested:
        name = sanitize_filename(requested)
        if not name:
            raise ScrapeError(ACCESS_DENIED, "filename is empty after sanitization", url=url)
        return name
    disp = sanitize_filename(_filename_from_disposition(content_disposition))
    if disp:
        return disp
    from_url = _filename_from_url(url)
    if from_url:
        return from_url
    return generated_filename(url, mime)


def _classify_httpx(exc: BaseException, url: str) -> ScrapeError:
    if isinstance(exc, ScrapeError):
        if not exc.url:
            exc.url = url
        return exc
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError)):
        return ScrapeError(TIMEOUT, f"request exceeded timeout: {exc}", url=url)
    if isinstance(exc, ssl.SSLError):
        return ScrapeError(TLS_ERROR, f"certificate validation failed: {exc}", url=url)
    text = str(exc).lower()
    cause = exc.__cause__ or getattr(exc, "__context__", None)
    cause_text = str(cause).lower() if cause else ""
    combined = f"{text} {cause_text}"
    if any(s in combined for s in ("name or service not known", "getaddrinfo", "nodename nor servname", "name resolution", "temporary failure in name resolution")):
        return ScrapeError(DNS_FAIL, f"hostname did not resolve: {exc}", url=url)
    if any(s in combined for s in ("ssl", "certificate", "tls")):
        return ScrapeError(TLS_ERROR, f"certificate validation failed: {exc}", url=url)
    return ScrapeError(UNKNOWN, str(exc), url=url)


def build_client(settings: Settings) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(settings.timeout_sec),
        "follow_redirects": False,
        "verify": settings.ssl_verify,
        "headers": {"User-Agent": settings.user_agent},
        "max_redirects": 0,
    }
    if settings.proxy:
        kwargs["proxy"] = settings.proxy
    return httpx.AsyncClient(**kwargs)


def merge_headers(settings: Settings, extra: dict[str, str] | None) -> dict[str, str]:
    headers = {"User-Agent": settings.user_agent}
    if extra:
        for key, value in extra.items():
            if str(key).lower() == "user-agent":
                headers["User-Agent"] = str(value)
            else:
                headers[str(key)] = str(value)
    return headers


async def fetch_text(settings: Settings, url: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
    """Return (final_url, text body). Used for HTML pages only."""
    async with build_client(settings) as client:
        response = await _send_following_redirects(
            client, settings, "GET", url, headers=merge_headers(settings, headers)
        )
        if response.status_code >= 400:
            raise ScrapeError(
                http_code(response.status_code),
                f"upstream HTTP {response.status_code}",
                url=str(response.url),
            )
        return str(response.url), response.text


async def _send_following_redirects(
    client: httpx.AsyncClient,
    settings: Settings,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    stream: bool = False,
) -> httpx.Response:
    current = url
    for hop in range(settings.max_redirects + 1):
        check_url_ssrf(current, block_private=settings.block_private_ips)
        request = client.build_request(method, current, headers=headers)
        try:
            response = await client.send(request, stream=stream)
        except Exception as exc:
            raise _classify_httpx(exc, current) from exc
        if response.is_redirect:
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise ScrapeError(UNKNOWN, "redirect with no Location header", url=current)
            current = urljoin(current, location)
            continue
        return response
    raise ScrapeError(MAX_REDIRECTS, f"exceeded redirect chain limit ({settings.max_redirects})", url=url)


async def download_to_path(
    settings: Settings,
    url: str,
    dest: Path | None = None,
    *,
    folder: Path | None = None,
    requested_filename: str = "",
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    folder = resolve_in_allowed(settings.allowed_dir, folder or settings.allowed_dir)
    if dest is not None:
        dest = resolve_in_allowed(settings.allowed_dir, dest)
        if dest.exists() and not overwrite:
            mime = mime_from_header_or_file(None, dest)
            return {
                "status": "ok",
                "file_path": str(dest),
                "bytes": dest.stat().st_size,
                "mime_type": mime or "application/octet-stream",
                "content_length": dest.stat().st_size,
                "duration_ms": 0,
                "final_url": url,
                "http_status": 200,
                "skipped": True,
            }

    cap = max_bytes if max_bytes is not None else settings.max_file_size_bytes
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".tmp_{uuid.uuid4().hex}"
    started = time.perf_counter()
    merged = merge_headers(settings, headers)
    last_error: ScrapeError | None = None
    attempts = settings.retry_count + 1

    async with build_client(settings) as client:
        for attempt in range(attempts):
            try:
                return await _stream_once(
                    client,
                    settings,
                    url,
                    tmp,
                    dest,
                    folder,
                    requested_filename,
                    merged,
                    cap,
                    started,
                    overwrite,
                )
            except ScrapeError as exc:
                last_error = exc
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                retryable = exc.code in {"HTTP_429", "HTTP_503"}
                if retryable and attempt < attempts - 1:
                    delay = _backoff_seconds(attempt)
                    log.info("retry %s after %s in %.2fs", url, exc.code, delay)
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception as exc:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                raise _classify_httpx(exc, url) from exc

    raise last_error or ScrapeError(UNKNOWN, "download failed", url=url)


async def _stream_once(
    client: httpx.AsyncClient,
    settings: Settings,
    url: str,
    tmp: Path,
    dest: Path | None,
    folder: Path,
    requested_filename: str,
    headers: dict[str, str],
    cap: int,
    started: float,
    overwrite: bool,
) -> dict[str, Any]:
    response = await _send_following_redirects(
        client, settings, "GET", url, headers=headers, stream=True
    )
    final_url = str(response.url)
    status = response.status_code
    try:
        if status >= 400:
            await response.aread()
            raise ScrapeError(http_code(status), f"upstream HTTP {status}", url=final_url)

        content_length_header = response.headers.get("content-length")
        content_length = None
        if content_length_header and content_length_header.isdigit():
            content_length = int(content_length_header)
            if content_length > cap:
                raise ScrapeError(
                    FILE_TOO_LARGE,
                    f"Content-Length {content_length} exceeds max {cap} bytes",
                    url=final_url,
                )

        written = 0
        async with aiofiles.open(tmp, "wb") as handle:
            async for chunk in response.aiter_bytes(settings.stream_chunk_bytes):
                if not chunk:
                    continue
                if len(chunk) > settings.max_memory_chunk_bytes:
                    raise ScrapeError(UNKNOWN, "chunk exceeded 64 MB memory cap", url=final_url)
                written += len(chunk)
                if written > cap:
                    raise ScrapeError(
                        FILE_TOO_LARGE,
                        f"download exceeded max {cap} bytes",
                        url=final_url,
                    )
                await handle.write(chunk)

        header_mime = response.headers.get("content-type")
        if settings.strict_mime:
            mime = check_strict_mime(header_mime, tmp)
        else:
            mime = mime_from_header_or_file(header_mime, tmp)

        if dest is None:
            name = choose_filename(
                final_url,
                requested=requested_filename,
                content_disposition=response.headers.get("content-disposition"),
                mime=mime,
            )
            dest = resolve_in_allowed(settings.allowed_dir, folder / name)

        if dest.exists() and not overwrite:
            tmp.unlink(missing_ok=True)
            mime = mime_from_header_or_file(None, dest)
            return {
                "status": "ok",
                "file_path": str(dest),
                "bytes": dest.stat().st_size,
                "mime_type": mime or "application/octet-stream",
                "content_length": dest.stat().st_size,
                "duration_ms": 0,
                "final_url": final_url,
                "http_status": status,
                "skipped": True,
            }

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dest)
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "status": "ok",
            "file_path": str(dest),
            "bytes": written,
            "mime_type": mime or "application/octet-stream",
            "content_length": content_length if content_length is not None else written,
            "duration_ms": duration_ms,
            "final_url": final_url,
            "http_status": status,
        }
    except ScrapeError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise _classify_httpx(exc, final_url) from exc
    finally:
        await response.aclose()
        if tmp.exists():
            tmp.unlink(missing_ok=True)

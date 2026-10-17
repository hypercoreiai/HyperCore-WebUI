#!/usr/bin/env python3
"""serve/mcp/web_scrape_mcp.py - MCP server: sandboxed downloads and page media.

HyperCore starts it from mcp.yaml.  The Monitor shows it as soon as it answers; its tools are `status`,
`extract_media`, `download_file`, `download_page_media`, `batch_download`, `file_info`, `list_downloads`, and
`remove_downloads`.  Everything written lands under SCRAPE_DIR (/home/ai-agent/downloads unless SCRAPE_DIR is set);
nothing outside it is read or written.  Only public http(s) addresses are fetched, the same rule the Firefox server
uses (SCRAPE_BLOCK_PRIVATE=0 relaxes it).  Standard library only, like the other servers here: no MCP SDK, no httpx,
no BeautifulSoup.  Pillow and ffprobe are used only if they are installed; `status` says whether they are.

Downloaded text (html, svg, json, plain text) can be read with the `docs` tools, since SCRAPE_DIR is under /home.
Binary files are inspected with `file_info`, which reports size, type and image or video dimensions.

    python serve/mcp/web_scrape_mcp.py          stdio, JSON-RPC one message per line
"""
from __future__ import annotations

import contextlib
import hashlib
import html.parser
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRAPE_DIR = Path(os.environ.get("SCRAPE_DIR") or (Path.home() / "downloads")).expanduser().resolve()
MAX_FILE_BYTES = int(float(os.environ.get("SCRAPE_MAX_FILE_MB") or 200) * 1024 * 1024)
PAGE_BYTES = 5_000_000
FETCH_TIMEOUT = float(os.environ.get("SCRAPE_TIMEOUT_S") or 30.0)
WORKERS = max(1, int(os.environ.get("SCRAPE_CONCURRENCY") or 4))
AGENT = os.environ.get("SCRAPE_USER_AGENT") or "Mozilla/5.0 (compatible; HyperCore/1.0)"
MAX_REDIRECTS = max(0, int(os.environ.get("SCRAPE_MAX_REDIRECTS") or 5))
RETRIES = max(0, int(os.environ.get("SCRAPE_RETRY_COUNT") or 2))
BLOCK_PRIVATE = (os.environ.get("SCRAPE_BLOCK_PRIVATE", "").strip().lower() not in {"0", "false", "no", "off"})
MAX_LIST = 200
MAX_MEDIA = 200
MAX_URLS = 50
MAX_ITEMS = 20
CHUNK = 64 * 1024

_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00]')
_BG = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
_YT = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|shorts/|watch\?.*v=)|youtu\.be/)([\w-]{11})", re.IGNORECASE)
_VIMEO = re.compile(r"(?:player\.)?vimeo\.com/(?:video/)?(\d+)", re.IGNORECASE)

_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF8", "image/gif"),
    (b"\x1a\x45\xdf\xa3", "video/webm"), (b"OggS", "audio/ogg"), (b"ID3", "audio/mpeg"),
    (b"%PDF", "application/pdf"), (b"PK", "application/zip"), (b"RIFF", "audio/wav"),
    (b"\x00\x00\x00\x18", "video/mp4"), (b"\x00\x00\x00\x19", "video/mp4"), (b"\x00\x00\x00\x1a", "video/mp4"),
]
_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "image/bmp": ".bmp", "image/avif": ".avif",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/ogg": ".ogv",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg", "audio/webm": ".weba",
    "application/pdf": ".pdf", "application/zip": ".zip", "application/json": ".json",
    "text/html": ".html", "text/plain": ".txt",
}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg", ".tiff"}
_VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".ogv"}
_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".weba"}


def public_url(url: str) -> str:
    """An http(s) URL whose host resolves only to public addresses, or ValueError."""
    raw = (url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https addresses can be fetched")
    if parts.username or parts.password:
        raise ValueError("remove the username and password from the address")
    host = parts.hostname
    if host.lower() == "localhost" or host.lower().endswith((".local", ".localhost", ".internal")):
        raise ValueError(f"{host} is not a public internet address")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [literal] if literal is not None else []
    if not addresses:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise ValueError(f"could not resolve {host}: {e}") from None
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses:
        raise ValueError(f"could not resolve {host}")
    if BLOCK_PRIVATE:
        for ip in addresses:
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                    or ip.is_unspecified):
                raise ValueError(f"{host} is not a public internet address")
    return raw


class _StopRedirects(urllib.request.HTTPRedirectHandler):
    """Return None so urllib raises the redirect instead of following it; we then check each hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_url(url: str, headers: dict | None = None):
    """GET with redirects followed by hand, every hop checked. Returns (response, final_url)."""
    current = public_url(url)
    merged = {"User-Agent": AGENT}
    for key, value in (headers or {}).items():
        merged[str(key)] = str(value)
    opener = urllib.request.build_opener(_StopRedirects())
    for _ in range(MAX_REDIRECTS + 1):
        request = urllib.request.Request(current, headers=merged)
        try:
            return opener.open(request, timeout=FETCH_TIMEOUT), current
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get("location") if e.headers else None
                if not location:
                    raise ValueError(f"{current} redirected with no address") from None
                current = public_url(urllib.parse.urljoin(current, location))
                continue
            raise ValueError(f"{current} returned HTTP {e.code}") from None
        except OSError as e:
            raise ValueError(f"could not reach {current}: {e}") from None
    raise ValueError(f"{url} redirected more than {MAX_REDIRECTS} times")


def safe_name(name: str) -> str:
    normalized = str(name or "").replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("a name may not contain '..'")
    return _ILLEGAL.sub("_", normalized).strip().strip(".")


def inside(path: str | Path) -> Path:
    """Resolve path under SCRAPE_DIR; anything that lands outside it is refused."""
    raw = str(path or "").strip().rstrip("/")
    if not raw:
        return SCRAPE_DIR
    if "\x00" in raw:
        raise ValueError("path contains a null byte")
    if any(part == ".." for part in Path(raw).parts):
        raise ValueError("path may not contain '..'")
    candidate = Path(raw)
    target = candidate if candidate.is_absolute() else SCRAPE_DIR / candidate
    resolved = target.parent.resolve() / target.name
    if not resolved.is_relative_to(SCRAPE_DIR):
        raise ValueError(f"{path} is outside the download folder")
    return resolved


def sniff(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return ""
    for signature, mime in _MAGIC:
        if head.startswith(signature):
            return mime
    if b"<svg" in head[:200]:
        return "image/svg+xml"
    return ""


def header_mime(header: str | None) -> str:
    return (header or "").split(";")[0].strip().lower()


def pick_name(url: str, requested: str, disposition: str | None, mime: str) -> str:
    if requested:
        name = safe_name(requested)
        if not name:
            raise ValueError("the filename is empty after cleaning")
        return name
    for pattern in (r"filename\*=\s*([^;]+)", r'filename=\s*"([^"]+)"', r"filename=\s*([^;]+)"):
        match = re.search(pattern, disposition or "", re.IGNORECASE)
        if match:
            value = urllib.parse.unquote(match.group(1).strip().strip('"'))
            if value.lower().startswith("utf-8''"):
                value = value[8:]
            name = safe_name(value)
            if name:
                return name
    from_url = safe_name(Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name)
    if from_url:
        return from_url
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{int(time.time())}_{digest}{_EXT.get(mime, '')}"


def guess_mime(path: Path) -> str:
    return sniff(path) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def image_size(path: Path) -> dict | None:
    with contextlib.suppress(ImportError, OSError, ValueError):
        # Pillow is optional here; a file Pillow cannot open is not an error, it just
        # means the header check below has to decide.
        from PIL import Image
        with Image.open(path) as img:
            return {"width": int(img.width), "height": int(img.height)}
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        return {"width": int(width), "height": int(height)}
    if head.startswith(b"GIF8") and len(head) >= 8:
        width, height = struct.unpack("<HH", head[6:8])
        return {"width": int(width), "height": int(height)}
    if head.startswith(b"\xff\xd8"):
        i = 2
        while i < len(head) - 6:
            if head[i] == 0xFF and head[i + 1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                height, width = struct.unpack(">HH", head[i + 3:i + 7])
                return {"width": int(width), "height": int(height)}
            i += 1
    return None


def media_size(path: Path) -> dict | None:
    binary = shutil.which("ffprobe")
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams",
                               str(path)], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    info: dict[str, object] = {}
    for stream in data.get("streams") or []:
        if stream.get("width") and stream.get("height"):
            info["width"], info["height"] = int(stream["width"]), int(stream["height"])
        if stream.get("codec_name") and "codec" not in info:
            info["codec"] = stream["codec_name"]
        if stream.get("codec_type") == "audio" and "audio_codec" not in info:
            info["audio_codec"] = stream.get("codec_name")
            channels = stream.get("channels")
            if channels:
                info["audio_channels"] = int(channels)
        if stream.get("frame_rate") and "frame_rate" not in info:
            info["frame_rate"] = stream["frame_rate"]
    fmt = data.get("format") or {}
    duration = fmt.get("duration")
    try:
        if duration:
            info["duration_s"] = float(duration)
    except (TypeError, ValueError):
        pass
    if fmt.get("format_name"):
        info["container"] = fmt["format_name"]
    bitrate = fmt.get("bit_rate")
    try:
        if bitrate:
            info["bit_rate_bps"] = int(bitrate)
    except (TypeError, ValueError):
        pass
    return info or None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip().rstrip("px"))
    except (TypeError, ValueError):
        return None


def _as_bool(value, default: bool) -> bool:
    """Models often send a boolean as "false" or 0; bool("false") is True, so read it as text."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _mb(nbytes: int) -> str:
    return f"{nbytes / (1024 * 1024):.2f} MB"


def _has_pillow() -> bool:
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


class MediaFinder(html.parser.HTMLParser):
    """Every image, video and audio address on a page, made absolute. No page text is kept."""

    def __init__(self, base: str, want: str, backgrounds: bool):
        super().__init__(convert_charrefs=True)
        self.base, self.want, self.backgrounds = base, want, backgrounds
        self.items: list[dict] = []
        self.seen: set[str] = set()
        self.open: list[str] = []

    def abs(self, raw: str | None) -> str:
        value = (raw or "").strip()
        if not value or value.startswith(("#", "javascript:", "mailto:", "data:")):
            return ""
        return urllib.parse.urljoin(self.base, value)

    def first_srcset(self, srcset: str | None) -> str:
        if not srcset:
            return ""
        return self.abs(srcset.split(",")[0].strip().split()[0])

    def add(self, url: str, kind: str, tag: str, **extra) -> None:
        if not url or url in self.seen or self.want not in (kind, "all"):
            return
        self.seen.add(url)
        item = {"type": kind, "url": url, "tag": tag}
        item.update({key: value for key, value in extra.items() if value})
        self.items.append(item)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        tag = (tag or "").lower()
        if tag in ("video", "audio", "picture"):
            self.open.append(tag)
        if tag == "img":
            self.add(self.abs(a.get("src")) or self.first_srcset(a.get("srcset")), "image", "img",
                     alt=a.get("alt"), width=_as_int(a.get("width")), height=_as_int(a.get("height")))
        elif tag == "source":
            parent = self.open[-1] if self.open else ""
            typ = a.get("type") or ""
            url = self.abs(a.get("src")) or self.first_srcset(a.get("srcset"))
            if parent == "picture" or typ.startswith("image/"):
                self.add(url, "image", f"{parent or 'source'} > source")
            elif parent == "video" or typ.startswith("video/"):
                self.add(url, "video", "video > source")
            elif parent == "audio" or typ.startswith("audio/"):
                self.add(url, "audio", "audio > source")
        elif tag == "video":
            poster = self.abs(a.get("poster"))
            self.add(self.abs(a.get("src")), "video", "video", poster=poster)
            if poster:
                self.add(poster, "image", "video poster")
        elif tag == "audio":
            self.add(self.abs(a.get("src")), "audio", "audio")
        elif tag == "embed":
            typ = a.get("type") or ""
            if typ.startswith("video/"):
                self.add(self.abs(a.get("src")), "video", "embed")
            elif typ.startswith("audio/"):
                self.add(self.abs(a.get("src")), "audio", "embed")
        elif tag == "iframe":
            src = self.abs(a.get("src"))
            if src and ("/embed/" in src or _YT.search(src) or _VIMEO.search(src)):
                video_id = _YT.search(src)
                self.add(src, "video", "iframe",
                        poster=f"https://i.ytimg.com/vi/{video_id.group(1)}/maxresdefault.jpg" if video_id else "")
        style = a.get("style")
        if style and self.backgrounds:
            for match in _BG.finditer(style):
                self.add(self.abs(match.group(1)), "image", "style attribute")

    def handle_endtag(self, tag):
        if self.open and (tag or "").lower() == self.open[-1]:
            self.open.pop()

    def handle_data(self, data):
        if self.backgrounds and self.open and self.open[-1] == "style":
            for match in _BG.finditer(data or ""):
                self.add(self.abs(match.group(1)), "image", "style")


def extract_media(url: str, media_type: str = "all", backgrounds: bool = False, max_results: int = 50,
                  min_width: int = 0, min_height: int = 0) -> dict:
    want = media_type if media_type in {"image", "video", "audio", "all"} else "all"
    response, final = open_url(url)
    try:
        body = response.read(PAGE_BYTES).decode("utf-8", "replace")
        truncated = len(body) >= PAGE_BYTES
    finally:
        try:
            response.close()
        except OSError:
            pass
    finder = MediaFinder(final, want, backgrounds)
    finder.feed(body)
    items = finder.items
    if min_width or min_height:
        kept = []
        for item in items:
            if item["type"] != "image":
                kept.append(item)
                continue
            width, height = item.get("width"), item.get("height")
            if (width is None or int(width) >= min_width) and (height is None or int(height) >= min_height):
                kept.append(item)
        items = kept
    limit = max(1, min(int(max_results), MAX_MEDIA))
    return {"page_url": final, "total_found": len(items), "returned": min(len(items), limit),
            "html_truncated": truncated, "media": items[:limit]}


def _source(item: dict) -> str:
    """An embedded player has no downloadable src; its poster is the file worth keeping."""
    if item.get("tag") == "iframe" and item.get("poster"):
        return item["poster"]
    return item.get("url") or ""


def download_file(url: str, filename: str = "", subfolder: str = "", headers: dict | None = None,
                  overwrite: bool = True, max_mb: float = 0) -> dict:
    cap = MAX_FILE_BYTES if not max_mb or float(max_mb) <= 0 else int(float(max_mb) * 1024 * 1024)
    folder = inside(subfolder or SCRAPE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".tmp_{os.urandom(8).hex()}"
    started = time.time()
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            response, final = open_url(url, headers)
            try:
                content_type = response.headers.get("content-type")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > cap:
                    raise ValueError(f"the file is {_mb(int(declared))}, over the {_mb(cap)} limit")
                written = 0
                with open(tmp, "wb") as handle:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > cap:
                            raise ValueError(f"the file is bigger than the {_mb(cap)} limit "
                                           f"(stopped after {_mb(written)})")
                        handle.write(chunk)
            finally:
                try:
                    response.close()
                except OSError:
                    pass
            mime = header_mime(content_type) or sniff(tmp)
            dest = inside(folder / pick_name(final, filename, response.headers.get("content-disposition"), mime))
            if dest.exists() and not overwrite:
                return {"skipped": True, "file_path": str(dest), "bytes": dest.stat().st_size,
                        "mime_type": guess_mime(dest), "url": final}
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest)
            return {"file_path": str(dest), "bytes": written, "mime_type": mime or "application/octet-stream",
                    "url": final, "duration_ms": int((time.time() - started) * 1000)}
        except ValueError as e:
            last_error = e
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if ("429" in str(e) or "503" in str(e)) and attempt < RETRIES:
                time.sleep(1.5 * (2 ** attempt))
                continue
            raise
    raise last_error or ValueError("the download failed")


def download_page_media(url: str, media_type: str = "all", max_items: int = 5, subfolder: str = "") -> dict:
    wanted_count = max(1, min(int(max_items), MAX_ITEMS))
    found = extract_media(url, media_type=media_type, max_results=max(wanted_count * 4, 50))
    folder_name = safe_name(subfolder) or f"{urllib.parse.urlsplit(url).hostname or 'page'}_{int(time.time())}"
    folder = inside(folder_name)
    folder.mkdir(parents=True, exist_ok=True)
    wanted = found["media"][: wanted_count]

    def one(item: dict) -> dict:
        source = _source(item)
        try:
            result = download_file(source, subfolder=str(folder), overwrite=False)
            return {"url": source, "file_path": result["file_path"], "bytes": result["bytes"],
                    "mime_type": result["mime_type"]}
        except ValueError as e:
            return {"url": source, "error": str(e)}

    with ThreadPoolExecutor(max_workers=min(WORKERS, max(1, len(wanted)))) as pool:
        results = list(pool.map(one, wanted))
    saved = [row for row in results if "file_path" in row]
    failed = [row for row in results if "error" in row]
    return {"page_url": found["page_url"], "folder": str(folder), "total_found": found["total_found"],
            "downloaded": len(saved), "failed": len(failed), "files": saved, "errors": failed}


def batch_download(urls: list[str], subfolder: str = "") -> dict:
    if not urls or len(urls) > MAX_URLS:
        raise ValueError(f"urls must hold between 1 and {MAX_URLS} addresses")
    folder = inside(subfolder or SCRAPE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def one(url: str) -> dict:
        try:
            result = download_file(url, subfolder=str(folder))
            return {"url": url, "file_path": result["file_path"], "bytes": result["bytes"],
                    "mime_type": result["mime_type"]}
        except ValueError as e:
            return {"url": url, "error": str(e)}

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(urls))) as pool:
        results = list(pool.map(one, urls))
    return {"folder": str(folder), "downloaded": sum(1 for row in results if "file_path" in row),
            "failed": sum(1 for row in results if "error" in row),
            "duration_ms": int((time.time() - started) * 1000), "files": results}


def file_info(path: str) -> dict:
    target = inside(path)
    if not target.exists():
        raise ValueError(f"no such file in the download folder: {path}")
    if target.is_dir():
        files = [p for p in target.rglob("*") if p.is_file()]
        return {"path": str(target), "is_directory": True, "files": len(files),
                "bytes": sum(p.stat().st_size for p in files)}
    stat = target.stat()
    mime = guess_mime(target)
    info = {"file_path": str(target), "bytes": stat.st_size, "mime_type": mime,
           "created": _iso(stat.st_ctime), "modified": _iso(stat.st_mtime)}
    suffix = target.suffix.lower()
    if mime.startswith("image/") or suffix in _IMAGE_EXT:
        size = image_size(target)
        if size:
            info["dimensions"] = size
    if mime.startswith(("video/", "audio/")) or suffix in _VIDEO_EXT | _AUDIO_EXT:
        size = media_size(target)
        if size:
            info.update(size)
    return info


def list_downloads(subfolder: str = "", mime_type: str = "", sort: str = "newest", limit: int = 100) -> dict:
    header = summary()                      # where downloads live, how many, the size limit
    root = inside(subfolder or SCRAPE_DIR)
    if not root.exists():
        return {**header, "root": str(root), "total": 0, "shown": 0, "files": [],
                "note": "that folder does not exist yet"}
    if not root.is_dir():
        return {**header, "root": str(root), "total": 1, "shown": 1,
                "files": [{"path": str(root.relative_to(SCRAPE_DIR)), "bytes": root.stat().st_size,
                          "mime": guess_mime(root), "modified": _iso(root.stat().st_mtime)}]}
    rows: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith(".tmp_"):
            continue
        mime = guess_mime(path)
        if mime_type and not (mime == mime_type
                              or (mime_type.endswith("/*") and mime.startswith(mime_type[:-1]))):
            continue
        stat = path.stat()
        rows.append({"path": str(path.relative_to(SCRAPE_DIR)), "bytes": stat.st_size, "mime": mime,
                    "modified": _iso(stat.st_mtime)})
    if sort == "oldest":
        rows.sort(key=lambda row: str(row["modified"]))
    elif sort == "name":
        rows.sort(key=lambda row: str(row["path"]).lower())
    elif sort == "size":
        rows.sort(key=lambda row: int(row["bytes"]), reverse=True)
    else:
        rows.sort(key=lambda row: str(row["modified"]), reverse=True)
    shown = rows[: max(1, min(int(limit), MAX_LIST))]
    return {**header, "root": str(SCRAPE_DIR), "total": len(rows), "shown": len(shown),
            "truncated": len(rows) > len(shown), "files": shown}


def remove_downloads(path: str = "") -> dict:
    """Delete one file or a folder and everything in it. The download root itself cannot be removed here."""
    target = inside(path)
    if target == SCRAPE_DIR:
        raise ValueError("pass a file or a subfolder; the download folder itself is not removed")
    if not target.exists():
        raise ValueError(f"nothing to remove at {path}")
    if target.is_file():
        size = target.stat().st_size
        target.unlink()
        return {"removed": 1, "bytes": size, "path": str(target)}
    count = size = 0
    for child in target.rglob("*"):
        if child.is_file():
            count += 1
            size += child.stat().st_size
    shutil.rmtree(target)
    return {"removed": count, "bytes": size, "path": str(target)}


def summary() -> dict:
    """The folder's totals and limits.

    Not a tool of its own: these are the header of a listing. Where a caller used to make two calls
    - one to ask what is in the folder, one to list it - the listing carries both now.
    """
    count = total = 0
    if SCRAPE_DIR.exists():
        for path in SCRAPE_DIR.rglob("*"):
            if path.is_file() and not path.name.startswith(".tmp_"):
                count += 1
                total += path.stat().st_size
    return {"download_dir": str(SCRAPE_DIR), "files_in_folder": count, "bytes_in_folder": total,
            "max_file_mb": MAX_FILE_BYTES // (1024 * 1024), "private_ips_blocked": BLOCK_PRIVATE,
            "dimensions": {"pillow": _has_pillow(), "ffprobe": shutil.which("ffprobe") is not None}}


TOOLS = [
    {"name": "extract_media",
     "description": "List the image, video and audio addresses on a public page. Returns addresses, not page text.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The http or https address of the page."},
         "media_type": {"type": "string", "enum": ["image", "video", "audio", "all"],
                        "description": "Which kind to return. Default all."},
         "backgrounds": {"type": "boolean", "description": "Also take images from CSS background rules. Default false."},
         "max_results": {"type": "integer", "description": "How many addresses to return, 1 to 200. Default 50."},
         "min_width": {"type": "integer", "description": "Skip images narrower than this. 0 for no limit."},
         "min_height": {"type": "integer", "description": "Skip images shorter than this. 0 for no limit."}},
         "required": ["url"]}},
    {"name": "download_file",
     "description": "Download one file from a public address into the download folder. Returns its path, size and type.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The http or https address to download."},
         "filename": {"type": "string", "description": "A name for the file. Otherwise it comes from the address."},
         "subfolder": {"type": "string", "description": "A folder inside the download folder. Optional."},
         "overwrite": {"type": "boolean", "description": "Replace the file if it already exists. Default true."},
         "max_mb": {"type": "number", "description": "Refuse files larger than this many MB. 0 uses the default limit."}},
         "required": ["url"]}},
    {"name": "download_page_media",
     "description": "Find the media on a page and download the first few files, into their own subfolder.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The page to read."},
         "media_type": {"type": "string", "enum": ["image", "video", "audio", "all"], "description": "Default all."},
         "max_items": {"type": "integer", "description": "How many files to download, 1 to 20. Default 5."},
         "subfolder": {"type": "string", "description": "Folder name for this run. Defaults to the host and a time."}},
         "required": ["url"]}},
    {"name": "batch_download",
     "description": "Download a list of addresses in parallel, up to 50.",
     "inputSchema": {"type": "object", "properties": {
         "urls": {"type": "array", "items": {"type": "string"}, "description": "Up to 50 http(s) addresses."},
         "subfolder": {"type": "string", "description": "Folder inside the download folder. Optional."}},
         "required": ["urls"]}},
    {"name": "file_info",
     "description": "Size, type, and image or video dimensions of a file already in the download folder.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Path inside the download folder."}}, "required": ["path"]}},
    {"name": "list_downloads",
     "description": "List the files in the download folder, with size, type and modified time.",
     "inputSchema": {"type": "object", "properties": {
         "subfolder": {"type": "string", "description": "Only this folder, recursively. Default the whole folder."},
         "mime_type": {"type": "string", "description": "Filter, e.g. image/ or image/png."},
         "sort": {"type": "string", "enum": ["newest", "oldest", "name", "size"], "description": "Default newest."},
         "limit": {"type": "integer", "description": "How many entries to return, 1 to 200. Default 100."}}}},
    {"name": "remove_downloads",
     "description": "Delete a downloaded file or a whole subfolder. Returns how many files and bytes were removed.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A file or subfolder inside the download folder."}},
         "required": ["path"]}},
]


def text(body: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "isError": error}


def call(name: str, args: dict) -> dict:
    try:
        if name == "extract_media":
            return text(json.dumps(extract_media(
                str(args.get("url") or ""), str(args.get("media_type") or "all"),
                _as_bool(args.get("backgrounds"), False), int(args.get("max_results") or 50),
                int(args.get("min_width") or 0), int(args.get("min_height") or 0)), indent=1))
        if name == "download_file":
            headers = args.get("headers")
            if isinstance(headers, str):
                try:
                    headers = json.loads(headers)
                except ValueError:
                    headers = None
            return text(json.dumps(download_file(
                str(args.get("url") or ""), str(args.get("filename") or ""), str(args.get("subfolder") or ""),
                headers if isinstance(headers, dict) else None,
                _as_bool(args.get("overwrite"), True), float(args.get("max_mb") or 0)), indent=1))
        if name == "download_page_media":
            return text(json.dumps(download_page_media(
                str(args.get("url") or ""), str(args.get("media_type") or "all"),
                int(args.get("max_items") or 5), str(args.get("subfolder") or "")), indent=1))
        if name == "batch_download":
            urls = args.get("urls") or []
            if isinstance(urls, str):
                urls = [part.strip() for part in re.split(r"[,;]", urls) if part.strip()]
            return text(json.dumps(batch_download([str(u) for u in urls], str(args.get("subfolder") or "")), indent=1))
        if name == "file_info":
            return text(json.dumps(file_info(str(args.get("path") or "")), indent=1))
        if name == "list_downloads":
            return text(json.dumps(list_downloads(
                str(args.get("subfolder") or ""), str(args.get("mime_type") or ""),
                str(args.get("sort") or "newest"), int(args.get("limit") or 100)), indent=1))
        if name == "remove_downloads":
            return text(json.dumps(remove_downloads(str(args.get("path") or "")), indent=1))
    except Exception as e:  # noqa: BLE001 - the model reads the failure as a tool result
        return text(str(e), error=True)
    return text(f"unknown tool {name}", error=True)


def handle(msg: dict):
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "scrape", "title": "Downloaded files and page media", "version": "0.1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": call(str(params.get("name") or ""),
                                                         params.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def stdio():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    SCRAPE_DIR.mkdir(parents=True, exist_ok=True)
    out = threading.Lock()

    def answer(msg):
        reply = handle(msg)
        if reply is None:
            return
        with out:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("method") == "tools/call":
            threading.Thread(target=answer, args=(msg,), daemon=True).start()
        else:
            answer(msg)


if __name__ == "__main__":
    stdio()

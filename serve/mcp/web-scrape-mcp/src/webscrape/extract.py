"""HTML media extraction for extract-media."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

_BG_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
_YT_ID_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|shorts/|watch\?.*v=)|youtu\.be/)([\w-]{11})",
    re.IGNORECASE,
)
_VIMEO_ID_RE = re.compile(r"(?:player\.)?vimeo\.com/(?:video/)?(\d+)", re.IGNORECASE)


def _abs(page_url: str, raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value or value.startswith(("#", "javascript:", "mailto:")):
        return None
    if value.lower().startswith("data:"):
        return None
    return urljoin(page_url, value)


def _srcset_first(page_url: str, srcset: str | None) -> str | None:
    if not srcset:
        return None
    first = srcset.split(",")[0].strip().split()[0]
    return _abs(page_url, first)


def _int_attr(tag: Tag, *names: str) -> int | None:
    for name in names:
        raw = tag.get(name)
        if raw is None:
            continue
        try:
            return int(str(raw).strip().rstrip("px"))
        except (TypeError, ValueError):
            continue
    return None


def _youtube_id(url: str) -> str | None:
    match = _YT_ID_RE.search(url)
    return match.group(1) if match else None


def _vimeo_id(url: str) -> str | None:
    match = _VIMEO_ID_RE.search(url)
    return match.group(1) if match else None


def extract_media(
    html: str,
    page_url: str,
    *,
    media_type: str = "all",
    include_bg: bool = False,
    max_results: int = 50,
    min_width: int | None = None,
    min_height: int | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    wanted = media_type if media_type in {"image", "video", "audio", "all"} else "all"
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        url = item.get("url")
        if not url or url in seen:
            return
        if item.get("type") == "image":
            width = item.get("width")
            height = item.get("height")
            if min_width is not None and width is not None and int(width) < min_width:
                return
            if min_height is not None and height is not None and int(height) < min_height:
                return
        seen.add(url)
        items.append(item)

    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        name = tag.name.lower() if tag.name else ""

        if name == "source":
            typ = str(tag.get("type") or "")
            parent = (
                tag.parent.name.lower()
                if tag.parent and isinstance(tag.parent, Tag) and tag.parent.name
                else ""
            )
            url = _abs(page_url, tag.get("src")) or _srcset_first(page_url, tag.get("srcset"))
            if wanted in {"image", "all"} and (typ.startswith("image/") or parent == "picture"):
                add(
                    {
                        "type": "image",
                        "url": url,
                        "tag": "picture > source" if parent == "picture" else "source",
                    }
                )
            elif wanted in {"video", "all"} and (typ.startswith("video/") or parent == "video"):
                add({"type": "video", "url": url, "tag": "video > source"})
            elif wanted in {"audio", "all"} and (typ.startswith("audio/") or parent == "audio"):
                add({"type": "audio", "url": url, "tag": "audio > source"})

        if wanted in {"image", "all"}:
            if name == "img":
                url = _abs(page_url, tag.get("src")) or _srcset_first(page_url, tag.get("srcset"))
                item: dict[str, Any] = {"type": "image", "url": url, "tag": "img"}
                if tag.get("alt"):
                    item["alt"] = str(tag.get("alt"))
                width = _int_attr(tag, "width")
                height = _int_attr(tag, "height")
                if width is not None:
                    item["width"] = width
                if height is not None:
                    item["height"] = height
                add(item)
            if include_bg:
                style = tag.get("style")
                if style:
                    for match in _BG_URL_RE.finditer(str(style)):
                        add({"type": "image", "url": _abs(page_url, match.group(1)), "tag": "style-attr"})
                if name == "style" and tag.string:
                    for match in _BG_URL_RE.finditer(str(tag.string)):
                        add({"type": "image", "url": _abs(page_url, match.group(1)), "tag": "style"})

        if wanted in {"video", "all"}:
            if name == "video":
                url = _abs(page_url, tag.get("src"))
                item = {"type": "video", "url": url, "tag": "video"}
                poster = _abs(page_url, tag.get("poster"))
                if poster:
                    item["poster"] = poster
                add(item)
            elif name == "embed":
                typ = str(tag.get("type") or "")
                if typ.startswith("video/"):
                    add({"type": "video", "url": _abs(page_url, tag.get("src")), "tag": "embed"})
            elif name == "iframe":
                src = _abs(page_url, tag.get("src")) or ""
                if "/embed/" in src or _youtube_id(src) or _vimeo_id(src):
                    item = {"type": "video", "url": src, "tag": "iframe"}
                    yt = _youtube_id(src)
                    if yt:
                        item["poster"] = f"https://i.ytimg.com/vi/{yt}/maxresdefault.jpg"
                    add(item)

        if wanted in {"audio", "all"}:
            if name == "audio":
                add({"type": "audio", "url": _abs(page_url, tag.get("src")), "tag": "audio"})
            elif name == "embed":
                typ = str(tag.get("type") or "")
                if typ.startswith("audio/"):
                    add({"type": "audio", "url": _abs(page_url, tag.get("src")), "tag": "embed"})

    total = len(items)
    capped = items[: max(0, int(max_results))]
    return {
        "status": "ok",
        "page_url": page_url,
        "media": capped,
        "total_found": total,
        "returned": len(capped),
    }


def hostname_stamp(page_url: str, timestamp: int) -> str:
    host = urlparse(page_url).hostname or "page"
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_") or "page"
    return f"{safe}_{timestamp}"

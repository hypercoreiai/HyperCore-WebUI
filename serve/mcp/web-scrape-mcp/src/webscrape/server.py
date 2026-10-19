"""MCP tools for sandboxed binary downloads and page media extraction."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from webscrape import service
from webscrape.config import Settings, load_settings

mcp = MCPServer(
    "web-scrape",
    instructions=(
        "Download binary files and extract page media into a sandboxed directory. "
        "Use extract-media to list image/video/audio URLs on a page, "
        "download-file or batch-download to save them, and list-downloads "
        "or get-downloaded-file-info to inspect what is on disk. "
        "All writes stay under ALLOWED_DIR. Private IPs are blocked by default."
    ),
)

_settings: Settings | None = None


def init_app(settings: Settings | None = None) -> Settings:
    global _settings
    _settings = settings or load_settings()
    _settings.allowed_dir.mkdir(parents=True, exist_ok=True)
    return _settings


def settings() -> Settings:
    if _settings is None:
        return init_app()
    return _settings


def _dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


@mcp.tool(name="download-file")
async def download_file(
    url: str,
    filename: str = "",
    headers: dict[str, str] | None = None,
    overwrite: bool = True,
    max_size_mb: float = -1,
) -> str:
    """Download a single binary or text resource from a URL into ALLOWED_DIR."""
    return _dumps(
        await service.download_file(
            settings(),
            url=url,
            filename=filename,
            headers=headers,
            overwrite=overwrite,
            max_size_mb=max_size_mb,
        )
    )


@mcp.tool(name="extract-media")
async def extract_media(
    url: str,
    media_type: str = "all",
    include_bg: bool = False,
    max_results: int = 50,
    min_width: int = -1,
    min_height: int = -1,
) -> str:
    """Scrape a page's HTML and return media resource URLs (no page text)."""
    return _dumps(
        await service.extract_media_from_url(
            settings(),
            url=url,
            media_type=media_type,
            include_bg=include_bg,
            max_results=max_results,
            min_width=min_width,
            min_height=min_height,
        )
    )


@mcp.tool(name="download-page-media")
async def download_page_media(
    url: str,
    media_type: str = "all",
    max_items: int = 5,
    subfolder: str = "",
    headers: dict[str, str] | None = None,
) -> str:
    """Extract media from a page and download the first max_items into ALLOWED_DIR."""
    return _dumps(
        await service.download_page_media(
            settings(),
            url=url,
            media_type=media_type,
            max_items=max_items,
            subfolder=subfolder,
            headers=headers,
        )
    )


@mcp.tool(name="batch-download")
async def batch_download(
    urls: list[str],
    filenames: list[str] | None = None,
    subfolder: str = "",
    headers: dict[str, str] | None = None,
    overwrite: bool = True,
) -> str:
    """Download a list of URLs in parallel with bounded concurrency (max 50)."""
    return _dumps(
        await service.batch_download(
            settings(),
            urls=urls,
            filenames=filenames,
            subfolder=subfolder,
            headers=headers,
            overwrite=overwrite,
        )
    )


@mcp.tool(name="get-downloaded-file-info")
def get_downloaded_file_info(file_path: str) -> str:
    """Inspect an already-downloaded file (size, MIME, image/video dimensions)."""
    return _dumps(service.get_downloaded_file_info(settings(), file_path=file_path))


@mcp.tool(name="list-downloads")
def list_downloads(
    subfolder: str = "",
    mime_type: str = "",
    sort: str = "modified_desc",
    limit: int = 100,
    offset: int = 0,
) -> str:
    """List files in ALLOWED_DIR (recursive) with size, MIME, and modified time."""
    return _dumps(
        service.list_downloads(
            settings(),
            subfolder=subfolder,
            mime_type=mime_type,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    )

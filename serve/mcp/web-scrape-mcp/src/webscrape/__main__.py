"""CLI: python -m webscrape [--stdio|--http]."""

from __future__ import annotations

import argparse
import logging
import sys

from webscrape.config import load_settings
from webscrape.server import init_app, mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webscrape")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="MCP over stdin/stdout (Cursor / local clients)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="MCP streamable HTTP (AnythingLLM). This is the default.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    settings.allowed_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    logging.getLogger("webscrape").info(
        "allowed_dir=%s max_mb=%s block_private=%s http=%s:%s",
        settings.allowed_dir,
        settings.max_file_size_mb,
        settings.block_private_ips,
        settings.mcp_host,
        settings.mcp_port,
    )
    init_app(settings)

    if args.stdio:
        mcp.run(transport="stdio")
        return 0

    print(f"web-scrape MCP HTTP on http://{settings.mcp_host}:{settings.mcp_port}/mcp", flush=True)
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

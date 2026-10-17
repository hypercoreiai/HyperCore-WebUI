"""Runtime settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _default_allowed_dir() -> Path:
    env = os.environ.get("MCP_ALLOWED_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "downloads").resolve()


@dataclass
class Settings:
    allowed_dir: Path = field(default_factory=_default_allowed_dir)
    max_file_size_mb: float = 500.0
    timeout_sec: float = 30.0
    concurrent: int = 3
    user_agent: str = "Mozilla/5.0 (compatible; MCPScra/1.0)"
    proxy: str = ""
    ssl_verify: bool = True
    max_redirects: int = 5
    retry_count: int = 2
    log_level: str = "info"
    block_private_ips: bool = True
    strict_mime: bool = False
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8766
    stream_chunk_bytes: int = 64 * 1024
    max_memory_chunk_bytes: int = 64 * 1024 * 1024

    @property
    def max_file_size_bytes(self) -> int:
        return int(self.max_file_size_mb * 1024 * 1024)


def load_settings() -> Settings:
    settings = Settings()
    settings.allowed_dir = _default_allowed_dir()
    settings.max_file_size_mb = _float("MCP_MAX_FILE_SIZE_MB", settings.max_file_size_mb)
    settings.timeout_sec = _float("MCP_TIMEOUT_SEC", settings.timeout_sec)
    settings.concurrent = max(1, _int("MCP_CONCURRENT", settings.concurrent))
    settings.user_agent = os.environ.get("MCP_USER_AGENT", settings.user_agent)
    settings.proxy = os.environ.get("MCP_PROXY", settings.proxy).strip()
    settings.ssl_verify = _bool("MCP_SSL_VERIFY", settings.ssl_verify)
    settings.max_redirects = max(0, _int("MCP_MAX_REDIRECTS", settings.max_redirects))
    settings.retry_count = max(0, _int("MCP_RETRY_COUNT", settings.retry_count))
    settings.log_level = os.environ.get("MCP_LOG_LEVEL", settings.log_level).lower()
    settings.block_private_ips = _bool("MCP_BLOCK_PRIVATE_IPS", settings.block_private_ips)
    settings.strict_mime = _bool("MCP_STRICT_MIME", settings.strict_mime)
    settings.mcp_host = os.environ.get("MCP_HTTP_HOST", settings.mcp_host)
    settings.mcp_port = _int("MCP_HTTP_PORT", settings.mcp_port)
    return settings

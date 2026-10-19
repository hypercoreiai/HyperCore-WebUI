"""Path sandboxing, filename sanitization, and SSRF IP checks."""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from webscrape.errors import ACCESS_DENIED, INVALID_URL, ScrapeError

_ILLEGAL_CHARS = re.compile(r'[/\\:*?"<>|\x00]')


def ensure_allowed_dir(root: Path) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize_filename(name: str) -> str:
    if "\x00" in name:
        raise ScrapeError(ACCESS_DENIED, "filename contains a null byte")
    normalized = name.replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise ScrapeError(ACCESS_DENIED, "path traversal attempt in filename")
    cleaned = _ILLEGAL_CHARS.sub("_", normalized).strip().strip(".")
    if not cleaned or cleaned in {".", ".."}:
        return ""
    return cleaned


def resolve_in_allowed(root: Path, target: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve target under root. Raises ACCESS_DENIED on traversal."""
    root = ensure_allowed_dir(root)
    raw = Path(str(target))
    if "\x00" in str(target):
        raise ScrapeError(ACCESS_DENIED, "path contains a null byte")
    if raw.is_absolute():
        candidate = raw
    else:
        candidate = root / raw
    # Resolve existing parents so ../ cannot escape; keep final name if missing.
    parent = candidate.parent
    try:
        resolved_parent = parent.resolve()
    except OSError as exc:
        raise ScrapeError(ACCESS_DENIED, f"cannot resolve path: {exc}") from exc
    resolved = resolved_parent / candidate.name
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ScrapeError(ACCESS_DENIED, "target path is outside ALLOWED_DIR")
    if must_exist and not resolved.exists():
        raise ScrapeError(ACCESS_DENIED, f"file not found under ALLOWED_DIR: {resolved}")
    return resolved


def subfolder_in_allowed(root: Path, subfolder: str) -> Path:
    root = ensure_allowed_dir(root)
    cleaned = subfolder.replace("\\", "/").strip("/")
    if not cleaned:
        return root
    parts = []
    for part in cleaned.split("/"):
        name = sanitize_filename(part)
        if not name:
            raise ScrapeError(ACCESS_DENIED, "invalid subfolder segment")
        parts.append(name)
    path = root.joinpath(*parts)
    return resolve_in_allowed(root, path)


def is_private_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
        return True
    if addr.is_multicast or addr.is_unspecified:
        return True
    return False


def require_http_url(url: str) -> None:
    if not url or not isinstance(url, str):
        raise ScrapeError(INVALID_URL, "url is required", url=str(url or ""))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ScrapeError(INVALID_URL, "not a valid absolute http(s) URL", url=url)


def resolve_host_ips(host: str) -> list[str]:
    hostname = host.split("%")[0]
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    try:
        addr = ipaddress.ip_address(hostname)
        return [str(addr)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        from webscrape.errors import DNS_FAIL

        raise ScrapeError(DNS_FAIL, f"hostname did not resolve: {exc}", url=host) from exc
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        from webscrape.errors import DNS_FAIL

        raise ScrapeError(DNS_FAIL, "hostname did not resolve", url=host)
    return ips


def check_url_ssrf(url: str, *, block_private: bool) -> None:
    require_http_url(url)
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ScrapeError(INVALID_URL, "URL is missing a hostname", url=url)
    ips = resolve_host_ips(host)
    if not block_private:
        return
    for ip in ips:
        if is_private_ip(ip):
            raise ScrapeError(
                ACCESS_DENIED,
                f"SSRF guard blocked private/loopback IP {ip} for host {host}",
                url=url,
            )

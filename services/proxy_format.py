"""Lightweight proxy string normalization helpers."""

from __future__ import annotations

from urllib.parse import quote, urlparse


def _compact_proxy_url(host: str, port: str, credentials: str) -> str:
    username, separator, password = str(credentials or "").partition(":")
    if not separator or not username:
        return ""
    try:
        parsed = urlparse(f"//{host}:{port}")
        parsed_port = parsed.port
    except ValueError:
        return ""
    if not parsed.hostname or parsed_port is None:
        return ""
    normalized_host = parsed.hostname
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    return (
        f"http://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{normalized_host}:{parsed_port}"
    )


def _compact_proxy_host_port(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(f"//{str(value or '').strip()}")
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or port is None:
        return None
    return parsed.hostname, str(port)


def colon_proxy_to_url(url: str) -> str:
    """Convert common no-scheme proxy formats into a standard HTTP URL."""
    candidate = str(url or "").strip()
    if not candidate or "://" in candidate:
        return candidate

    host_first = candidate.split(":", 3)
    if len(host_first) == 4 and host_first[1].isdigit():
        host, port, username, password = host_first
        normalized = _compact_proxy_url(host, port, f"{username}:{password}")
        if normalized:
            return normalized

    credentials_first = candidate.rsplit(":", 2)
    if len(credentials_first) == 3 and credentials_first[2].isdigit():
        credentials, host, port = credentials_first
        normalized = _compact_proxy_url(host, port, credentials)
        if normalized:
            return normalized

    if "@" in candidate:
        left, right = candidate.rsplit("@", 1)
        right_host_port = _compact_proxy_host_port(right)
        if right_host_port:
            normalized = _compact_proxy_url(*right_host_port, left)
            if normalized:
                return normalized
        left_host_port = _compact_proxy_host_port(left)
        if left_host_port:
            normalized = _compact_proxy_url(*left_host_port, right)
            if normalized:
                return normalized

    if _compact_proxy_host_port(candidate):
        return f"http://{candidate}"
    return candidate

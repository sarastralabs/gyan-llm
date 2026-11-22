"""SSRF protection: only fetch public web pages, never internal services.

A research request makes the server fetch URLs chosen by search results (and, indirectly, by
whoever wrote those pages). Without these checks a malicious page could point the fetcher at
`http://redis:6379`, `http://127.0.0.1:11434` (Ollama) or a cloud metadata address.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_PORTS = {None, 80, 443}


def ip_is_public(address: str) -> bool:
    """True only for globally routable addresses (not private, loopback, link-local, CGNAT...)."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast


def _resolves_to_public_only(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ip_is_public(info[4][0]) for info in infos)


async def is_public_url(url: str) -> bool:
    """Reject non-http(s) URLs, odd ports, embedded credentials and non-public destinations."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password or port not in ALLOWED_PORTS:
        return False
    return await asyncio.to_thread(_resolves_to_public_only, parsed.hostname)

"""Download a web page as text, with SSRF, size, redirect and time limits."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from .safety import ip_is_public, is_public_url

MAX_REDIRECTS = 4
ALLOWED_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
USER_AGENT = "Mozilla/5.0 (compatible; GyanBot/0.2; +https://sarastralabs.com)"

UrlGuard = Callable[[str], Awaitable[bool]]


class PageFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        timeout_s: float = 8.0,
        max_bytes: int = 1_000_000,
        guard: UrlGuard = is_public_url,
    ) -> None:
        # The client must NOT follow redirects itself: each hop is checked here.
        self.client = client
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.guard = guard

    async def fetch(self, url: str) -> str | None:
        """Return the page HTML/text, or None if it is unsafe, slow, too big or not a web page."""
        try:
            return await asyncio.wait_for(self._fetch(url), timeout=self.timeout_s + 4)
        except (TimeoutError, httpx.HTTPError, ValueError):
            return None

    async def _fetch(self, url: str) -> str | None:
        current = url
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"}
        for _ in range(MAX_REDIRECTS + 1):
            if not await self.guard(current):
                return None
            request = self.client.build_request(
                "GET", current, headers=headers, timeout=self.timeout_s
            )
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200 or not self._peer_is_public(response):
                    return None
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype not in ALLOWED_TYPES:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= self.max_bytes:
                        break
                return bytes(body[: self.max_bytes]).decode(
                    response.encoding or "utf-8", errors="replace"
                )
            finally:
                await response.aclose()
        return None  # too many redirects

    @staticmethod
    def _peer_is_public(response: httpx.Response) -> bool:
        """Second check after connecting: the address we actually reached must be public.

        This closes the gap between the DNS lookup in the guard and the real connection
        (DNS rebinding). Test transports have no network stream, so they skip the check.
        """
        stream = response.extensions.get("network_stream")
        if stream is None:
            return True
        server = stream.get_extra_info("server_addr")
        return bool(server) and ip_is_public(str(server[0]))

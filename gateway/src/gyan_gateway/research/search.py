"""Web search through a self-hosted SearXNG instance (no API key needed)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from redis.asyncio import Redis

TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "yclid"}


class ResearchError(Exception):
    """Search or research could not be completed (the message is safe to show to clients)."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def normalize_url(url: str) -> str:
    """Drop fragments, tracking parameters and trailing slashes so duplicates compare equal."""
    parsed = urlparse(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", urlencode(query), ""))


def parse_results(payload: dict, limit: int) -> list[SearchResult]:
    seen: set[str] = set()
    results: list[SearchResult] = []
    for item in payload.get("results", []):
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            SearchResult(
                title=str(item.get("title") or url).strip(),
                url=url,
                snippet=str(item.get("content") or "").strip(),
            )
        )
        if len(results) >= limit:
            break
    return results


class SearxClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        redis: Redis,
        cache_ttl_s: int,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.redis = redis
        self.cache_ttl_s = cache_ttl_s
        self.timeout_s = timeout_s

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        digest = hashlib.sha256(f"{query.strip().lower()}|{limit}".encode()).hexdigest()
        cache_key = f"search:{digest}"
        cached = await self.redis.get(cache_key)
        if cached:
            return [SearchResult(**r) for r in json.loads(cached)]

        try:
            response = await self.client.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "safesearch": 1, "language": "auto"},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            results = parse_results(response.json(), limit)
        except (httpx.HTTPError, ValueError):
            raise ResearchError("Web search is unavailable right now.") from None

        if results:  # never cache an empty answer: it is often a temporary engine problem
            payload = json.dumps([r.__dict__ for r in results])
            await self.redis.set(cache_key, payload, ex=self.cache_ttl_s)
        return results

"""Search, fetch, extract and rank: turn a question into a few numbered, readable sources."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

from redis.asyncio import Redis

from .extract import Page, best_passages, extract_page, is_relevant
from .fetch import PageFetcher
from .search import SearchResult, SearxClient

MAX_PER_DOMAIN = 2
MAX_CACHED_CHARS = 30_000
FAILURE_TTL_S = 600
MIN_SNIPPET_CHARS = 40


@dataclass
class Source:
    n: int
    title: str
    url: str
    text: str
    from_page: bool  # False when only the search snippet was available


@dataclass
class Context:
    sources: list[Source] = field(default_factory=list)
    results_found: int = 0
    pages_read: int = 0


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _key(prefix: str, url: str) -> str:
    return f"{prefix}:{hashlib.sha256(url.encode()).hexdigest()}"


class Researcher:
    def __init__(
        self,
        search: SearxClient,
        fetcher: PageFetcher,
        redis: Redis,
        *,
        page_ttl_s: int = 86_400,
        fetch_concurrency: int = 4,
    ) -> None:
        self.search = search
        self.fetcher = fetcher
        self.redis = redis
        self.page_ttl_s = page_ttl_s
        self._fetch_gate = asyncio.Semaphore(fetch_concurrency)

    async def gather(
        self, query: str, max_sources: int, chars_per_source: int, chars_total: int
    ) -> Context:
        results = await self.search.search(query, limit=max(8, max_sources * 3))
        candidates = self._pick_candidates(results, max_sources * 2)
        pages = await asyncio.gather(*(self._load_page(r.url) for r in candidates))

        budget = min(chars_per_source, max(200, chars_total // max_sources))
        context = Context(results_found=len(results))
        for result, page in zip(candidates, pages, strict=True):
            if len(context.sources) >= max_sources:
                break
            if page is not None:
                context.pages_read += 1
                text = best_passages(page.text, query, budget)
                title = page.title or result.title
                from_page = True
            else:
                text = result.snippet[:budget]
                title = result.title
                from_page = False
                if len(text) < MIN_SNIPPET_CHARS or not is_relevant(query, f"{title} {text}"):
                    continue
            if text:
                context.sources.append(
                    Source(len(context.sources) + 1, title, result.url, text, from_page)
                )
        return context

    @staticmethod
    def _pick_candidates(results: list[SearchResult], limit: int) -> list[SearchResult]:
        """Keep search order, but not more than MAX_PER_DOMAIN results from one site."""
        per_domain: dict[str, int] = {}
        chosen: list[SearchResult] = []
        for result in results:
            domain = _domain(result.url)
            if per_domain.get(domain, 0) >= MAX_PER_DOMAIN:
                continue
            per_domain[domain] = per_domain.get(domain, 0) + 1
            chosen.append(result)
            if len(chosen) >= limit:
                break
        return chosen

    async def _load_page(self, url: str) -> Page | None:
        """Cached page text, or a fresh fetch. Never raises: a failed page is just skipped."""
        try:
            cached = await self.redis.get(_key("page", url))
            if cached:
                data = json.loads(cached)
                return Page(title=data["title"], text=data["text"])
            if await self.redis.get(_key("pagefail", url)):
                return None

            async with self._fetch_gate:
                html = await self.fetcher.fetch(url)
            page = extract_page(html, url) if html else None
            if page is None:
                await self.redis.set(_key("pagefail", url), "1", ex=FAILURE_TTL_S)
                return None
            payload = json.dumps({"title": page.title, "text": page.text[:MAX_CACHED_CHARS]})
            await self.redis.set(_key("page", url), payload, ex=self.page_ttl_s)
            return Page(page.title, page.text[:MAX_CACHED_CHARS])
        except Exception:
            return None

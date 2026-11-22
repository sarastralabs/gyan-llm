"""The research flow as a stream of events.

Events (dicts):
    {"type": "status",  "text": "..."}
    {"type": "sources", "sources": [{"n", "title", "url", "from_page"}]}
    {"type": "token",   "text": "..."}
    {"type": "error",   "code": "...", "status": 5xx/4xx, "text": "..."}
    {"type": "done",    "usage": {"prompt": n, "completion": n}}

Both /v1/research and the `gyan-research` chat model are built on this.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from ..deps import Services
from ..errors import ApiError
from ..store import ApiKey
from .prompts import build_messages
from .search import ResearchError


@dataclass(frozen=True)
class ResearchParams:
    query: str
    model: str
    max_sources: int


def _error(code: str, status: int, text: str) -> dict:
    return {"type": "error", "code": code, "status": status, "text": text}


async def research_events(
    svc: Services, key: ApiKey, params: ResearchParams, endpoint: str
) -> AsyncIterator[dict]:
    """Run one research request and record its usage, even if the client disconnects."""
    started = time.monotonic()
    state: dict[str, Any] = {"status": 200, "prompt": 0, "completion": 0}
    try:
        async for event in _run(svc, params, state):
            yield event
    finally:
        await svc.store.log_usage(
            key_id=key.id,
            endpoint=endpoint,
            model=params.model,
            status=state["status"],
            prompt_tokens=state["prompt"],
            completion_tokens=state["completion"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )


async def _run(svc: Services, params: ResearchParams, state: dict[str, Any]) -> AsyncIterator[dict]:
    cfg = svc.settings

    def fail(code: str, status: int, text: str) -> dict:
        state["status"] = status
        return _error(code, status, text)

    yield {"type": "status", "text": "Searching the web..."}
    try:
        await asyncio.wait_for(svc.research_gate.acquire(), cfg.queue_timeout_s)
    except TimeoutError:
        yield fail("server_busy", 503, "Too many research requests. Please retry shortly.")
        return
    try:
        context = await svc.researcher.gather(
            params.query, params.max_sources, cfg.research_chars_per_source,
            cfg.research_chars_total,
        )
    except ResearchError as exc:
        yield fail("search_unavailable", 502, str(exc))
        return
    finally:
        svc.research_gate.release()

    if not context.sources:
        yield fail("no_sources", 404, "No readable web sources were found for this question.")
        return
    yield {
        "type": "sources",
        "sources": [
            {"n": s.n, "title": s.title, "url": s.url, "from_page": s.from_page}
            for s in context.sources
        ],
    }

    yield {"type": "status", "text": "Writing the answer..."}
    body = {
        "model": params.model,
        "messages": build_messages(params.query, context.sources),
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": cfg.max_tokens_cap,
        "temperature": 0.2,
    }
    try:
        lease = await svc.slots.acquire()
    except ApiError as exc:
        yield fail(exc.code or "server_busy", exc.status, exc.message)
        return
    try:
        async for kind, value in svc.ollama.stream_chat(body):
            if kind == "token":
                yield {"type": "token", "text": value}
            else:
                state["prompt"], state["completion"] = value["prompt"], value["completion"]
    except ApiError as exc:
        yield fail(exc.code or "upstream_error", exc.status, exc.message)
        return
    except httpx.HTTPError:
        yield fail("upstream_error", 502, "The model stream was interrupted.")
        return
    finally:
        await lease.release()

    yield {"type": "done", "usage": {"prompt": state["prompt"], "completion": state["completion"]}}

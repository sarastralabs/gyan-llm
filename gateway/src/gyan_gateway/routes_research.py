"""Research endpoints.

POST /v1/research                 search the web, read pages, answer with numbered sources
model "gyan-research"         the same thing through /v1/chat/completions, so any
                                  OpenAI-compatible tool or chat UI can use it
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .deps import Services, current_key, services
from .errors import ApiError, server_busy
from .models import resolve_model
from .research.service import ResearchParams, research_events
from .schemas import ChatRequest, parse_body
from .store import ApiKey

router = APIRouter()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=3, max_length=500)
    model: str | None = None
    max_sources: int | None = Field(None, ge=1, le=6)
    stream: bool = False


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def with_keepalive(chunks: AsyncIterator[str], interval_s: float) -> AsyncIterator[str]:
    """Pass chunks through, adding SSE comment lines while waiting.

    Research can be quiet for 10 to 30 seconds (searching, reading pages); without a sign of
    life, proxies and some clients give up. SSE comments are ignored by every client.
    """
    iterator = chunks.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval_s)
            if not done:
                yield ": keepalive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            yield item
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        if not pending.done():
            pending.cancel()
        with contextlib.suppress(BaseException):
            await pending
        await chunks.aclose()  # type: ignore[attr-defined]


async def collect(events: AsyncIterator[dict]) -> dict[str, Any]:
    """Run the whole flow and return the final answer, or raise the first error as HTTP."""
    answer: list[str] = []
    sources: list[dict] = []
    usage = {"prompt": 0, "completion": 0}
    try:
        async for event in events:
            kind = event["type"]
            if kind == "token":
                answer.append(event["text"])
            elif kind == "sources":
                sources = event["sources"]
            elif kind == "done":
                usage = event["usage"]
            elif kind == "error":
                status = event["status"]
                raise ApiError(
                    status, event["text"],
                    type_="server_error" if status >= 500 else "invalid_request_error",
                    code=event["code"],
                    headers={"Retry-After": "10"} if status == 503 else {},
                )
    finally:
        await events.aclose()  # type: ignore[attr-defined]
    return {"answer": "".join(answer), "sources": sources, "usage": usage}


def sources_markdown(sources: list[dict]) -> str:
    """Sources as a Markdown list, for chat UIs that only render the answer text."""
    if not sources:
        return ""
    lines = [f"{s['n']}. [{s['title'] or s['url']}]({s['url']})" for s in sources]
    return "\n\n---\n**Sources**\n" + "\n".join(lines)


@router.post("/v1/research")
async def research(request: Request, key: ApiKey = Depends(current_key)):
    svc = services(request)
    req = parse_body(await request.body(), svc.settings.max_body_bytes, ResearchRequest)
    rate_headers = await svc.limiter.check_key(key, cost=svc.settings.research_cost)
    model = await resolve_model(svc, req.model)
    params = ResearchParams(
        query=req.query.strip(),
        model=model,
        max_sources=req.max_sources or svc.settings.research_max_sources,
    )
    if await svc.slots.would_refuse():
        raise server_busy()

    events = research_events(svc, key, params, "/v1/research")
    if not req.stream:
        result = await collect(events)
        body = {"id": "research-" + uuid.uuid4().hex[:12], "object": "research", "model": model,
                "query": params.query, **result}
        return JSONResponse(body, headers=rate_headers)

    async def frames() -> AsyncIterator[str]:
        async for event in events:
            yield _sse(event)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        with_keepalive(frames(), svc.settings.stream_keepalive_s),
        media_type="text/event-stream",
        headers={**rate_headers, **SSE_HEADERS},
    )


async def research_chat(
    svc: Services, key: ApiKey, req: ChatRequest, rate_headers: dict[str, str]
):
    """`model: "gyan-research"` in /v1/chat/completions. Uses the last user message."""
    query = next(
        (m.flat_text() for m in reversed(req.messages) if m.role == "user"), ""
    ).strip()[:500]
    if len(query) < 3:
        raise ApiError(400, "Send a question as the last user message.", code="invalid_request")
    model = await resolve_model(svc, None)
    if await svc.slots.would_refuse():
        raise server_busy()

    name = svc.settings.research_model_name
    events = research_events(
        svc, key, ResearchParams(query, model, svc.settings.research_max_sources),
        "/v1/chat/completions",
    )
    chat_id, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {
            "id": chat_id, "object": "chat.completion.chunk", "created": created, "model": name,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    if not req.stream:
        result = await collect(events)
        usage = result["usage"]
        return JSONResponse(
            {
                "id": chat_id, "object": "chat.completion", "created": created, "model": name,
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": result["answer"] + sources_markdown(result["sources"]),
                    },
                }],
                "usage": {
                    "prompt_tokens": usage["prompt"], "completion_tokens": usage["completion"],
                    "total_tokens": usage["prompt"] + usage["completion"],
                },
            },
            headers=rate_headers,
        )

    wants_usage = bool(req.stream_options and req.stream_options.include_usage)

    async def frames() -> AsyncIterator[str]:
        sources: list[dict] = []
        usage = {"prompt": 0, "completion": 0}
        yield _sse(chunk({"role": "assistant", "content": ""}))
        async for event in events:
            kind = event["type"]
            if kind == "token":
                yield _sse(chunk({"content": event["text"]}))
            elif kind == "sources":
                sources = event["sources"]
            elif kind == "done":
                usage = event["usage"]
            elif kind == "error":
                yield _sse(chunk({"content": f"\n\n(Research failed: {event['text']})"}))
        if sources:
            yield _sse(chunk({"content": sources_markdown(sources)}))
        yield _sse(chunk({}, "stop"))
        if wants_usage:
            yield _sse({
                "id": chat_id, "object": "chat.completion.chunk", "created": created,
                "model": name, "choices": [],
                "usage": {
                    "prompt_tokens": usage["prompt"], "completion_tokens": usage["completion"],
                    "total_tokens": usage["prompt"] + usage["completion"],
                },
            })
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        with_keepalive(frames(), svc.settings.stream_keepalive_s),
        media_type="text/event-stream",
        headers={**rate_headers, **SSE_HEADERS},
    )

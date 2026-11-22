"""OpenAI-compatible endpoints: GET /v1/models and POST /v1/chat/completions."""
from __future__ import annotations

import json
import math
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .deps import current_key, services
from .errors import ApiError, error_body
from .models import public_models, visible_models
from .routes_research import research_chat
from .schemas import ChatRequest, build_upstream_body, parse_body
from .store import ApiKey
from .vision import count_images

router = APIRouter()


def _tokens_from_chars(chars: int) -> int:
    return math.ceil(chars / 4) if chars else 0


@router.get("/v1/models")
async def list_models(request: Request, key: ApiKey = Depends(current_key)) -> JSONResponse:
    svc = services(request)
    headers = await svc.limiter.check_key(key)
    models = await public_models(svc)
    data = [{"id": m, "object": "model", "created": 0, "owned_by": "sarastra-labs"} for m in models]
    return JSONResponse({"object": "list", "data": data}, headers=headers)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, key: ApiKey = Depends(current_key)):
    svc = services(request)
    started = time.monotonic()

    raw = await request.body()
    req = parse_body(raw, svc.settings.chat_body_limit, ChatRequest)
    images = count_images(req)
    if not images and len(raw) > svc.settings.max_body_bytes:
        raise ApiError(413, "Request body too large.", code="body_too_large")
    model = req.model or svc.settings.default_model
    is_research = model == svc.settings.research_model_name
    # Web research is much more expensive than a plain chat, so it uses more of the budget.
    cost = svc.settings.research_cost if is_research else 1
    cost += images * svc.settings.image_cost
    rate_headers = await svc.limiter.check_key(key, cost=cost)
    if images:
        req = await svc.vision.resolve(req)  # images become text; see vision.py

    if is_research:
        return await research_chat(svc, key, req, rate_headers)
    if model not in await visible_models(svc):
        raise ApiError(404, f"The model '{model}' does not exist.", code="model_not_found")

    body = build_upstream_body(
        req, model, svc.settings.max_tokens_cap,
        svc.settings.max_total_chars, svc.settings.max_messages,
    )
    prompt_chars = sum(len(m["content"]) for m in body["messages"])

    async def record(status: int, prompt: int, completion: int) -> None:
        await svc.store.log_usage(
            key_id=key.id, endpoint="/v1/chat/completions", model=model, status=status,
            prompt_tokens=prompt, completion_tokens=completion,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    lease = await svc.slots.acquire()  # 503 when the line is full
    try:
        upstream = await svc.ollama.open_chat(body)
    except ApiError as exc:
        await lease.release()
        await record(exc.status, 0, 0)
        raise
    except BaseException:
        await lease.release()
        raise

    if not req.stream:
        try:
            data: dict[str, Any] = json.loads(await upstream.aread())
        except (json.JSONDecodeError, httpx.HTTPError):
            await record(502, 0, 0)
            raise ApiError(
                502, "Invalid response from the model server.",
                type_="server_error", code="upstream_error",
            ) from None
        finally:
            await upstream.aclose()
            await lease.release()
        usage = data.get("usage") or {}
        await record(200, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return JSONResponse(data, headers=rate_headers)

    wants_usage = bool(req.stream_options and req.stream_options.include_usage)
    totals = {"prompt": 0, "completion": 0, "delta_chars": 0, "have_usage": False, "status": 200}
    finished = False

    async def cleanup() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        await upstream.aclose()
        await lease.release()
        if totals["have_usage"]:
            prompt, completion = totals["prompt"], totals["completion"]
        else:
            prompt = _tokens_from_chars(prompt_chars)
            completion = _tokens_from_chars(totals["delta_chars"])
        await record(totals["status"], prompt, completion)

    async def sse() -> AsyncIterator[str]:
        try:
            async for line in upstream.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    yield "data: [DONE]\n\n"
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage")
                if usage:
                    totals["prompt"] = usage.get("prompt_tokens", 0)
                    totals["completion"] = usage.get("completion_tokens", 0)
                    totals["have_usage"] = True
                    if not chunk.get("choices") and not wants_usage:
                        continue  # usage-only chunk the client did not ask for
                for choice in chunk.get("choices") or []:
                    totals["delta_chars"] += len((choice.get("delta") or {}).get("content") or "")
                yield f"data: {payload}\n\n"
        except httpx.HTTPError:
            totals["status"] = 502
            err = error_body("The model stream was interrupted.", "server_error", "upstream_error")
            yield f"data: {json.dumps(err)}\n\n"
        finally:
            await cleanup()

    headers = {**rate_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        sse(), media_type="text/event-stream", headers=headers, background=BackgroundTask(cleanup)
    )

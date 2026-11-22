"""Application factory.

Run:  uvicorn gyan_gateway.main:app_factory --factory --host 127.0.0.1 --port 8000
(use a single worker: the generation slot is held per process)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from . import __version__, routes_admin, routes_chat, routes_research
from .config import Settings
from .deps import Services
from .errors import ApiError, api_error_handler, error_body
from .limits import Limiter, SlotPool
from .ollama import OllamaClient
from .research.fetch import USER_AGENT, PageFetcher
from .research.pipeline import Researcher
from .research.search import SearxClient
from .store import Store
from .vision import VisionService

log = logging.getLogger("gyan.gateway")


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    http_client: httpx.AsyncClient | None = None,
    researcher: Researcher | None = None,
) -> FastAPI:
    """Build the app. The keyword arguments exist so tests can inject fakes."""
    cfg = settings or Settings()  # type: ignore[call-arg]  # ADMIN_TOKEN comes from the env

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_redis = redis is None
        own_http = http_client is None
        r = redis or Redis.from_url(cfg.redis_url, decode_responses=True)
        client = http_client or httpx.AsyncClient()
        # A separate client for fetching web pages: it never follows redirects by itself
        # (the fetcher checks every hop) and identifies itself politely.
        web_client = httpx.AsyncClient(
            follow_redirects=False, headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=20),
        )
        store = Store(cfg.database_path)
        slots = SlotPool(
            r, cfg.max_concurrent, cfg.max_queue, cfg.queue_timeout_s, cfg.max_slot_hold_s
        )
        await slots.reset()
        active_researcher = researcher or Researcher(
            SearxClient(cfg.searxng_url, client, r, cfg.search_cache_ttl_s),
            PageFetcher(web_client, timeout_s=cfg.fetch_timeout_s, max_bytes=cfg.fetch_max_bytes),
            r,
            page_ttl_s=cfg.page_cache_ttl_s,
            fetch_concurrency=cfg.fetch_concurrency,
        )
        ollama = OllamaClient(cfg.ollama_url, client, cfg.request_timeout_s)
        app.state.svc = Services(
            settings=cfg,
            store=store,
            limiter=Limiter(r, cfg.auth_failures_per_min),
            slots=slots,
            ollama=ollama,
            redis=r,
            researcher=active_researcher,
            vision=VisionService(ollama, r, slots, cfg),
            research_gate=asyncio.Semaphore(cfg.research_concurrency),
        )
        yield
        store.close()
        await web_client.aclose()
        if own_http:
            await client.aclose()
        if own_redis:
            await r.aclose()

    app = FastAPI(
        title="Gyan Gateway", version=__version__, docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan,
    )
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]

    if cfg.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origin_list,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.monotonic()
        declared = request.headers.get("content-length", "")
        is_chat = request.url.path == "/v1/chat/completions"
        limit = cfg.chat_body_limit if is_chat else cfg.max_body_bytes
        if declared.isdigit() and int(declared) > limit:
            return JSONResponse(
                error_body("Request body too large.", "invalid_request_error", "body_too_large"),
                status_code=413,
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        log.info(json.dumps({
            "event": "request",
            "id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": int((time.monotonic() - started) * 1000),
        }))
        return response

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        svc: Services = request.app.state.svc
        try:
            redis_ok = bool(await svc.redis.ping())
        except Exception:
            redis_ok = False
        ollama_ok = await svc.ollama.ping()
        ok = redis_ok and ollama_ok
        return JSONResponse(
            {"status": "ready" if ok else "degraded", "redis": redis_ok, "ollama": ollama_ok},
            status_code=200 if ok else 503,
        )

    app.include_router(routes_chat.router)
    app.include_router(routes_research.router)
    app.include_router(routes_admin.router)
    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return create_app()

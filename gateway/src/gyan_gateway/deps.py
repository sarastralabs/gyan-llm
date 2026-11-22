"""Shared service container and request authentication."""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .errors import rate_limited, unauthorized
from .limits import Limiter, SlotPool
from .ollama import OllamaClient
from .research.pipeline import Researcher
from .store import ApiKey, Store
from .vision import VisionService


@dataclass
class Services:
    settings: Settings
    store: Store
    limiter: Limiter
    slots: SlotPool
    ollama: OllamaClient
    redis: object
    researcher: Researcher
    vision: VisionService
    # Caps how many research pipelines (search + fetch) run at once.
    research_gate: asyncio.Semaphore


def services(request: Request) -> Services:
    return request.app.state.svc


def client_id(request: Request, settings: Settings) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


async def _guard_attempts(request: Request, svc: Services) -> str:
    cid = client_id(request, svc.settings)
    if await svc.limiter.auth_blocked(cid):
        raise rate_limited(60, svc.settings.auth_failures_per_min, "too many failed attempts")
    return cid


async def current_key(request: Request) -> ApiKey:
    """FastAPI dependency: the active API key sent as `Authorization: Bearer <key>`."""
    svc = services(request)
    cid = await _guard_attempts(request, svc)
    token = bearer_token(request)
    key = await svc.store.find_by_key(token) if token else None
    if key is None or not key.active:
        await svc.limiter.note_auth_failure(cid)
        raise unauthorized()
    await svc.store.touch(key.id)
    return key


async def require_admin(request: Request) -> None:
    """FastAPI dependency: the admin token, for /admin routes."""
    svc = services(request)
    cid = await _guard_attempts(request, svc)
    token = bearer_token(request) or request.headers.get("x-admin-token", "")
    if not token or not secrets.compare_digest(
        token.encode("utf-8"), svc.settings.admin_token.encode("utf-8")
    ):
        await svc.limiter.note_auth_failure(cid)
        raise unauthorized("Invalid or missing admin token.")

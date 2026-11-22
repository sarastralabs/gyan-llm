"""Admin API (protected by ADMIN_TOKEN): manage API keys, see usage and queue state."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .deps import require_admin, services
from .errors import ApiError
from .models import visible_models
from .store import ApiKey

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    rpm: int | None = Field(None, ge=1, le=10_000)
    daily: int | None = Field(None, ge=1, le=1_000_000)


def _public(key: ApiKey) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "rpm": key.rpm,
        "daily": key.daily,
        "active": key.active,
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
    }


@router.post("/keys", status_code=201)
async def create_key(body: NewKey, request: Request) -> dict:
    svc = services(request)
    key, plain = await svc.store.create_key(
        body.name, body.rpm or svc.settings.default_rpm, body.daily or svc.settings.default_daily
    )
    # The plain key is returned exactly once and cannot be recovered later.
    return {**_public(key), "key": plain}


@router.get("/keys")
async def list_keys(request: Request) -> dict:
    keys = await services(request).store.list_keys()
    return {"data": [_public(k) for k in keys]}


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: int, request: Request) -> dict:
    if not await services(request).store.set_active(key_id, False):
        raise ApiError(404, "Key not found.", code="key_not_found")
    return {"id": key_id, "active": False}


@router.post("/keys/{key_id}/restore")
async def restore_key(key_id: int, request: Request) -> dict:
    if not await services(request).store.set_active(key_id, True):
        raise ApiError(404, "Key not found.", code="key_not_found")
    return {"id": key_id, "active": True}


@router.get("/usage")
async def usage(request: Request, hours: int = Query(24, ge=1, le=24 * 90)) -> dict:
    rows = await services(request).store.usage_summary(time.time() - hours * 3600)
    return {"hours": hours, "data": rows}


@router.get("/stats")
async def stats(request: Request) -> dict:
    svc = services(request)
    try:
        redis_ok = bool(await svc.redis.ping())
    except Exception:
        redis_ok = False
    ollama_ok = await svc.ollama.ping()
    models: list[str] = []
    if ollama_ok:
        try:
            models = await visible_models(svc)
        except ApiError:
            ollama_ok = False
    queue = await svc.slots.snapshot() if redis_ok else {"waiting": 0, "running": 0}
    return {"redis": redis_ok, "ollama": ollama_ok, "models": models, "queue": queue}

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest
from conftest import ADMIN, bearer, new_key

from gyan_gateway.errors import ApiError
from gyan_gateway.limits import SlotPool

MSG = {"messages": [{"role": "user", "content": "hi"}]}


def test_per_minute_limit(client):
    key = new_key(client, rpm=2)
    ok = [client.post("/v1/chat/completions", json=MSG, headers=bearer(key)) for _ in range(2)]
    assert [r.status_code for r in ok] == [200, 200]
    assert ok[0].headers["X-RateLimit-Remaining"] == "1"
    blocked = client.post("/v1/chat/completions", json=MSG, headers=bearer(key))
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json()["error"]["type"] == "rate_limit_error"


def test_daily_quota(client):
    key = new_key(client, rpm=100, daily=2)
    codes = [
        client.post("/v1/chat/completions", json=MSG, headers=bearer(key)).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


def test_limits_are_per_key(client):
    a, b = new_key(client, "a", rpm=1), new_key(client, "b", rpm=1)
    assert client.get("/v1/models", headers=bearer(a)).status_code == 200
    assert client.get("/v1/models", headers=bearer(a)).status_code == 429
    assert client.get("/v1/models", headers=bearer(b)).status_code == 200


def test_stats_show_an_idle_queue(client):
    stats = client.get("/admin/stats", headers=ADMIN).json()
    assert stats["redis"] and stats["ollama"]
    assert stats["queue"] == {"waiting": 0, "running": 0}
    assert "qwen2.5:3b" in stats["models"]


def test_readyz(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200 and resp.json()["status"] == "ready"


# --------------------------------------------------------------------------- SlotPool


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def pool(redis, **kw) -> SlotPool:
    args = {"max_concurrent": 1, "max_queue": 0, "queue_timeout_s": 1.0, "max_hold_s": 30.0}
    args.update(kw)
    return SlotPool(redis, **args)


async def test_second_request_is_refused_when_line_is_full(redis):
    p = pool(redis)
    lease = await p.acquire()
    assert (await p.snapshot())["running"] == 1
    with pytest.raises(ApiError) as exc:
        await p.acquire()
    assert exc.value.status == 503 and exc.value.headers["Retry-After"]
    await lease.release()
    again = await p.acquire()
    await again.release()
    assert await p.snapshot() == {"waiting": 0, "running": 0}


async def test_release_is_idempotent(redis):
    p = pool(redis)
    lease = await p.acquire()
    await lease.release()
    await lease.release()  # must not hand out a second slot
    first = await p.acquire()
    with pytest.raises(ApiError):
        await p.acquire()
    await first.release()


async def test_waiting_request_gets_the_slot_when_freed(redis):
    p = pool(redis, max_queue=2)
    first = await p.acquire()
    waiter = asyncio.create_task(p.acquire())
    await asyncio.sleep(0.05)
    assert (await p.snapshot())["waiting"] == 1
    await first.release()
    second = await asyncio.wait_for(waiter, 1)
    assert (await p.snapshot())["running"] == 1
    await second.release()


async def test_wait_times_out(redis):
    p = pool(redis, max_queue=2, queue_timeout_s=0.05)
    first = await p.acquire()
    with pytest.raises(ApiError) as exc:
        await p.acquire()
    assert exc.value.status == 503
    assert (await p.snapshot())["waiting"] == 0
    await first.release()


async def test_watchdog_frees_a_stuck_slot(redis):
    p = pool(redis, max_hold_s=0.05)
    await p.acquire()  # never released by the caller
    await asyncio.sleep(0.2)
    lease = await p.acquire()
    await lease.release()


async def test_reset_clears_counters(redis):
    p = pool(redis)
    await redis.set("queue:waiting", 5)
    await p.reset()
    assert await p.snapshot() == {"waiting": 0, "running": 0}

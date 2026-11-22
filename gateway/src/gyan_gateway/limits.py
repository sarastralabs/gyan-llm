"""Redis-backed limits: per-key rate and daily quota, brute-force guard, and the request queue."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from redis.asyncio import Redis

from .errors import rate_limited, server_busy
from .store import ApiKey


class Limiter:
    """Fixed-window counters in Redis. Cheap, and exact enough for API quotas."""

    def __init__(self, redis: Redis, auth_failures_per_min: int) -> None:
        self.redis = redis
        self.auth_failures_per_min = auth_failures_per_min

    async def check_key(self, key: ApiKey, cost: int = 1) -> dict[str, str]:
        """Count a request for `key`. Raises 429 when over the per-minute or daily limit.

        `cost` lets expensive requests (web research) use more of the budget than a plain chat.
        Returns rate-limit headers to attach to the response.
        """
        now = time.time()
        minute = int(now // 60)
        rpm_key = f"rl:{key.id}:{minute}"
        used = await self.redis.incrby(rpm_key, cost)
        if used == cost:
            await self.redis.expire(rpm_key, 90)
        if used > key.rpm:
            raise rate_limited(max(1, 60 - int(now) % 60), key.rpm, "per minute")

        day = datetime.now(UTC).strftime("%Y%m%d")
        day_key = f"quota:{key.id}:{day}"
        today = await self.redis.incrby(day_key, cost)
        if today == cost:
            await self.redis.expire(day_key, 90_000)
        if today > key.daily:
            secs_left = 86_400 - int(now) % 86_400
            raise rate_limited(secs_left, key.daily, "per day")

        return {
            "X-RateLimit-Limit": str(key.rpm),
            "X-RateLimit-Remaining": str(max(0, key.rpm - used)),
        }

    async def auth_blocked(self, client_id: str) -> bool:
        raw = await self.redis.get(f"authfail:{client_id}")
        return raw is not None and int(raw) >= self.auth_failures_per_min

    async def note_auth_failure(self, client_id: str) -> None:
        key = f"authfail:{client_id}"
        n = await self.redis.incr(key)
        if n == 1:
            await self.redis.expire(key, 60)


class Lease:
    """A held generation slot. `release()` is safe to call more than once."""

    def __init__(self, pool: SlotPool) -> None:
        self._pool = pool
        self._released = False
        self._watchdog: asyncio.TimerHandle | None = None

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._watchdog:
            self._watchdog.cancel()
        await self._pool._release()


class SlotPool:
    """Limits concurrent generations and bounds the waiting line.

    One Ollama instance on 2 CPU cores serves one request at a time, so extra requests wait
    here (up to `max_queue`) instead of piling up inside the model server. Counters are kept
    in Redis so `/admin/stats` can show them; the semaphore itself is per process, so run the
    gateway with a single worker.
    """

    def __init__(
        self,
        redis: Redis,
        max_concurrent: int,
        max_queue: int,
        queue_timeout_s: float,
        max_hold_s: float,
    ) -> None:
        self.redis = redis
        self.max_queue = max_queue
        self.queue_timeout_s = queue_timeout_s
        self.max_hold_s = max_hold_s
        self._sem = asyncio.Semaphore(max_concurrent)

    async def reset(self) -> None:
        await self.redis.set("queue:waiting", 0)
        await self.redis.set("queue:running", 0)

    async def would_refuse(self) -> bool:
        """True when a new request would be turned away right now (line full, slot busy).

        Lets long-running endpoints refuse with a proper HTTP 503 before they start streaming.
        """
        waiting = int(await self.redis.get("queue:waiting") or 0)
        return self._sem.locked() and waiting >= self.max_queue

    async def acquire(self) -> Lease:
        """Wait for a free slot. Raises 503 when the line is full or the wait times out."""
        waiting = await self.redis.incr("queue:waiting")
        try:
            if waiting > self.max_queue and self._sem.locked():
                raise server_busy()
            try:
                await asyncio.wait_for(self._sem.acquire(), self.queue_timeout_s)
            except TimeoutError:
                raise server_busy() from None
        finally:
            await self.redis.decr("queue:waiting")

        await self.redis.incr("queue:running")
        lease = Lease(self)
        # Safety net for a client that disconnects without the response ever finishing.
        lease._watchdog = asyncio.get_running_loop().call_later(
            self.max_hold_s, lambda: asyncio.ensure_future(lease.release())
        )
        return lease

    async def _release(self) -> None:
        self._sem.release()
        await self.redis.decr("queue:running")

    async def snapshot(self) -> dict[str, int]:
        waiting = int(await self.redis.get("queue:waiting") or 0)
        running = int(await self.redis.get("queue:running") or 0)
        return {"waiting": max(0, waiting), "running": max(0, running)}

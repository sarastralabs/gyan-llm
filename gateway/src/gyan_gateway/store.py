"""Durable storage (SQLite): API keys and usage records.

Redis holds only short-lived state (rate counters, queue). Keys and usage live here so a
Redis restart never loses a customer's key. Calls are synchronous and short; the async
wrappers in `Store` run them in a worker thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

KEY_PREFIX = "sk-gyan-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    key_hash     TEXT    NOT NULL UNIQUE,
    prefix       TEXT    NOT NULL,
    rpm          INTEGER NOT NULL,
    daily        INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   REAL    NOT NULL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id            INTEGER NOT NULL REFERENCES api_keys(id),
    ts                REAL    NOT NULL,
    endpoint          TEXT    NOT NULL,
    model             TEXT,
    status            INTEGER NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage (key_id, ts);
"""


def generate_key() -> str:
    """A new random API key. Shown to its owner once; only the hash is stored."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    # Keys are 256-bit random values, so a plain SHA-256 is enough (no salt or slow hash needed).
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApiKey:
    id: int
    name: str
    rpm: int
    daily: int
    active: bool
    prefix: str = ""
    created_at: float = 0.0
    last_used_at: float | None = None


def _row_to_key(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        id=row["id"],
        name=row["name"],
        rpm=row["rpm"],
        daily=row["daily"],
        active=bool(row["active"]),
        prefix=row["prefix"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ sync core

    def _create_key(self, name: str, rpm: int, daily: int) -> tuple[ApiKey, str]:
        plain = generate_key()
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO api_keys (name, key_hash, prefix, rpm, daily, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, hash_key(plain), plain[:12], rpm, daily, now),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM api_keys WHERE id=?", (cur.lastrowid,)).fetchone()
        return _row_to_key(row), plain

    def _find_by_key(self, plain: str) -> ApiKey | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM api_keys WHERE key_hash=?", (hash_key(plain),)
            ).fetchone()
        return _row_to_key(row) if row else None

    def _list_keys(self) -> list[ApiKey]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
        return [_row_to_key(r) for r in rows]

    def _set_active(self, key_id: int, active: bool) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE api_keys SET active=? WHERE id=?", (1 if active else 0, key_id)
            )
            self._db.commit()
        return cur.rowcount > 0

    def _touch(self, key_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), key_id))
            self._db.commit()

    def _log_usage(
        self,
        key_id: int,
        endpoint: str,
        model: str | None,
        status: int,
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: int,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO usage (key_id, ts, endpoint, model, status, prompt_tokens,"
                " completion_tokens, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key_id, time.time(), endpoint, model, status, prompt_tokens,
                 completion_tokens, duration_ms),
            )
            self._db.commit()

    def _usage_summary(self, since: float) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT k.id AS key_id, k.name AS name, COUNT(u.id) AS requests,"
                " COALESCE(SUM(u.prompt_tokens),0) AS prompt_tokens,"
                " COALESCE(SUM(u.completion_tokens),0) AS completion_tokens,"
                " COALESCE(SUM(CASE WHEN u.status >= 400 THEN 1 ELSE 0 END),0) AS errors"
                " FROM api_keys k LEFT JOIN usage u ON u.key_id = k.id AND u.ts >= ?"
                " GROUP BY k.id ORDER BY k.id",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ async API

    async def create_key(self, name: str, rpm: int, daily: int) -> tuple[ApiKey, str]:
        return await asyncio.to_thread(self._create_key, name, rpm, daily)

    async def find_by_key(self, plain: str) -> ApiKey | None:
        return await asyncio.to_thread(self._find_by_key, plain)

    async def list_keys(self) -> list[ApiKey]:
        return await asyncio.to_thread(self._list_keys)

    async def set_active(self, key_id: int, active: bool) -> bool:
        return await asyncio.to_thread(self._set_active, key_id, active)

    async def touch(self, key_id: int) -> None:
        await asyncio.to_thread(self._touch, key_id)

    async def log_usage(self, **kwargs) -> None:
        await asyncio.to_thread(self._log_usage, **kwargs)

    async def usage_summary(self, since: float) -> list[dict]:
        return await asyncio.to_thread(self._usage_summary, since)

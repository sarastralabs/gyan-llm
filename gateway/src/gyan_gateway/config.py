"""Settings, read from environment variables (see .env.example at the repo root)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- backends
    ollama_url: str = "http://127.0.0.1:11434"
    redis_url: str = "redis://127.0.0.1:6379/0"
    database_path: str = "data/gateway.db"

    # --- admin (required; used for the /admin API)
    admin_token: str = Field(min_length=24)

    # --- models
    default_model: str = "qwen2.5:3b"
    # Comma-separated allowlist. Empty means "every model Ollama has installed".
    allowed_models: str = ""

    # --- capacity (a 2-core machine serves one generation at a time)
    max_concurrent: int = Field(1, ge=1)
    max_queue: int = Field(8, ge=0)
    queue_timeout_s: float = 120.0
    # Safety net: a slot is force-released after this long, even if a client vanished.
    max_slot_hold_s: float = 600.0
    request_timeout_s: float = 300.0

    # --- request limits
    max_tokens_cap: int = Field(1024, ge=1)
    max_body_bytes: int = 262_144
    max_total_chars: int = 24_000
    max_messages: int = 60

    # --- default per-key limits (override per key when creating it)
    default_rpm: int = Field(20, ge=1)
    default_daily: int = Field(500, ge=1)
    # Failed key attempts allowed per client address per minute (brute-force guard).
    auth_failures_per_min: int = Field(20, ge=1)

    # --- research (web search + page reading + cited answer)
    searxng_url: str = "http://127.0.0.1:8080"
    # Clients select research by using this as the `model` in /v1/chat/completions.
    research_model_name: str = "gyan-research"
    research_max_sources: int = Field(3, ge=1, le=6)
    # Context budget sent to the model. The model has a 4096-token window and the answer
    # needs room, so keep the total near 6000 characters (about 1500 tokens).
    research_chars_total: int = Field(6000, ge=500)
    research_chars_per_source: int = Field(2000, ge=200)
    # One research request costs this many units of a key's per-minute and daily budget.
    research_cost: int = Field(3, ge=1)
    # Research pipelines (search + fetch) running at once.
    research_concurrency: int = Field(2, ge=1)
    fetch_timeout_s: float = 8.0
    fetch_max_bytes: int = 1_000_000
    fetch_concurrency: int = Field(4, ge=1)
    search_cache_ttl_s: int = 3600
    page_cache_ttl_s: int = 86_400
    # Stops reading the web when idle; keeps connections alive for proxies with timeouts.
    stream_keepalive_s: float = 10.0

    # --- network
    # Only true behind a proxy you control (nginx), so the real client IP is used.
    trust_proxy: bool = False
    # Comma-separated origins allowed to call the API from a browser. Empty = none.
    cors_origins: str = ""

    @property
    def allowed_model_list(self) -> list[str]:
        return [m.strip() for m in self.allowed_models.split(",") if m.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

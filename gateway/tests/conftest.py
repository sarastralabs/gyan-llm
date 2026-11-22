"""Test fixtures: fake Redis, a fake Ollama (httpx.MockTransport) and an app wired to both."""
from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest
from fastapi.testclient import TestClient

from gyan_gateway.config import Settings
from gyan_gateway.main import create_app
from gyan_gateway.research.pipeline import Researcher
from gyan_gateway.research.search import ResearchError, SearchResult

ADMIN_TOKEN = "a" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
MODELS = ("qwen2.5:3b", "tiny:1b")
HELPER_MODELS = ("qwen2.5vl:3b",)  # the image describer: installed, but hidden

ARTICLE = """<html><head><title>Alpha Docs - Install</title></head><body>
<nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav>
<article><h1>Installing Alpha</h1>
<p>Alpha is a small tool for syncing files between servers. It is written in Rust and ships as a
single binary that needs no other software.</p>
<p>To install Alpha on Linux, download the release archive, unpack it and move the binary to
/usr/local/bin. Then run alpha --version to check the installation worked.</p>
<p>Alpha stores its configuration in ~/.config/alpha/config.toml. The file is created on first
run with safe defaults, so most people never need to edit it.</p>
</article><footer>Copyright Alpha Inc. Subscribe to our newsletter</footer>
<script>var tracker = 1;</script></body></html>"""

ALPHA = "https://alpha.example/docs"
BETA = "https://beta.example/post"
GAMMA = "https://gamma.example/x"

RESULTS = [
    SearchResult("Alpha Docs", ALPHA, "Alpha snippet about installing the tool on Linux systems."),
    SearchResult("Beta Blog", BETA, "Beta snippet about how to install alpha with a package manager on Linux."),
    SearchResult("Gamma", GAMMA, "too short"),
]
PAGES = {ALPHA: ARTICLE}  # beta and gamma "fail to load", so beta falls back to its snippet


class FakeSearch:
    def __init__(self, results: list[SearchResult], error: bool = False) -> None:
        self.results, self.error, self.queries = results, error, []

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        self.queries.append(query)
        if self.error:
            raise ResearchError("Web search is unavailable right now.")
        return list(self.results)


class FakeFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.calls = pages, []

    async def fetch(self, url: str) -> str | None:
        self.calls.append(url)
        return self.pages.get(url)


def make_ollama(captured: list[dict], vision_calls: list[dict] | None = None):
    """A stand-in for Ollama's /api/tags and OpenAI-compatible /v1/chat/completions."""

    vision_calls = [] if vision_calls is None else vision_calls

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            names = [*MODELS, *HELPER_MODELS]
            return httpx.Response(200, json={"models": [{"name": m} for m in names]})
        if request.url.path == "/api/chat":  # native API, used to read images
            body = json.loads(request.content)
            vision_calls.append(body)
            text = "A brown dog running on a beach at sunset."
            return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            captured.append(body)
            if body["model"] not in MODELS:
                return httpx.Response(404, json={"error": {"message": "model not found"}})
            if body.get("stream"):
                def chunk(delta: dict, finish: str | None = None) -> dict:
                    return {
                        "id": "c1", "object": "chat.completion.chunk", "model": body["model"],
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                events = [
                    chunk({"role": "assistant", "content": "Hello"}),
                    chunk({"content": " world"}),
                    chunk({}, "stop"),
                    {"id": "c1", "object": "chat.completion.chunk", "model": body["model"],
                     "choices": [],
                     "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}},
                ]
                text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
                return httpx.Response(
                    200, content=text.encode(), headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(200, json={
                "id": "c1", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "Hello world"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            })
        return httpx.Response(404)

    return handler


@pytest.fixture
def captured() -> list[dict]:
    return []


@pytest.fixture
def vision_calls() -> list[dict]:
    return []


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        admin_token=ADMIN_TOKEN,
        database_path=str(tmp_path / "gateway.db"),
        default_rpm=100,
        default_daily=1000,
        max_queue=2,
    )


@pytest.fixture
def make_client(captured, vision_calls):
    def build(
        settings: Settings,
        *,
        results: list[SearchResult] | None = None,
        pages: dict[str, str] | None = None,
        search_error: bool = False,
    ) -> TestClient:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        http = httpx.AsyncClient(transport=httpx.MockTransport(make_ollama(captured, vision_calls)))
        researcher = Researcher(
            FakeSearch(RESULTS if results is None else results, search_error),  # type: ignore[arg-type]
            FakeFetcher(PAGES if pages is None else pages),  # type: ignore[arg-type]
            redis,
        )
        app = create_app(settings, redis=redis, http_client=http, researcher=researcher)
        return TestClient(app)

    return build


@pytest.fixture
def client(settings, make_client):
    with make_client(settings) as c:
        yield c


def new_key(client: TestClient, name: str = "test", **limits) -> dict:
    resp = client.post("/admin/keys", json={"name": name, **limits}, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()


def bearer(key: dict) -> dict:
    return {"Authorization": f"Bearer {key['key']}"}

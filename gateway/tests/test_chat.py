from __future__ import annotations

import json

from conftest import ADMIN, bearer, new_key

from gyan_gateway.config import Settings

MSG = {"messages": [{"role": "user", "content": "hi"}]}


def sse_events(text: str) -> list[str]:
    return [line[6:] for line in text.split("\n\n") if line.startswith("data: ")]


def test_non_streaming_completion(client):
    key = new_key(client)
    resp = client.post("/v1/chat/completions", json=MSG, headers=bearer(key))
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello world"
    assert data["usage"]["total_tokens"] == 9
    assert "X-RateLimit-Remaining" in resp.headers and "X-Request-ID" in resp.headers


def test_streaming_completion(client):
    key = new_key(client)
    resp = client.post("/v1/chat/completions", json={**MSG, "stream": True}, headers=bearer(key))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = sse_events(resp.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Hello world"
    # The usage-only chunk is hidden unless the client asks for it.
    assert all(c["choices"] for c in chunks)


def test_streaming_usage_chunk_when_requested(client):
    key = new_key(client)
    body = {**MSG, "stream": True, "stream_options": {"include_usage": True}}
    resp = client.post("/v1/chat/completions", json=body, headers=bearer(key))
    chunks = [json.loads(e) for e in sse_events(resp.text)[:-1]]
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["completion_tokens"] == 2


def test_usage_is_recorded(client):
    key = new_key(client, "billing")
    client.post("/v1/chat/completions", json=MSG, headers=bearer(key))
    client.post("/v1/chat/completions", json={**MSG, "stream": True}, headers=bearer(key))
    rows = client.get("/admin/usage", headers=ADMIN).json()["data"]
    row = next(r for r in rows if r["name"] == "billing")
    assert row["requests"] == 2 and row["prompt_tokens"] == 14 and row["completion_tokens"] == 4


def test_unknown_model_is_404(client):
    key = new_key(client)
    resp = client.post("/v1/chat/completions", json={**MSG, "model": "gpt-4"}, headers=bearer(key))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


def test_default_model_used_when_missing(client, captured):
    key = new_key(client)
    client.post("/v1/chat/completions", json=MSG, headers=bearer(key))
    assert captured[-1]["model"] == "qwen2.5:3b"


def test_remote_image_urls_are_rejected(client):
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}]}
    resp = client.post("/v1/chat/completions", json=body, headers=bearer(key))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "unsupported_image"


def test_text_parts_are_flattened(client, captured):
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]}
    assert client.post("/v1/chat/completions", json=body, headers=bearer(key)).status_code == 200
    assert captured[-1]["messages"][0]["content"] == "a\nb"


def test_max_tokens_is_capped(client, captured):
    key = new_key(client)
    client.post("/v1/chat/completions", json={**MSG, "max_tokens": 99999}, headers=bearer(key))
    assert captured[-1]["max_tokens"] == 1024
    client.post("/v1/chat/completions", json=MSG, headers=bearer(key))
    assert captured[-1]["max_tokens"] == 1024
    client.post("/v1/chat/completions", json={**MSG, "max_tokens": 50}, headers=bearer(key))
    assert captured[-1]["max_tokens"] == 50


def test_server_side_options_cannot_be_set_by_clients(client, captured):
    key = new_key(client)
    body = {**MSG, "keep_alive": "24h", "options": {"num_ctx": 999999}, "user": "someone"}
    assert client.post("/v1/chat/completions", json=body, headers=bearer(key)).status_code == 200
    sent = captured[-1]
    assert "keep_alive" not in sent and "options" not in sent and "user" not in sent


def test_sampling_parameters_are_forwarded(client, captured):
    key = new_key(client)
    body = {**MSG, "temperature": 0.2, "top_p": 0.9, "seed": 7, "stop": ["END"]}
    client.post("/v1/chat/completions", json=body, headers=bearer(key))
    sent = captured[-1]
    got = (sent["temperature"], sent["top_p"], sent["seed"], sent["stop"])
    assert got == (0.2, 0.9, 7, ["END"])


def test_validation_errors_are_400(client):
    key = new_key(client)
    for bad in (
        {"messages": []},
        {"messages": [{"role": "robot", "content": "x"}]},
        {**MSG, "n": 2},
        {**MSG, "temperature": 5},
        {**MSG, "stop": ["1", "2", "3", "4", "5"]},
    ):
        resp = client.post("/v1/chat/completions", json=bad, headers=bearer(key))
        assert resp.status_code == 400, bad
        assert resp.json()["error"]["type"] == "invalid_request_error"


def test_invalid_json_is_400(client):
    key = new_key(client)
    headers = {**bearer(key), "Content-Type": "application/json"}
    resp = client.post("/v1/chat/completions", content=b"{nope", headers=headers)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_json"


def test_conversation_size_limits(client):
    key = new_key(client)
    long = {"messages": [{"role": "user", "content": "x" * 25000}]}
    resp = client.post("/v1/chat/completions", json=long, headers=bearer(key))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "context_length_exceeded"
    many = {"messages": [{"role": "user", "content": "x"}] * 61}
    assert client.post("/v1/chat/completions", json=many, headers=bearer(key)).status_code == 400


def test_huge_body_is_413(client):
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": "x" * 300_000}]}
    resp = client.post("/v1/chat/completions", json=body, headers=bearer(key))
    assert resp.status_code == 413


def test_allowed_models_restrict_visibility(settings, make_client):
    restricted = Settings(**{**settings.model_dump(), "allowed_models": "qwen2.5:3b"})
    with make_client(restricted) as client:
        key = new_key(client)
        ids = [m["id"] for m in client.get("/v1/models", headers=bearer(key)).json()["data"]]
        assert ids == ["qwen2.5:3b", "gyan-research"]  # real models + the research model
        resp = client.post(
            "/v1/chat/completions", json={**MSG, "model": "tiny:1b"}, headers=bearer(key)
        )
        assert resp.status_code == 404

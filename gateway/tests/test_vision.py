"""Images in chat requests: read once (OCR or vision model), turned into text, cached."""
from __future__ import annotations

import base64

from conftest import bearer, new_key

PNG = b"\x89PNG\r\n\x1a\n"


def data_url(payload: bytes, header: bytes = PNG) -> str:
    return "data:image/png;base64," + base64.b64encode(header + payload).decode()


def ask(client, key, *images: str, text: str = "What is in this?", model: str | None = None):
    parts = [{"type": "text", "text": text}]
    parts += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    body = {"messages": [{"role": "user", "content": parts}]}
    if model:
        body["model"] = model
    return client.post("/v1/chat/completions", json=body, headers=bearer(key))


def test_document_is_read_with_ocr_and_sent_as_text(client, captured, vision_calls):
    key = new_key(client)
    resp = ask(client, key, data_url(b"doc"))
    assert resp.status_code == 200
    assert [c["model"] for c in vision_calls] == ["glm-ocr"]
    sent = captured[-1]["messages"][0]["content"]
    assert "What is in this?" in sent and "INVOICE 4821" in sent and "[Attached image 1]" in sent
    assert "images" not in captured[-1]["messages"][0]  # the text model never gets pixels


def test_photo_without_text_goes_to_the_vision_model(client, captured, vision_calls):
    key = new_key(client)
    ask(client, key, data_url(b"photo"))
    assert [c["model"] for c in vision_calls] == ["glm-ocr", "qwen2.5vl:3b"]
    assert "brown dog" in captured[-1]["messages"][0]["content"]


def test_same_image_is_read_only_once(client, vision_calls):
    key = new_key(client)
    image = data_url(b"doc")
    ask(client, key, image)
    ask(client, key, image)  # a follow-up resends the whole conversation, image included
    ask(client, key, image, image)  # and the same picture twice in one request
    assert len(vision_calls) == 1


def test_works_for_the_research_model_too(client, vision_calls):
    key = new_key(client)
    resp = ask(client, key, data_url(b"doc"), model="gyan-research")
    assert resp.status_code == 200
    assert len(vision_calls) == 1


def test_remote_image_urls_are_not_fetched(client, vision_calls):
    key = new_key(client)
    resp = ask(client, key, "http://169.254.169.254/latest/meta-data")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "unsupported_image"
    assert vision_calls == []


def test_not_an_image_is_rejected(client, vision_calls):
    key = new_key(client)
    resp = ask(client, key, data_url(b"just text", header=b"<html>"))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_image"
    assert vision_calls == []


def test_bad_base64_is_rejected(client):
    key = new_key(client)
    resp = ask(client, key, "data:image/png;base64,@@@not base64@@@")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_image"


def test_too_many_images(client, settings, make_client):
    settings.max_images = 2
    with make_client(settings) as c:
        key = new_key(c)
        resp = ask(c, key, data_url(b"a"), data_url(b"b"), data_url(b"c"))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "too_many_images"


def test_image_too_large(client, settings, make_client):
    settings.max_image_bytes = 10_000
    with make_client(settings) as c:
        key = new_key(c)
        resp = ask(c, key, data_url(b"x" * 20_000))
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "image_too_large"


def test_images_cost_more_of_the_budget(client):
    key = new_key(client, rpm=10, daily=100)
    first = ask(client, key, data_url(b"doc"))  # 1 + 3 units
    assert first.headers["X-RateLimit-Remaining"] == "6"


def test_helper_models_are_hidden(client):
    key = new_key(client)
    ids = [m["id"] for m in client.get("/v1/models", headers=bearer(key)).json()["data"]]
    assert "glm-ocr" not in ids and "glm-ocr:latest" not in ids and "qwen2.5vl:3b" not in ids
    assert "qwen2.5:3b" in ids
    resp = client.post("/v1/chat/completions", headers=bearer(key), json={
        "model": "glm-ocr:latest", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 404


def test_large_text_only_body_still_rejected(client):
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": "x" * 300_000}]}
    resp = client.post("/v1/chat/completions", json=body, headers=bearer(key))
    assert resp.status_code == 413

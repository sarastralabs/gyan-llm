"""Images in chat requests: Tesseract first, vision model as fallback, text to the chat model."""
from __future__ import annotations

import base64

import pytest
from conftest import bearer, new_key

from gyan_gateway.vision import VisionService, parse_tsv

PNG = b"\x89PNG\r\n\x1a\n"


def data_url(payload: bytes, header: bytes = PNG) -> str:
    return "data:image/png;base64," + base64.b64encode(header + payload).decode()


@pytest.fixture(autouse=True)
def fake_tesseract(monkeypatch):
    """Stand-in for the real program: what it 'finds' depends on the test image's bytes."""
    calls: list[bytes] = []

    async def fake(self, data: bytes):
        calls.append(data)
        if b"photo" in data:
            return "", 0.0, 0
        if b"blurry" in data:
            return "tx qq", 31.0, 2
        return "INVOICE 4821 Total: Rs 6,050 (GST included)", 93.0, 7

    monkeypatch.setattr(VisionService, "_tesseract", fake)
    return calls


def ask(client, key, *images: str, text: str = "What is in this?", model: str | None = None):
    parts = [{"type": "text", "text": text}]
    parts += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    body = {"messages": [{"role": "user", "content": parts}]}
    if model:
        body["model"] = model
    return client.post("/v1/chat/completions", json=body, headers=bearer(key))


def test_document_is_read_by_tesseract_and_sent_as_text(
    client, captured, vision_calls, fake_tesseract
):
    key = new_key(client)
    resp = ask(client, key, data_url(b"doc"))
    assert resp.status_code == 200
    assert vision_calls == []  # the slow model was not needed
    assert len(fake_tesseract) == 1
    sent = captured[-1]["messages"][0]["content"]
    assert "What is in this?" in sent and "INVOICE 4821" in sent and "[Attached image 1]" in sent
    assert "images" not in captured[-1]["messages"][0]  # the text model never gets pixels


def test_photo_without_text_goes_to_the_vision_model(client, captured, vision_calls):
    key = new_key(client)
    ask(client, key, data_url(b"photo"))
    assert [c["model"] for c in vision_calls] == ["qwen2.5vl:3b"]
    assert "brown dog" in captured[-1]["messages"][0]["content"]


def test_unsure_ocr_falls_back_to_the_vision_model(client, captured, vision_calls):
    key = new_key(client)
    ask(client, key, data_url(b"blurry"))
    assert len(vision_calls) == 1
    assert "tx qq" not in captured[-1]["messages"][0]["content"]


def test_without_the_vision_model_the_ocr_text_is_still_used(client, captured, monkeypatch):
    async def no_models(self, max_age_s=10.0):
        return ["qwen2.5:3b"]

    from gyan_gateway.ollama import OllamaClient
    monkeypatch.setattr(OllamaClient, "installed_models", no_models)
    key = new_key(client)
    resp = ask(client, key, data_url(b"blurry"))
    assert resp.status_code == 200
    assert "tx qq" in captured[-1]["messages"][0]["content"]


def test_same_image_is_read_only_once(client, fake_tesseract):
    key = new_key(client)
    image = data_url(b"doc")
    ask(client, key, image)
    ask(client, key, image)  # a follow-up resends the whole conversation, image included
    ask(client, key, image, image)  # and the same picture twice in one request
    assert len(fake_tesseract) == 1


def test_works_for_the_research_model_too(client, fake_tesseract):
    key = new_key(client)
    resp = ask(client, key, data_url(b"doc"), model="gyan-research")
    assert resp.status_code == 200
    assert len(fake_tesseract) == 1


def test_remote_image_urls_are_not_fetched(client, fake_tesseract):
    key = new_key(client)
    resp = ask(client, key, "http://169.254.169.254/latest/meta-data")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "unsupported_image"
    assert fake_tesseract == []


def test_not_an_image_is_rejected(client, fake_tesseract):
    key = new_key(client)
    resp = ask(client, key, data_url(b"just text", header=b"<html>"))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_image"
    assert fake_tesseract == []


def test_bad_base64_is_rejected(client):
    key = new_key(client)
    resp = ask(client, key, "data:image/png;base64,@@@not base64@@@")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_image"


def test_too_many_images(settings, make_client):
    settings.max_images = 2
    with make_client(settings) as c:
        key = new_key(c)
        resp = ask(c, key, data_url(b"a"), data_url(b"b"), data_url(b"c"))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "too_many_images"


def test_image_too_large(settings, make_client):
    settings.max_image_bytes = 10_000
    with make_client(settings) as c:
        key = new_key(c)
        resp = ask(c, key, data_url(b"x" * 20_000))
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "image_too_large"


def test_images_cost_more_of_the_budget(client):
    key = new_key(client, rpm=10, daily=100)
    first = ask(client, key, data_url(b"doc"))  # 1 + 3 units
    assert first.headers["X-RateLimit-Remaining"] == "6"


def test_helper_model_is_hidden(client):
    key = new_key(client)
    ids = [m["id"] for m in client.get("/v1/models", headers=bearer(key)).json()["data"]]
    assert "qwen2.5vl:3b" not in ids and "qwen2.5:3b" in ids
    resp = client.post("/v1/chat/completions", headers=bearer(key), json={
        "model": "qwen2.5vl:3b", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 404


def test_large_text_only_body_still_rejected(client):
    key = new_key(client)
    body = {"messages": [{"role": "user", "content": "x" * 300_000}]}
    resp = client.post("/v1/chat/completions", json=body, headers=bearer(key))
    assert resp.status_code == 413


def test_parse_tsv_rebuilds_lines_and_confidence():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [
        header,
        "4\t1\t1\t1\t1\t0\t0\t0\t10\t10\t-1\t",
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t96\tINVOICE",
        "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t90\t#4821",
        "5\t1\t1\t1\t2\t1\t0\t0\t10\t10\t80\tTotal:",
        "5\t1\t1\t1\t2\t2\t0\t0\t10\t10\t90\tRs",
    ]
    text, confidence, words = parse_tsv("\n".join(rows))
    assert text == "INVOICE #4821\nTotal: Rs"
    assert words == 4 and confidence == pytest.approx(89.0)
    assert parse_tsv("") == ("", 0.0, 0)

"""Thin client for Ollama (model list, health, OpenAI-compatible chat)."""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .errors import ApiError, upstream_unavailable


class OllamaClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient, request_timeout_s: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout = httpx.Timeout(connect=5, read=request_timeout_s, write=30, pool=5)
        self._models: list[str] = []
        self._models_at = 0.0

    async def installed_models(self, max_age_s: float = 10.0) -> list[str]:
        if self._models and time.monotonic() - self._models_at < max_age_s:
            return self._models
        try:
            resp = await self.client.get(f"{self.base_url}/api/tags", timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError:
            raise upstream_unavailable() from None
        self._models = [m["name"] for m in resp.json().get("models", [])]
        self._models_at = time.monotonic()
        return self._models

    async def ping(self) -> bool:
        try:
            resp = await self.client.get(f"{self.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def stream_chat(self, body: dict) -> AsyncIterator[tuple[str, Any]]:
        """Run a streaming chat and yield simple events.

        ("token", text) for each piece of the answer, then ("usage", {"prompt": n,
        "completion": n}) when the server reports token counts.
        """
        response = await self.open_chat({**body, "stream": True})
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage")
                if usage:
                    yield "usage", {
                        "prompt": usage.get("prompt_tokens", 0),
                        "completion": usage.get("completion_tokens", 0),
                    }
                for choice in chunk.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content") or ""
                    if text:
                        yield "token", text
        finally:
            await response.aclose()

    async def open_chat(self, body: dict) -> httpx.Response:
        """Send a chat request and return the open response (caller must close it)."""
        request = self.client.build_request(
            "POST", f"{self.base_url}/v1/chat/completions", json=body, timeout=self.timeout
        )
        try:
            resp = await self.client.send(request, stream=True)
        except httpx.HTTPError:
            raise upstream_unavailable() from None
        if resp.status_code != 200:
            detail = (await resp.aread()).decode(errors="replace")[:300]
            await resp.aclose()
            if resp.status_code == 404:
                raise ApiError(404, "The model was not found.", code="model_not_found")
            raise ApiError(
                502, f"Model server error ({resp.status_code}): {detail}",
                type_="server_error", code="upstream_error",
            )
        return resp

    async def read_image(self, model: str, prompt: str, image_b64: str, max_tokens: int) -> str:
        """Ask a vision model about one image (Ollama's native API) and return its text."""
        body = {
            "model": model,
            "stream": False,
            "keep_alive": "5m",
            "options": {"num_predict": max_tokens, "temperature": 0},
            "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        }
        url = f"{self.base_url}/api/chat"
        try:
            resp = await self.client.post(url, json=body, timeout=self.timeout)
        except httpx.HTTPError:
            raise upstream_unavailable() from None
        if resp.status_code != 200:
            raise ApiError(
                502, "The image could not be read.", type_="server_error", code="upstream_error"
            )
        try:
            return str(resp.json()["message"]["content"])
        except (ValueError, KeyError, TypeError):
            raise ApiError(
                502, "Invalid response from the model server.",
                type_="server_error", code="upstream_error",
            ) from None

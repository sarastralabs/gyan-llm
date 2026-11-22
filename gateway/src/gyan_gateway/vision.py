"""Turns images in a chat request into text, so the small text model can answer about them.

OpenAI-style clients send images as `image_url` parts holding a `data:image/...;base64,...` URL.
Each image is read once (OCR model first, vision model for photos with little text), the result
is cached by the image's hash, and the image part is replaced by a text part. Remote image URLs
are refused: fetching arbitrary URLs for a caller would be a server-side request forgery hole.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

from redis.asyncio import Redis

from .config import Settings
from .errors import ApiError
from .limits import SlotPool
from .ollama import OllamaClient
from .schemas import ChatMessage, ChatRequest

_DATA_URL = re.compile(r"^data:image/[a-z0-9.+-]+;base64,(.*)$", re.I | re.S)
OCR_PROMPT = "Text Recognition:"
VISION_PROMPT = (
    "Describe this image in detail. Then transcribe any text that is visible in it, exactly as "
    "written. Do not guess at things you cannot see."
)


def _base(name: str) -> str:
    return name.removesuffix(":latest")


def _image_url(part: dict) -> str | None:
    value = part.get("image_url")
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) else None


def _is_image(part: object) -> bool:
    return isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}


def count_images(req: ChatRequest) -> int:
    return sum(
        1
        for m in req.messages
        if isinstance(m.content, list)
        for part in m.content
        if _is_image(part)
    )


def sniff_format(data: bytes) -> str | None:
    """The real image type from the file's first bytes (the declared type is not trusted)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@dataclass
class _Image:
    data: bytes
    sha: str


class VisionService:
    def __init__(
        self, ollama: OllamaClient, redis: Redis, slots: SlotPool, settings: Settings
    ) -> None:
        self.ollama, self.redis, self.slots, self.settings = ollama, redis, slots, settings

    def _too_large(self) -> ApiError:
        mb = self.settings.max_image_bytes / 1_000_000
        return ApiError(413, f"Image too large (max {mb:g} MB).", code="image_too_large")

    def _decode(self, part: dict) -> _Image:
        url = _image_url(part)
        match = _DATA_URL.match(url or "")
        if not match:
            raise ApiError(
                400,
                "Images must be sent inline as base64 data URLs (data:image/png;base64,...). "
                "Remote image URLs are not fetched.",
                code="unsupported_image",
            )
        b64 = re.sub(r"\s+", "", match.group(1))
        if len(b64) * 3 // 4 > self.settings.max_image_bytes + 3:
            raise self._too_large()
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "The image is not valid base64.", code="invalid_image") from None
        if len(data) > self.settings.max_image_bytes:
            raise self._too_large()
        if sniff_format(data) is None:
            raise ApiError(
                400, "Unsupported image. Use PNG, JPEG, WebP or GIF.", code="invalid_image"
            )
        return _Image(data, hashlib.sha256(data).hexdigest())

    async def resolve(self, req: ChatRequest) -> ChatRequest:
        """The same request with every image replaced by the text read from it."""
        total = count_images(req)
        if total == 0:
            return req
        if total > self.settings.max_images:
            raise ApiError(
                400, f"Too many images (max {self.settings.max_images}).", code="too_many_images"
            )
        # Check every image first so a bad one fails fast, before any slow reading.
        decoded = [
            self._decode(part)
            for m in req.messages
            if isinstance(m.content, list)
            for part in m.content
            if _is_image(part)
        ]
        installed = {_base(name) for name in await self.ollama.installed_models()}
        if _base(self.settings.ocr_model) not in installed:
            raise ApiError(
                503, "Image reading is not set up on this server.",
                type_="server_error", code="vision_unavailable",
            )

        texts: dict[str, str] = {}
        messages: list[ChatMessage] = []
        number = 0
        for message in req.messages:
            if not isinstance(message.content, list):
                messages.append(message)
                continue
            parts: list[dict] = []
            for part in message.content:
                if not _is_image(part):
                    parts.append(part)
                    continue
                image = decoded[number]
                number += 1
                if image.sha not in texts:
                    texts[image.sha] = await self._read(image, installed)
                label = f"[Attached image {number}]\n{texts[image.sha]}"
                parts.append({"type": "text", "text": label})
            messages.append(message.model_copy(update={"content": parts}))
        return req.model_copy(update={"messages": messages})

    async def _read(self, image: _Image, installed: set[str]) -> str:
        key = f"vision:{image.sha}"
        cached = await self.redis.get(key)
        if cached:
            return str(cached)
        s = self.settings
        b64 = base64.b64encode(image.data).decode()
        lease = await self.slots.acquire()  # the model server handles one thing at a time
        try:
            text = (
                await self.ollama.read_image(s.ocr_model, OCR_PROMPT, b64, s.ocr_max_tokens)
            ).strip()
            if len(text) >= s.ocr_min_chars:
                result = f"(text read from the image)\n{text}"
            elif _base(s.vision_model) in installed:
                described = (
                    await self.ollama.read_image(
                        s.vision_model, VISION_PROMPT, b64, s.vision_max_tokens
                    )
                ).strip()
                result = f"(description of the photo, which has little text)\n{described}"
            else:
                result = text or "(no readable text found in the image)"
        finally:
            await lease.release()
        result = result[: s.image_text_max_chars]
        await self.redis.set(key, result, ex=s.image_cache_ttl_s)
        return result

"""Request validation for the OpenAI-compatible endpoints.

Only a whitelist of fields is forwarded to the model server, so clients cannot set server-side
options (keep-alive, context size, and so on). Unknown fields are ignored, as OpenAI clients
often send extras such as `user` or `logprobs`.
"""
from __future__ import annotations

import json
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ApiError


class TextPart(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["text"]
    text: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["system", "user", "assistant"]
    content: str | list[dict[str, Any]]

    def flat_text(self) -> str:
        """Plain text of the message. Non-text parts (images, audio) are not supported."""
        if isinstance(self.content, str):
            return self.content
        pieces: list[str] = []
        for part in self.content:
            if part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise ApiError(
                    400,
                    "Only text content is supported by this model.",
                    code="unsupported_content",
                )
            pieces.append(part["text"])
        return "\n".join(pieces)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    include_usage: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    max_tokens: int | None = Field(None, ge=1, alias="max_completion_tokens")
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = Field(None, ge=-2, le=2)
    frequency_penalty: float | None = Field(None, ge=-2, le=2)
    response_format: dict[str, Any] | None = None
    n: int = 1

    @field_validator("n")
    @classmethod
    def _one_choice(cls, v: int) -> int:
        if v != 1:
            raise ValueError("only n=1 is supported")
        return v

    @field_validator("stop")
    @classmethod
    def _stop_ok(cls, v: str | list[str] | None) -> str | list[str] | None:
        if isinstance(v, list) and len(v) > 4:
            raise ValueError("at most 4 stop sequences")
        return v

    @field_validator("response_format")
    @classmethod
    def _format_ok(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None and v.get("type") not in {"text", "json_object"}:
            raise ValueError("response_format.type must be 'text' or 'json_object'")
        return v


T = TypeVar("T", bound=BaseModel)


def parse_body(raw: bytes, max_bytes: int, model_cls: type[T]) -> T:
    """Parse and validate a JSON request body, with OpenAI-style 4xx errors."""
    if len(raw) > max_bytes:
        raise ApiError(413, "Request body too large.", code="body_too_large")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "Request body must be valid JSON.", code="invalid_json") from None
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise ApiError(
            400, f"Invalid request: {where}: {first['msg']}", code="invalid_request"
        ) from None


def build_upstream_body(
    req: ChatRequest, model: str, max_tokens_cap: int, max_total_chars: int, max_messages: int
) -> dict[str, Any]:
    """Validate sizes and build the body sent to the model server."""
    if len(req.messages) > max_messages:
        raise ApiError(400, f"Too many messages (max {max_messages}).", code="too_many_messages")

    messages = [{"role": m.role, "content": m.flat_text()} for m in req.messages]
    if sum(len(m["content"]) for m in messages) > max_total_chars:
        raise ApiError(400, "The conversation is too long.", code="context_length_exceeded")

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": req.stream,
        "max_tokens": min(req.max_tokens or max_tokens_cap, max_tokens_cap),
    }
    # Ask for token counts on streams so usage can be recorded; they are stripped from the
    # client's stream again unless the client asked for them.
    if req.stream:
        body["stream_options"] = {"include_usage": True}
    for field in (
        "temperature", "top_p", "stop", "seed", "presence_penalty", "frequency_penalty",
        "response_format",
    ):
        value = getattr(req, field)
        if value is not None:
            body[field] = value
    return body

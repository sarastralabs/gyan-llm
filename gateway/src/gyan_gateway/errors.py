"""OpenAI-style error responses, so standard SDKs show useful messages."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        type_: str = "invalid_request_error",
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_
        self.code = code
        self.headers = headers or {}


def error_body(message: str, type_: str, code: str | None) -> dict:
    return {"error": {"message": message, "type": type_, "param": None, "code": code}}


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        error_body(exc.message, exc.type, exc.code), status_code=exc.status, headers=exc.headers
    )


# Handy constructors ---------------------------------------------------------------------


def unauthorized(message: str = "Invalid or missing API key.") -> ApiError:
    return ApiError(
        401, message, type_="authentication_error", code="invalid_api_key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def rate_limited(retry_after: int, limit: int, scope: str) -> ApiError:
    return ApiError(
        429,
        f"Rate limit reached ({scope}). Retry in {retry_after}s.",
        type_="rate_limit_error",
        code="rate_limit_exceeded",
        headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(limit)},
    )


def server_busy(retry_after: int = 10) -> ApiError:
    return ApiError(
        503,
        "The model is busy with other requests. Please retry shortly.",
        type_="server_error",
        code="server_busy",
        headers={"Retry-After": str(retry_after)},
    )


def upstream_unavailable() -> ApiError:
    return ApiError(
        503, "The model server is unavailable.", type_="server_error", code="upstream_unavailable"
    )

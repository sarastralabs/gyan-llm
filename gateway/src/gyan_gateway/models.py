"""Which models clients may see and use."""
from __future__ import annotations

from .deps import Services
from .errors import ApiError


def _base(name: str) -> str:
    return name.removesuffix(":latest")


async def visible_models(svc: Services) -> list[str]:
    """Installed Ollama models, limited by ALLOWED_MODELS when it is set.

    The image-reading models are internal helpers, not chat models, so they are hidden.
    """
    installed = await svc.ollama.installed_models()
    allowed = svc.settings.allowed_model_list
    hidden = {_base(svc.settings.vision_model)}
    return [m for m in installed if _base(m) not in hidden and (not allowed or m in allowed)]


async def public_models(svc: Services) -> list[str]:
    """Models shown in /v1/models: the real ones plus the research model."""
    return [*await visible_models(svc), svc.settings.research_model_name]


async def resolve_model(svc: Services, requested: str | None) -> str:
    """The real model to run, or a 404 in OpenAI's format."""
    model = requested or svc.settings.default_model
    if model not in await visible_models(svc):
        raise ApiError(404, f"The model '{model}' does not exist.", code="model_not_found")
    return model

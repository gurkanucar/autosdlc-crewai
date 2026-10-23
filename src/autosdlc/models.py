"""OpenCode Go models: the chat/completions endpoint this application uses."""

from __future__ import annotations

import httpx

from .crewai_engine import DEFAULT_MODEL


MODELS_URL = "https://opencode.ai/inference/go/v1/models"
# OpenCode Go'nun chat/completions endpoint'inde belgelenen model kimlikleri.
CHAT_MODELS = (
    "glm-5.3-flash", "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.7-code",
    "longcat-2.0", "deepseek-v4.1-flash", "deepseek-v4-pro",
    "mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.5", "mimo-v2.5-pro",
    "minimax-m3.1-flash", "hy4-preview", "hy3",
    "longcat-2.5-preview-free", "step-5-preview", "beam-free",
)


def available_models() -> list[str]:
    """Intersect with the live catalog; if it is unreachable show the documented models."""
    try:
        response = httpx.get(MODELS_URL, timeout=8)
        response.raise_for_status()
        available = {item["id"] for item in response.json()["data"] if isinstance(item.get("id"), str)}
        result = [model for model in CHAT_MODELS if model in available]
        return result or list(CHAT_MODELS)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return list(CHAT_MODELS)


def default_model(models: list[str]) -> str:
    return DEFAULT_MODEL if DEFAULT_MODEL in models else models[0]

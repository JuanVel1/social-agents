from __future__ import annotations

from ..config import Settings
from .base import LLMProvider, Tier


def get_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(settings)
    if settings.llm_provider == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    raise ValueError(f"LLM_PROVIDER desconocido: {settings.llm_provider!r} (usa 'gemini' o 'fake')")


__all__ = ["LLMProvider", "Tier", "get_provider"]

from __future__ import annotations

from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# quality: parrilla y copy final. bulk: hashtags y variantes.
Tier = Literal["quality", "bulk"]


class LLMProvider(Protocol):
    def generate(self, prompt: str, schema: type[T], *, tier: Tier = "quality", system: str = "") -> T:
        """Devuelve la respuesta del modelo ya validada contra `schema`."""
        ...

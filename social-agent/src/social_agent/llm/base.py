from __future__ import annotations

from typing import Literal, Protocol, Sequence, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# quality: parrilla y copy final. bulk: hashtags y variantes.
Tier = Literal["quality", "bulk"]
# Archivo que acompaña al prompt: (contenido, tipo MIME), p. ej. (pdf, "application/pdf").
Attachment = tuple[bytes, str]


class NoQuotaError(RuntimeError):
    """El modelo no tiene cuota con esta clave (p. ej. no está en la capa gratuita): reintentar no sirve."""


class LLMProvider(Protocol):
    def generate(self, prompt: str, schema: type[T], *, tier: Tier = "quality", system: str = "",
                 attachments: Sequence[Attachment] = ()) -> T:
        """Devuelve la respuesta del modelo ya validada contra `schema`."""
        ...

    def generate_image(self, prompt: str, references: Sequence[Attachment] = (), aspect: str = "3:4") -> bytes:
        """Devuelve una imagen nueva (bytes PNG/JPEG) a partir del prompt y las imágenes de referencia."""
        ...

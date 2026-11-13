from __future__ import annotations

import io
import logging
import time
from collections import deque
from typing import Sequence

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from .. import progress
from ..config import Settings
from .base import Attachment, NoQuotaError, T, Tier

log = logging.getLogger(__name__)

INLINE_LIMIT = 15 * 1024 * 1024


class RateLimiter:
    """Ventana deslizante de 60 s: no más de `max_per_minute` llamadas."""

    def __init__(self, max_per_minute: int, clock=time.monotonic, sleep=time.sleep):
        self.max = max(1, max_per_minute)
        self.calls: deque[float] = deque()
        self.clock = clock
        self.sleep = sleep

    def wait(self) -> None:
        now = self.clock()
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        if len(self.calls) >= self.max:
            delay = 60 - (now - self.calls[0])
            log.info("Límite de %s solicitudes/min: esperando %.1f s", self.max, delay)
            with progress.waiting(f"Límite de {self.max} solicitudes por minuto", delay):
                self.sleep(delay)
            self.calls.popleft()
        self.calls.append(self.clock())


RETRYABLE = (429, 500, 503)


class GeminiProvider:
    def __init__(self, settings: Settings, client=None, sleep=time.sleep, backup=None):
        if not settings.gemini_api_key:
            raise RuntimeError("Falta GEMINI_API_KEY en .env (créala en aistudio.google.com)")
        # Sin reintentos internos del SDK: los maneja _call para poder cambiar de modelo y de clave.
        options = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
        self.client = client or genai.Client(api_key=settings.gemini_api_key, http_options=options)
        # Clave de respaldo (GEMINI_API_KEY_RESPALD): se usa en cuanto la principal falla con un modelo.
        if backup is None and client is None and settings.gemini_api_key_backup:
            backup = genai.Client(api_key=settings.gemini_api_key_backup, http_options=options)
        self.keys: list[tuple[str, object, RateLimiter]] = [
            ("principal", self.client, RateLimiter(settings.gemini_max_rpm))]
        if backup is not None:
            self.keys.append(("respaldo", backup, RateLimiter(settings.gemini_max_rpm)))
        self.models = {"quality": settings.gemini_model_quality, "bulk": settings.gemini_model_bulk}
        self.fallbacks = settings.gemini_fallback_models
        self.image_model = settings.gemini_image_model
        self.limiter = self.keys[0][2]
        self.sleep = sleep

    def generate(self, prompt: str, schema: type[T], *, tier: Tier = "quality", system: str = "",
                 attachments: Sequence[Attachment] = ()) -> T:
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_schema=schema,
            # Con documentos adjuntos se extrae, no se crea: sin variación.
            temperature=0.1 if attachments else 0.8,
        )
        contents = [*(self._part(data, mime) for data, mime in attachments), prompt] if attachments else prompt
        last_error: Exception | None = None
        for attempt in range(2):
            response = self._call(self.models[tier], contents, config)
            try:
                if isinstance(response.parsed, schema):
                    return response.parsed
                return schema.model_validate_json(response.text or "")
            except ValidationError as exc:
                last_error = exc
                log.warning("Respuesta no válida para %s (intento %s): %s", schema.__name__, attempt + 1, exc)
        raise RuntimeError(f"Gemini no devolvió un {schema.__name__} válido") from last_error

    def _part(self, data: bytes, mime: str):
        """Archivos pequeños van en la petición; los grandes por la File API (límite de ~20 MB por petición)."""
        if len(data) <= INLINE_LIMIT:
            return types.Part.from_bytes(data=data, mime_type=mime)
        return self.client.files.upload(file=io.BytesIO(data), config=types.UploadFileConfig(mime_type=mime))

    def generate_image(self, prompt: str, references: Sequence[Attachment] = (), aspect: str = "3:4") -> bytes:
        """Imagen nueva a partir del prompt y de imágenes de referencia (p. ej. fotos de la persona)."""
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect),
        )
        contents = [*(self._part(data, mime) for data, mime in references), prompt]
        # Sin modelos de respaldo: los de texto no generan imágenes.
        response = self._call(self.image_model, contents, config, fallbacks=False)
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        raise RuntimeError(f"{self.image_model} no devolvió imagen (posible bloqueo de seguridad)")

    def _call(self, model: str, contents, config: types.GenerateContentConfig, fallbacks: bool = True):
        """Por cada modelo, primero la clave principal y, si falla (429 sin cuota, 503 saturado, corte de
        red), la de respaldo al instante; solo se espera cuando ninguna clave respondió. Dos pasadas por
        modelo antes de pasar al siguiente."""
        models = [model] + ([m for m in self.fallbacks if m != model] if fallbacks else [])
        keys = self.keys
        if any(isinstance(part, types.File) for part in (contents if isinstance(contents, list) else [])):
            keys = keys[:1]  # los archivos subidos por la File API solo existen para la clave principal
        candidates = {(label, name) for name in models for label, _, _ in keys}
        last: Exception | None = None
        no_quota: set[tuple[str, str]] = set()
        for rnd in range(3):
            for name in models:
                for attempt in range(2):
                    tried = False
                    for label, client, limiter in keys:
                        if (label, name) in no_quota:
                            continue
                        tried = True
                        who = name if label == "principal" else f"{name} (clave de respaldo)"
                        limiter.wait()
                        progress.llm_call(who)
                        try:
                            response = client.models.generate_content(model=name, contents=contents, config=config)
                            if name != model:
                                log.info("Respondió el modelo de respaldo %s", who)
                            elif label != "principal":
                                log.info("Respondió %s", who)
                            return response
                        except (errors.APIError, httpx.TransportError) as exc:
                            # Cortes de red (conexión anulada, DNS) se reintentan igual que un 503.
                            code = getattr(exc, "code", type(exc).__name__)
                            if isinstance(exc, errors.APIError) and code not in RETRYABLE:
                                raise
                            last = exc
                            if code == 429 and "limit: 0" in str(exc):
                                # Cuota cero: el modelo no está en la capa gratuita; esperar no sirve.
                                no_quota.add((label, name))
                                log.warning("%s no tiene cuota con la clave %s", name, label)
                                continue
                            log.warning("%s respondió %s (intento %s)", who, code, attempt + 1)
                    if not tried:
                        break
                    if attempt == 0:
                        with progress.waiting(f"{name} no respondió: reintento", 10):
                            self.sleep(10)
            if no_quota >= candidates:
                raise NoQuotaError(f"{', '.join(models)} sin cuota con esta clave: activa la facturación "
                                   "del proyecto en aistudio.google.com") from last
            if rnd < 2:
                wait = 60 * (rnd + 1)
                log.warning("Todos los modelos ocupados; nueva ronda en %s s", wait)
                with progress.waiting("Todos los modelos ocupados: nueva ronda", wait):
                    self.sleep(wait)
        raise last

    def list_models(self) -> list[str]:
        return [m.name for m in self.client.models.list()]

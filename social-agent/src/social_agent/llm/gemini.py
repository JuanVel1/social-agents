from __future__ import annotations

import logging
import time
from collections import deque

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from ..config import Settings
from .base import T, Tier

log = logging.getLogger(__name__)


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
            self.sleep(delay)
            self.calls.popleft()
        self.calls.append(self.clock())


RETRYABLE = (429, 500, 503)


class GeminiProvider:
    def __init__(self, settings: Settings, client=None, sleep=time.sleep):
        if not settings.gemini_api_key:
            raise RuntimeError("Falta GEMINI_API_KEY en .env (créala en aistudio.google.com)")
        # Sin reintentos internos del SDK: los maneja _call para poder cambiar de modelo.
        self.client = client or genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
        )
        self.models = {"quality": settings.gemini_model_quality, "bulk": settings.gemini_model_bulk}
        self.fallbacks = settings.gemini_fallback_models
        self.limiter = RateLimiter(settings.gemini_max_rpm)
        self.sleep = sleep

    def generate(self, prompt: str, schema: type[T], *, tier: Tier = "quality", system: str = "") -> T:
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.8,
        )
        last_error: Exception | None = None
        for attempt in range(2):
            response = self._call(self.models[tier], prompt, config)
            try:
                if isinstance(response.parsed, schema):
                    return response.parsed
                return schema.model_validate_json(response.text or "")
            except ValidationError as exc:
                last_error = exc
                log.warning("Respuesta no válida para %s (intento %s): %s", schema.__name__, attempt + 1, exc)
        raise RuntimeError(f"Gemini no devolvió un {schema.__name__} válido") from last_error

    def _call(self, model: str, prompt: str, config: types.GenerateContentConfig):
        """Dos intentos por modelo; si sigue saturado (503) o sin cuota (429), pasa al siguiente."""
        candidates = [model] + [m for m in self.fallbacks if m != model]
        last: errors.APIError | None = None
        for rnd in range(3):
            for name in candidates:
                for attempt in range(2):
                    self.limiter.wait()
                    try:
                        response = self.client.models.generate_content(model=name, contents=prompt, config=config)
                        if name != model:
                            log.info("Respondió el modelo de respaldo %s", name)
                        return response
                    except errors.APIError as exc:
                        if exc.code not in RETRYABLE:
                            raise
                        last = exc
                        log.warning("%s respondió %s (intento %s)", name, exc.code, attempt + 1)
                        if attempt == 0:
                            self.sleep(10)
            if rnd < 2:
                wait = 60 * (rnd + 1)
                log.warning("Todos los modelos ocupados; nueva ronda en %s s", wait)
                self.sleep(wait)
        raise last

    def list_models(self) -> list[str]:
        return [m.name for m in self.client.models.list()]

"""Publicación con la API pública de Postiz (docs.postiz.com/public-api)."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .approval import MANUAL_TYPES
from .models import Brand, Piece

# Hora de publicación por tipo (hora local del cliente).
PUBLISH_TIME = {"post": time(18, 0), "carousel": time(12, 0)}


class PostizClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise RuntimeError("Falta POSTIZ_API_KEY en .env (Postiz > Settings > Public API)")
        self.base_url = base_url
        self.http = client or httpx.Client(
            base_url=f"{base_url}/public/v1", headers={"Authorization": api_key}, timeout=60
        )

    def integrations(self) -> list[dict]:
        response = self.http.get("/integrations")
        response.raise_for_status()
        return response.json()

    def upload(self, path: Path) -> dict:
        with path.open("rb") as fh:
            response = self.http.post("/upload", files={"file": (path.name, fh, "image/png")})
        response.raise_for_status()
        media = response.json()
        return {"id": media["id"], "path": media["path"]}

    def public_url(self, media: dict) -> str:
        path = media["path"]
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def schedule(self, brand: Brand, piece: Piece, tz: str, when: datetime | None = None) -> list[dict]:
        """Programa la pieza en todos los canales del cliente (solo Facebook, solo Instagram o ambos)."""
        if piece.type in MANUAL_TYPES:
            raise ValueError(f"{piece.id}: las piezas de tipo {piece.type} se publican a mano")
        if not brand.postiz_integrations:
            raise RuntimeError(f"El cliente {brand.slug} no tiene postiz_integrations en brand.yaml")
        when = when or datetime.combine(piece.date, PUBLISH_TIME[piece.type], ZoneInfo(tz))
        posts = []
        for network, integration_id in brand.postiz_integrations.items():
            settings: dict = {"__type": network}
            if network == "instagram":
                settings["post_type"] = "post"
            posts.append({
                "integration": {"id": integration_id},
                "value": [{"content": piece.caption_full, "image": piece.postiz_media}],
                "settings": settings,
            })
        response = self.http.post("/posts", json={
            "type": "schedule",
            "date": when.isoformat(),
            "shortLink": False,
            "tags": [],
            "posts": posts,
        })
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, list) else [result]


def soon(tz: str, minutes: int = 10) -> datetime:
    """Para pruebas: unos minutos desde ahora en vez de la fecha del calendario."""
    return datetime.now(ZoneInfo(tz)).replace(second=0, microsecond=0) + timedelta(minutes=minutes)


def describe(result: list[dict]) -> str:
    return json.dumps(result, ensure_ascii=False)[:1900]

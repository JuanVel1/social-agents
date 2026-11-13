"""Publicación con la API pública de Postiz (docs.postiz.com/public-api)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .approval import MANUAL_TYPES
from .models import Brand, Piece

# Hora de publicación por tipo (hora local del cliente).
PUBLISH_TIME = {"post": time(18, 0), "carousel": time(12, 0)}


log = logging.getLogger(__name__)

# Límites de Pinterest (y de la validación de Postiz): título, descripción e imágenes por pin.
PINTEREST_TITLE, PINTEREST_DESCRIPTION, PINTEREST_IMAGES = 100, 500, 5


def _cut(text: str, limit: int) -> str:
    """Recorta en la última palabra completa que cabe, con … al final."""
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" .,;:") + "…"


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

    def boards(self, integration_id: str) -> list[dict]:
        """Tableros de un canal de Pinterest conectado en Postiz: [{"id", "name"}]."""
        response = self.http.post(f"/integration-trigger/{integration_id}", json={"methodName": "boards", "data": {}})
        response.raise_for_status()
        return response.json().get("output") or []

    def schedule(self, brand: Brand, piece: Piece, tz: str, when: datetime | None = None) -> list[dict]:
        """Programa la pieza en todos los canales del cliente (Facebook, Instagram y Pinterest)."""
        if piece.type in MANUAL_TYPES:
            raise ValueError(f"{piece.id}: las piezas de tipo {piece.type} se publican a mano")
        if not brand.postiz_integrations:
            raise RuntimeError(f"El cliente {brand.slug} no tiene postiz_integrations en brand.yaml")
        when = when or datetime.combine(piece.date, PUBLISH_TIME[piece.type], ZoneInfo(tz))
        posts = []
        for network, integration_id in brand.postiz_integrations.items():
            settings: dict = {"__type": network}
            content, media = piece.caption_full, piece.postiz_media
            if network.startswith("instagram"):  # instagram o instagram-standalone
                settings["post_type"] = "post"
            if network == "pinterest":
                if not brand.pinterest_board:
                    log.warning("%s tiene Pinterest sin pinterest_board en brand.yaml: %s no va a Pinterest",
                                brand.slug, piece.id)
                    continue
                title = piece.content.headline if piece.content else piece.topic
                settings.update(board=brand.pinterest_board, title=_cut(title, PINTEREST_TITLE))
                if brand.website:
                    settings["link"] = brand.website
                content, media = _cut(content, PINTEREST_DESCRIPTION), media[:PINTEREST_IMAGES]
            posts.append({
                "integration": {"id": integration_id},
                "value": [{"content": content, "image": media}],
                "settings": settings,
            })
        if not posts:
            raise RuntimeError(f"{piece.id}: ningún canal de {brand.slug} puede recibir la pieza")
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

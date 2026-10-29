"""Tablero de aprobación en Notion (API REST, versión 2022-06-28).

La base de datos debe tener estas propiedades (ver README):
Pieza (título), ID (texto), Cliente (selección), Tipo (selección), Fecha (fecha),
Pilar (selección), Copy (texto), Estado (selección: Pendiente / Aprobado / Cambios /
Programado), Comentarios (texto), Imágenes (archivos), Postiz (texto).
"""
from __future__ import annotations

import re

import httpx

from .models import Piece

API = "https://api.notion.com/v1"
TYPE_LABELS = {"post": "Post", "carousel": "Carrusel", "story": "Historia", "reel_script": "Guion de Reel"}
# Se aprueban en Notion pero las publica una persona: las historias llevan un sticker que la API
# de Instagram no permite añadir, y los guiones se graban.
MANUAL_TYPES = ("story", "reel_script")


def _text(value: str) -> list[dict]:
    # Notion limita cada bloque de texto a 2000 caracteres.
    return [{"type": "text", "text": {"content": value[i:i + 2000]}} for i in range(0, len(value), 2000)]


def _paragraph(value: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _text(value)}}


def database_id_from(value: str) -> str:
    """Acepta el ID o el enlace completo de la base de datos (con o sin guiones)."""
    uuid = r"(?<![0-9a-f])[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}(?![0-9a-f])"
    found = re.findall(uuid, value.split("?")[0])
    return found[-1].replace("-", "") if found else value


class NotionBoard:
    def __init__(self, token: str, database_id: str, client: httpx.Client | None = None):
        if not token or not database_id:
            raise RuntimeError("Faltan NOTION_TOKEN o NOTION_DATABASE_ID en .env")
        self.database_id = database_id_from(database_id)
        self.http = client or httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=30,
        )

    def add_piece(self, client_slug: str, month: str, piece: Piece) -> str:
        c = piece.content
        children: list[dict] = [
            {"object": "block", "type": "image", "image": {"type": "external", "external": {"url": url}}}
            for url in piece.image_urls
        ]
        if piece.type == "carousel" and c:
            children += [_paragraph(f"{i}. {s.heading} — {s.body}") for i, s in enumerate(c.slides, 1)]
        if piece.type == "story" and c:
            children.append(_paragraph(
                "Publicación manual: cuando esté aprobada, sube esta imagen como historia desde la app "
                f"de Instagram, añade el sticker y cambia el Estado a Programado. Sticker: {c.sticker or '—'}"))
        if piece.type == "reel_script" and c:
            children.append(_paragraph(c.script))

        properties = {
            "Pieza": {"title": _text(c.headline if c else piece.topic)},
            "ID": {"rich_text": _text(f"{client_slug}/{month}/{piece.id}")},
            "Cliente": {"select": {"name": client_slug}},
            "Tipo": {"select": {"name": TYPE_LABELS[piece.type]}},
            "Fecha": {"date": {"start": piece.date.isoformat()}},
            "Pilar": {"select": {"name": piece.pillar}},
            "Copy": {"rich_text": _text(piece.caption_full)},
            "Estado": {"select": {"name": "Pendiente"}},
        }
        if piece.image_urls:
            properties["Imágenes"] = {"files": [
                {"name": url.rsplit("/", 1)[-1][:100], "type": "external", "external": {"url": url}}
                for url in piece.image_urls
            ]}
        response = self.http.post("/pages", json={
            "parent": {"database_id": self.database_id},
            "properties": properties,
            "children": children[:100],
        })
        response.raise_for_status()
        return response.json()["id"]

    def approved(self) -> list[dict]:
        """Piezas aprobadas que el agente publica solo y que aún no están programadas."""
        body = {"filter": {"and": [
            {"property": "Estado", "select": {"equals": "Aprobado"}},
            *({"property": "Tipo", "select": {"does_not_equal": TYPE_LABELS[t]}} for t in MANUAL_TYPES),
        ]}}
        results, cursor = [], None
        while True:
            if cursor:
                body["start_cursor"] = cursor
            response = self.http.post(f"/databases/{self.database_id}/query", json=body)
            response.raise_for_status()
            data = response.json()
            for page in data["results"]:
                ids = page["properties"]["ID"]["rich_text"]
                results.append({"page_id": page["id"], "key": "".join(t["plain_text"] for t in ids)})
            if not data.get("has_more"):
                return results
            cursor = data["next_cursor"]

    def mark_scheduled(self, page_id: str, postiz_ref: str) -> None:
        response = self.http.patch(f"/pages/{page_id}", json={"properties": {
            "Estado": {"select": {"name": "Programado"}},
            "Postiz": {"rich_text": _text(postiz_ref)},
        }})
        response.raise_for_status()

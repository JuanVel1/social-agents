"""Modelos de datos. Los que terminan en el esquema del LLM usan solo str/listas para
que Gemini los acepte como response_schema."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PieceType = Literal["post", "carousel", "story", "reel_script"]


class Pillar(BaseModel):
    name: str
    goal: str


class Brand(BaseModel):
    slug: str
    name: str
    sector: str
    audience: str
    tone: str
    pillars: list[Pillar]
    # Datos reales del negocio (productos, precios, historia, equipo, horarios). Es lo único
    # concreto que el modelo puede afirmar; lo demás sale como [DATO POR CONFIRMAR].
    facts: list[str] = Field(default_factory=list)
    colors: dict[str, str]
    fonts: dict[str, str]
    logo: str | None = None
    handle: str | None = None
    plan: Literal["plan1", "plan2"] = "plan1"
    # Canales de Postiz del cliente: {"instagram": "<integration id>", "facebook": "<id>"}
    postiz_integrations: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, clients_dir: Path, slug: str) -> Brand:
        path = clients_dir / slug / "brand.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(slug=slug, **data)


class Slot(BaseModel):
    id: str
    type: PieceType
    date: date


# --- Esquemas de respuesta del LLM ---

class TopicAssignment(BaseModel):
    slot_id: str
    pillar: str
    topic: str
    angle: str


class MonthOutline(BaseModel):
    assignments: list[TopicAssignment]


class Slide(BaseModel):
    heading: str
    body: str


class PieceContent(BaseModel):
    slot_id: str
    headline: str
    slides: list[Slide]
    caption: str
    cta: str
    sticker: str = ""
    script: str = ""


class WeekContent(BaseModel):
    pieces: list[PieceContent]


class HashtagSet(BaseModel):
    slot_id: str
    hashtags: list[str]


class HashtagBatch(BaseModel):
    items: list[HashtagSet]


# --- Pieza final ---

class Piece(BaseModel):
    id: str
    type: PieceType
    date: date
    pillar: str
    topic: str
    angle: str
    content: PieceContent | None = None
    hashtags: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    postiz_media: list[dict] = Field(default_factory=list)

    @property
    def caption_full(self) -> str:
        if not self.content:
            return ""
        tags = " ".join(f"#{h.lstrip('#')}" for h in self.hashtags)
        return f"{self.content.caption}\n\n{self.content.cta}\n\n{tags}".strip()


class MonthPlan(BaseModel):
    client: str
    month: str
    pieces: list[Piece]

    def count(self, piece_type: PieceType) -> int:
        return sum(1 for p in self.pieces if p.type == piece_type)

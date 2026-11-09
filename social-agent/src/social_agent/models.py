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


class Style(BaseModel):
    """Estilo visual. Vacío = plantillas por defecto (templates/*.html.j2)."""
    # Carpeta en templates/estilos/ con plantillas que reemplazan a las de por defecto.
    name: str = ""
    # Pilares que van sobre fondo claro (papel); el resto, sobre fondo oscuro (pizarra).
    light_pillars: list[str] = Field(default_factory=lambda: ["Educativo"])
    # Usar el banco de fotos de clients/<slug>/fotos/ (se arma con `social-agent fotos`).
    photos: bool = False
    # Generar con IA una foto nueva de la persona para cada pieza, a partir del banco (requiere photos).
    generate_photos: bool = False
    # Usar las poses del personaje 3D (fotos/personaje/, `social-agent personaje`) en vez de las fotos
    # reales. Con esto, generate_photos no se aplica: las poses ya son imágenes generadas.
    character: bool = False
    # Foto real de la que sale el personaje (relativa a fotos/): `serve` crea las variantes del maestro.
    character_photo: str = ""


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
    # Del formulario de onboarding: objetivos en redes (Leads, Ventas...) y competidores o referentes.
    objectives: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    colors: dict[str, str]
    # heading y body; los estilos pueden usar además display (titulares) y hand (manuscrita).
    fonts: dict[str, str]
    style: Style = Field(default_factory=Style)
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
    # Una o dos palabras del headline que el diseño resalta con el color de la marca.
    highlight: str = ""
    # La foto de la persona de la marca que acompaña la pieza (pose, gesto, ropa, objeto); se genera con IA.
    visual: str = ""
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


class ColorSpec(BaseModel):
    """Un color tal como lo da el manual; el agente calcula el hex si solo viene en RGB o CMYK."""
    role: str = ""  # primary, secondary, background, text, accent o vacío
    name: str = ""
    hex: str = ""
    rgb: str = ""  # "0, 84, 164"
    cmyk: str = ""  # "100, 49, 0, 36" en porcentaje
    pantone: str = ""


class PhotoReview(BaseModel):
    """Revisión de un recorte del banco de fotos (la hace el modelo mirando la imagen)."""
    id: str
    usable: bool = False
    reason: str = ""  # por qué no sirve: fondo pegado, texto, otra persona, borroso...
    mirrored_text: bool = False  # texto al revés: foto de cámara frontal
    description: str = ""  # gesto, ropa, escena, objetos
    framing: str = ""  # cara, medio cuerpo o cuerpo entero


class PhotoReviewBatch(BaseModel):
    reviews: list[PhotoReview]


class PhotoChoice(BaseModel):
    slot_id: str
    photo: str = ""


class PhotoChoices(BaseModel):
    choices: list[PhotoChoice]


class Visual(BaseModel):
    slot_id: str
    visual: str


class VisualBatch(BaseModel):
    items: list[Visual]


class BrandIntake(BaseModel):
    """Lo que el modelo extrae de un manual de marca en PDF. Vacío = el manual no lo dice."""
    sector: str = ""
    networks: list[str] = Field(default_factory=list)
    colors: list[ColorSpec] = Field(default_factory=list)
    # Tipografías del manual y, si no son de Google Fonts, la alternativa (la digital del manual si la da).
    font_heading: str = ""
    font_heading_alternative: str = ""
    font_body: str = ""
    font_body_alternative: str = ""
    website: str = ""
    instagram: str = ""
    pillar_educational: str = ""
    pillar_sales: str = ""
    pillar_connection: str = ""
    objectives: list[str] = Field(default_factory=list)
    audience: str = ""
    competitors: list[str] = Field(default_factory=list)
    tone: list[str] = Field(default_factory=list)
    business_facts: list[str] = Field(default_factory=list)
    notes: str = ""


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
    photo: str = ""  # archivo del banco de fotos del cliente que lleva el diseño

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

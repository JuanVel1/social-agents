"""Modelos de datos. Los que terminan en el esquema del LLM usan solo str/listas para
que Gemini los acepte como response_schema."""
from __future__ import annotations

import json
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
    # Si un pin del moodboard muestra una fotografía de una persona, su plantilla lleva una foto real del
    # banco elegida por su pose. Las fotos se usan tal cual: solo se les quita el fondo.
    real_photo_references: bool = False
    # Generar con IA la imagen completa de cada pieza cuya referencia muestra una persona real (covers.py):
    # el pin como composición y las fotos reales del banco como identidad. Usa GEMINI_IMAGE_MODEL (de pago).
    ai_images: bool = False
    # Rasgos fijos de la persona, en inglés, para los prompts de imagen (pelo, barba, piercings, tatuajes).
    person: str = ""


class Moodboard(BaseModel):
    """Tablero de Pinterest con referencias visuales que alguien cura a mano (pines guardados)."""
    pinterest: str = ""  # ID del canal de Pinterest en Postiz (de ahí sale el token, solo lectura)
    board: str = ""      # ID numérico del tablero (`social-agent integrations` lo lista)


class VisualGuide(BaseModel):
    """Dirección visual que Gemini saca de los pines del moodboard."""
    summary: str = ""
    composition: list[str] = Field(default_factory=list)
    color_and_light: list[str] = Field(default_factory=list)
    imagery: list[str] = Field(default_factory=list)
    graphic_elements: list[str] = Field(default_factory=list)
    trends: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)

    def as_text(self) -> str:
        parts = [self.summary] + [f"{label}: {'; '.join(items)}" for label, items in (
            ("Composición", self.composition), ("Color y luz", self.color_and_light), ("Imágenes", self.imagery),
            ("Recursos gráficos", self.graphic_elements), ("Tendencias", self.trends), ("Evitar", self.avoid))
            if items]
        return "\n".join(p for p in parts if p)


class RefField(BaseModel):
    name: str          # identificador en minúsculas: titulo, mensaje_1, etiqueta_2...
    description: str   # qué va en el hueco
    max_chars: int


class ReferenceDesign(BaseModel):
    """Lo que Gemini saca de un pin: la idea, cómo llevarla a la marca y la plantilla que la recrea."""
    concept: str = ""          # la idea creativa del pin, sin nombrar a su autor
    adaptation: str = ""       # cómo se aplica a la marca
    season: str = ""           # temporada o fecha a la que está atada (navidad, mundial...) o vacío
    background: str = "pizarra"  # papel o pizarra (los fondos del estilo de la marca)
    photo: bool = False        # usa la imagen de la persona de la marca
    photo_hint: str = ""       # qué pose o gesto pide la composición
    photo_file: str = ""       # la pose del banco elegida (file exacto); la plantilla se dimensiona para ella
    extra_font: str = ""       # familia de Google Fonts con el carácter tipográfico del pin (var(--extra))
    fields: list[RefField] = Field(default_factory=list)
    example: list[FieldValue] = Field(default_factory=list)
    template: str = ""         # plantilla Jinja que extiende base.html.j2
    # Prompt detallado para generar la imagen completa con IA, con un hueco [[campo]] por texto (covers.py).
    image_prompt: str = ""


class PinSubject(BaseModel):
    """Si el sujeto de un pin es una fotografía de una persona y qué foto real del banco hace su papel."""
    realistic_person: bool = False
    pose: str = ""        # gesto y encuadre de la persona del pin
    photo_file: str = ""  # file exacto del catálogo de fotos reales, o vacío


class ImagePrompt(BaseModel):
    prompt: str = ""


class ReferenceReview(BaseModel):
    ok: bool = False
    issues: str = ""


class ReferenceChoice(BaseModel):
    slot_id: str
    reference: str = ""  # id de la referencia o vacío
    topic: str = ""      # el tema ajustado a la idea de la referencia


class ReferenceChoices(BaseModel):
    choices: list[ReferenceChoice]


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
    # Canales de Postiz del cliente: {"instagram": "<integration id>", "facebook": "<id>", "pinterest": "<id>"}
    postiz_integrations: dict[str, str] = Field(default_factory=dict)
    # Pinterest: ID numérico del tablero donde van los pines (`social-agent integrations` lo lista) y el
    # enlace al que lleva cada pin. Sin tablero, Pinterest se omite y el resto de canales sigue igual.
    pinterest_board: str = ""
    website: str = ""
    # Moodboard de Pinterest (paso "Investigación visual" del flujo): `social-agent moodboard` lo lee y
    # guarda la dirección visual en clients/<slug>/moodboard.json, que llega a la parrilla.
    moodboard: Moodboard = Field(default_factory=Moodboard)
    visual_direction: str = Field(default="", exclude=True)  # se carga de moodboard.json

    @classmethod
    def load(cls, clients_dir: Path, slug: str) -> Brand:
        path = clients_dir / slug / "brand.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        brand = cls(slug=slug, **data)
        guide = clients_dir / slug / "moodboard.json"
        if guide.exists():
            brand.visual_direction = VisualGuide.model_validate(
                json.loads(guide.read_text(encoding="utf-8")).get("guide", {})).as_text()
        return brand


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


class FieldValue(BaseModel):
    name: str
    value: str


class FieldSet(BaseModel):
    slot_id: str
    fields: list[FieldValue]


class FieldBatch(BaseModel):
    items: list[FieldSet]


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
    # Textos de los huecos de la plantilla de referencia (si la pieza adapta un pin del moodboard).
    fields: list[FieldValue] = Field(default_factory=list)


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
    generated: str = ""  # imagen completa generada con IA (covers.py); reemplaza a la de la plantilla
    # Referencia del moodboard que adapta la pieza (carpeta en clients/<slug>/referencias/) y su origen.
    reference: str = ""
    reference_url: str = ""

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

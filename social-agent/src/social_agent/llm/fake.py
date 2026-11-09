"""Proveedor sin red para pruebas y para revisar plantillas sin gastar cuota."""
from __future__ import annotations

import json
import re
from typing import Sequence

from ..models import (
    BrandIntake,
    ColorSpec,
    HashtagBatch,
    HashtagSet,
    MonthOutline,
    PhotoChoice,
    PhotoChoices,
    PhotoReview,
    PhotoReviewBatch,
    PieceContent,
    Slide,
    TopicAssignment,
    Visual,
    VisualBatch,
    WeekContent,
)
from .base import Attachment, T, Tier


def _json_block(prompt: str, tag: str) -> list[dict]:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", prompt, re.S)
    return json.loads(match.group(1)) if match else []


class FakeProvider:
    def generate(self, prompt: str, schema: type[T], *, tier: Tier = "quality", system: str = "",
                 attachments: Sequence[Attachment] = ()) -> T:
        if schema is MonthOutline:
            slots = _json_block(prompt, "slots")
            pillars = [p["name"] for p in _json_block(prompt, "pillars")] or ["General"]
            return MonthOutline(assignments=[
                TopicAssignment(
                    slot_id=s["id"],
                    pillar=pillars[i % len(pillars)],
                    topic=f"Tema de ejemplo {i + 1}",
                    angle="Ángulo de ejemplo para revisar la plantilla",
                )
                for i, s in enumerate(slots)
            ])
        if schema is WeekContent:
            pieces = []
            for item in _json_block(prompt, "pieces"):
                n = 6 if item["type"] == "carousel" else 1
                pieces.append(PieceContent(
                    slot_id=item["slot_id"],
                    headline=item["topic"],
                    highlight=item["topic"].split()[-1],
                    visual="Persona sonriendo, camiseta negra, con una laptop",
                    slides=[Slide(heading=f"Punto {k + 1}", body="Texto de ejemplo que ocupa dos líneas en la pieza para probar el diseño.") for k in range(n)],
                    caption="Texto de ejemplo para la publicación.",
                    cta="Escríbenos por mensaje directo.",
                    sticker="¿Te pasó alguna vez?" if item["type"] == "story" else "",
                    script="GANCHO: ...\nDESARROLLO: ...\nCTA: ..." if item["type"] == "reel_script" else "",
                ))
            return WeekContent(pieces=pieces)
        if schema is HashtagBatch:
            return HashtagBatch(items=[
                HashtagSet(slot_id=item["slot_id"], hashtags=["ejemplo", "marca", "emprendimiento"])
                for item in _json_block(prompt, "pieces")
            ])
        if schema is PhotoReviewBatch:
            return PhotoReviewBatch(reviews=[
                PhotoReview(id=photo_id, usable=True, description="Persona sonriendo a cámara", framing="medio cuerpo")
                for photo_id in _json_block(prompt, "photos")
            ])
        if schema is PhotoChoices:
            files = [p["file"] for p in _json_block(prompt, "photos")]
            return PhotoChoices(choices=[
                PhotoChoice(slot_id=item["slot_id"], photo=files[i % len(files)])
                for i, item in enumerate(_json_block(prompt, "pieces"))
            ])
        if schema is VisualBatch:
            return VisualBatch(items=[
                Visual(slot_id=item["slot_id"], visual="Persona explicando con las manos, camiseta blanca")
                for item in _json_block(prompt, "pieces")
            ])
        if schema is BrandIntake:
            return BrandIntake(
                sector="Alimentos y bebidas",
                networks=["Instagram", "Snapchat"],
                colors=[
                    ColorSpec(role="primary", name="Terracota", hex="#8A3B12"),
                    ColorSpec(role="background", name="Crema", cmyk="0, 3, 6, 4"),
                    ColorSpec(role="accent", name="Oro", pantone="871 C"),
                ],
                font_heading="Branding Bold",
                font_heading_alternative="Fraunces",
                font_body="Work Sans",
                pillar_educational="Enseñar a preparar buen café en casa",
                audience="Adultos de 25 a 45 años que trabajan cerca del local",
                tone=["Cercano", "Sarcástico"],
                business_facts=["Café de especialidad de origen antioqueño"],
                notes="Ejemplo del proveedor falso",
            )
        raise TypeError(f"FakeProvider no sabe generar {schema.__name__}")

    def generate_image(self, prompt: str, references: Sequence[Attachment] = (), aspect: str = "3:4") -> bytes:
        """Devuelve la primera referencia tal cual: sirve para probar el flujo sin generar nada."""
        if references:
            return references[0][0]
        raise ValueError("FakeProvider necesita al menos una imagen de referencia")

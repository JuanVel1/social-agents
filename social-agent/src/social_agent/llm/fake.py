"""Proveedor sin red para pruebas y para revisar plantillas sin gastar cuota."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Sequence

from .. import progress

from ..models import (
    BrandIntake,
    ColorSpec,
    FieldValue,
    HashtagBatch,
    HashtagSet,
    ImagePrompt,
    MonthOutline,
    PhotoChoice,
    PhotoChoices,
    PhotoReview,
    PhotoReviewBatch,
    PinSubject,
    PieceContent,
    RefField,
    ReferenceChoice,
    ReferenceChoices,
    ReferenceDesign,
    ReferenceReview,
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


def _pause() -> None:
    """FAKE_LLM_DELAY=segundos simula la latencia de un modelo real (para ver el panel en acción)."""
    progress.llm_call("fake")
    delay = float(os.environ.get("FAKE_LLM_DELAY") or 0)
    if delay > 0:
        time.sleep(delay)


class FakeProvider:
    def generate(self, prompt: str, schema: type[T], *, tier: Tier = "quality", system: str = "",
                 attachments: Sequence[Attachment] = ()) -> T:
        _pause()
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
                    fields=[FieldValue(name=f["name"], value=f"Texto de {f['name']}")
                            for f in item.get("reference", {}).get("fields", [])],
                ))
            return WeekContent(pieces=pieces)
        if schema.__name__ == "FieldBatch":
            from ..models import FieldSet
            return schema(items=[FieldSet(slot_id=i["slot_id"], fields=[FieldValue(name=f["name"], value="Relleno")
                                                                           for f in i["missing"]])
                                 for i in _json_block(prompt, "pieces")])
        if schema is ReferenceChoices:
            # Solo elige para la primera pieza: el resto lo completa el reparto de respaldo.
            refs = [r["id"] for r in _json_block(prompt, "references")]
            pieces = _json_block(prompt, "pieces")
            return ReferenceChoices(choices=[
                ReferenceChoice(slot_id=p["slot_id"], reference=refs[0] if i == 0 and refs else "",
                                topic=f"{p['topic']} (adaptado)")
                for i, p in enumerate(pieces)
            ])
        if schema is ReferenceDesign:
            return ReferenceDesign(
                concept="Conversación de chat con un personaje de temporada", adaptation="Chat con la marca",
                season="navidad", background="pizarra", photo=True, photo_hint="Con una laptop",
                photo_file=(_json_block(prompt, "poses") or [{}])[0].get("file", ""),
                fields=[RefField(name="contacto", description="Nombre del chat", max_chars=24),
                        RefField(name="mensaje_1", description="Primer mensaje", max_chars=40)],
                example=[FieldValue(name="contacto", value="Vic"), FieldValue(name="mensaje_1", value="¡Hola!")],
                template=('{% extends "referencia_base.html.j2" %}{% block style %}body { background: '
                          'var(--primary); } .chat { position: absolute; top: 200px; left: 80px; font-size: 40px; '
                          'color: #fff; }{% endblock %}{% block content %}<div class="chat">{% if kind == "slide" %}'
                          '<b>{{ slide.heading }}</b><p>{{ slide.body }}</p>{% else %}<b>{{ f.contacto }}</b>'
                          '<p>{{ f.mensaje_1 }}</p>{% endif %}</div>{% if photo %}<img class="photo" '
                          'src="{{ photo }}" alt="">{% endif %}{% endblock %}'),
            )
        if schema is ReferenceReview:
            return ReferenceReview(ok=True)
        if schema is PinSubject:
            return PinSubject()
        if schema is ImagePrompt:
            names = [f["name"] for f in json.loads(prompt.split("vez cada uno): ")[1].split("\n")[0])] \
                if "vez cada uno): " in prompt else []
            return ImagePrompt(prompt="Photorealistic cover of the man. " + " ".join(f'"[[{n}]]"' for n in names))
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
        if schema.__name__ == "VisualGuide":
            return schema(summary="Fotos luminosas con mucho aire y titulares grandes.",
                          composition=["Sujeto a un lado y texto al otro"], avoid=["Fondos recargados"])
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
        _pause()
        if references:
            return references[0][0]
        raise ValueError("FakeProvider necesita al menos una imagen de referencia")

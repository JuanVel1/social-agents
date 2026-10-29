"""Proveedor sin red para pruebas y para revisar plantillas sin gastar cuota."""
from __future__ import annotations

import json
import re

from ..models import (
    HashtagBatch,
    HashtagSet,
    MonthOutline,
    PieceContent,
    Slide,
    TopicAssignment,
    WeekContent,
)
from .base import T, Tier


def _json_block(prompt: str, tag: str) -> list[dict]:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", prompt, re.S)
    return json.loads(match.group(1)) if match else []


class FakeProvider:
    def generate(self, prompt: str, schema: type[T], *, tier: Tier = "quality", system: str = "") -> T:
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
        raise TypeError(f"FakeProvider no sabe generar {schema.__name__}")

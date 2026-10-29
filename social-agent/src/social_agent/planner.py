"""Parrilla mensual. El código fija fechas y conteos del plan; el modelo asigna temas y escribe."""
from __future__ import annotations

import calendar
import json
import logging
from collections import defaultdict
from datetime import date

from .llm import LLMProvider
from .models import (
    Brand,
    HashtagBatch,
    MonthOutline,
    MonthPlan,
    Piece,
    PieceType,
    Slot,
    WeekContent,
)

log = logging.getLogger(__name__)

# Día de la semana (0 = lunes) en que sale cada tipo de pieza.
# Plan 1: 2 posts, 1 carrusel, 3 historias y 2 guiones por semana.
SCHEDULES: dict[str, dict[PieceType, tuple[int, ...]]] = {
    "plan1": {
        "post": (0, 3),
        "carousel": (1,),
        "story": (0, 2, 4),
        "reel_script": (2, 5),
    },
}

SYSTEM = (
    "Eres estratega de contenido para redes sociales de pequeñas empresas en Colombia. "
    "Escribes en español de Colombia, claro y cercano, sin anglicismos innecesarios, "
    "y respetas al pie de la letra el tono y los pilares de la marca. "
    "Solo puedes afirmar hechos concretos que aparezcan en <facts> o en la descripción de la marca. "
    "No inventes nombres de personas, lugares, fincas, productos, precios, horarios, años, cifras, "
    "premios ni testimonios: si la pieza necesita uno que no está, escribe [DATO POR CONFIRMAR]. "
    "Puedes hablar en términos generales (\"nuestro equipo\", \"los caficultores\") sin dar nombres."
)

TYPE_RULES = """\
- post: headline de máximo 8 palabras; slides con exactamente 1 elemento (body de máximo 25 palabras); caption de 60 a 120 palabras.
- carousel: headline gancho de máximo 8 palabras; slides con 6 a 8 elementos: el primero engancha, los del medio enseñan un punto cada uno (body de máximo 30 palabras), el último invita a la acción; caption de 60 a 120 palabras.
- story: headline de máximo 10 palabras; slides con exactamente 1 elemento (body de máximo 15 palabras); sticker con la pregunta o encuesta interactiva; caption vacío.
- reel_script: headline con el título interno; slides vacío; script con las secciones GANCHO (0-3 s), DESARROLLO y CTA, para 30 a 45 segundos, con indicaciones de toma entre corchetes; caption de 40 a 80 palabras."""


def build_slots(month: str, plan: str) -> list[Slot]:
    year, mon = (int(x) for x in month.split("-"))
    schedule = SCHEDULES[plan]
    slots: list[Slot] = []
    for day in range(1, calendar.monthrange(year, mon)[1] + 1):
        d = date(year, mon, day)
        for piece_type, weekdays in schedule.items():
            if d.weekday() in weekdays:
                slots.append(Slot(id=f"{d:%m%d}-{piece_type}", type=piece_type, date=d))
    return slots


def _brand_brief(brand: Brand) -> str:
    facts = "\n".join(f"- {f}" for f in brand.facts) or "(ninguno: no afirmes datos concretos del negocio)"
    return (
        f"Marca: {brand.name}\nSector: {brand.sector}\nAudiencia: {brand.audience}\n"
        f"Tono: {brand.tone}\n<pillars>{json.dumps([p.model_dump() for p in brand.pillars], ensure_ascii=False)}</pillars>\n"
        f"<facts>\n{facts}\n</facts>"
    )


def plan_month(brand: Brand, month: str, llm: LLMProvider) -> MonthPlan:
    slots = build_slots(month, brand.plan)
    slot_json = json.dumps([{"id": s.id, "type": s.type, "date": s.date.isoformat()} for s in slots], ensure_ascii=False)

    outline = llm.generate(
        f"{_brand_brief(brand)}\n\n"
        f"Mes: {month}. Asigna a cada espacio del calendario un pilar y un tema concreto.\n"
        "Reglas: reparte los pilares de forma equilibrada; no repitas temas; los carruseles "
        "profundizan en un tema educativo; las historias invitan a interactuar; los guiones de "
        "reel tienen un tema que se pueda grabar con el celular en el negocio. Usa exactamente "
        "los nombres de pilar dados y devuelve una asignación por cada slot_id.\n"
        f"<slots>{slot_json}</slots>",
        MonthOutline,
        tier="quality",
        system=SYSTEM,
    )
    by_slot = {a.slot_id: a for a in outline.assignments}
    missing = [s.id for s in slots if s.id not in by_slot]
    if missing:
        raise RuntimeError(f"El modelo no asignó tema a {len(missing)} espacios: {missing[:5]}…")

    pieces = [
        Piece(id=s.id, type=s.type, date=s.date, pillar=by_slot[s.id].pillar,
              topic=by_slot[s.id].topic, angle=by_slot[s.id].angle)
        for s in slots
    ]

    weeks: dict[int, list[Piece]] = defaultdict(list)
    for p in pieces:
        weeks[p.date.isocalendar().week].append(p)
    for week, week_pieces in sorted(weeks.items()):
        log.info("Escribiendo semana %s (%s piezas)", week, len(week_pieces))
        _write_week(brand, week_pieces, llm)

    feed = [p for p in pieces if p.type in ("post", "carousel")]
    if feed:
        _add_hashtags(brand, feed, llm)

    return MonthPlan(client=brand.slug, month=month, pieces=pieces)


def _write_week(brand: Brand, pieces: list[Piece], llm: LLMProvider) -> None:
    items = [{"slot_id": p.id, "type": p.type, "date": p.date.isoformat(), "pillar": p.pillar,
              "topic": p.topic, "angle": p.angle} for p in pieces]
    result = llm.generate(
        f"{_brand_brief(brand)}\n\nEscribe el contenido completo de estas piezas.\n"
        f"Reglas por tipo:\n{TYPE_RULES}\n"
        "El cta es una sola frase de llamada a la acción. Devuelve una pieza por cada slot_id.\n"
        f"<pieces>{json.dumps(items, ensure_ascii=False)}</pieces>",
        WeekContent,
        tier="quality",
        system=SYSTEM,
    )
    by_slot = {c.slot_id: c for c in result.pieces}
    for p in pieces:
        p.content = by_slot.get(p.id)
        if p.content is None:
            raise RuntimeError(f"Falta el contenido de la pieza {p.id}")


def _add_hashtags(brand: Brand, pieces: list[Piece], llm: LLMProvider) -> None:
    items = [{"slot_id": p.id, "topic": p.topic, "headline": p.content.headline if p.content else ""} for p in pieces]
    result = llm.generate(
        f"Marca: {brand.name} ({brand.sector}). Audiencia: {brand.audience}.\n"
        "Para cada pieza propone de 8 a 12 hashtags en español, sin el símbolo #, mezclando "
        "temas del sector, la ciudad o región si aplica y el tema de la pieza.\n"
        f"<pieces>{json.dumps(items, ensure_ascii=False)}</pieces>",
        HashtagBatch,
        tier="bulk",
    )
    by_slot = {h.slot_id: h.hashtags for h in result.items}
    for p in pieces:
        p.hashtags = by_slot.get(p.id, [])

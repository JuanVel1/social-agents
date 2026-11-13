"""Parrilla mensual. El código fija fechas y conteos del plan; el modelo asigna temas y escribe."""
from __future__ import annotations

import calendar
import json
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import date

from . import progress
from .llm import LLMProvider
from .models import (
    Brand,
    HashtagBatch,
    MonthOutline,
    MonthPlan,
    Piece,
    FieldBatch,
    PieceType,
    ReferenceChoices,
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

VISUAL_RULE = (
    "visual: en una frase, la foto de la persona de la marca que acompaña la pieza: pose, gesto, "
    "expresión, ropa y un objeto si aplica (laptop, celular, libreta, taza), coherente con el tema y "
    "distinta entre piezas de la misma semana; sin texto, logos ni pantallas legibles."
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


# Opciones del formulario de onboarding → qué tipo de llamada a la acción las mueve.
OBJECTIVE_CTAS = {
    "Leads": "conseguir contactos: escribir por mensaje directo o WhatsApp, agendar, pedir información",
    "Ventas": "comprar o reservar un producto o servicio de los datos del negocio",
    "Reconocimiento": "que recuerden y compartan la marca: guardar, compartir, seguir la cuenta",
    "Fidelización": "cuidar a quienes ya son clientes: comentar su experiencia, volver, beneficios de la comunidad",
}


def _brand_brief(brand: Brand) -> str:
    facts = "\n".join(f"- {f}" for f in brand.facts) or "(ninguno: no afirmes datos concretos del negocio)"
    brief = (
        f"Marca: {brand.name}\nSector: {brand.sector}\nAudiencia: {brand.audience}\n"
        f"Tono: {brand.tone}\n<pillars>{json.dumps([p.model_dump() for p in brand.pillars], ensure_ascii=False)}</pillars>\n"
        f"<facts>\n{facts}\n</facts>"
    )
    if brand.objectives:
        goals = "\n".join(f"- {o}: {OBJECTIVE_CTAS.get(o, o)}" for o in brand.objectives)
        brief += (f"\n<objectives>\n{goals}\n</objectives>\n"
                  "Reparte las llamadas a la acción entre estos objetivos según el pilar de cada pieza.")
    if brand.competitors:
        brief += (f"\n<competitors>{', '.join(brand.competitors)}</competitors>\n"
                  "Son competidores o referentes: úsalos solo para diferenciar a la marca. Nunca los "
                  "nombres, ni en el texto ni en los hashtags, ni hagas comparaciones directas.")
    if brand.visual_direction:
        brief += (f"\n<visual_direction>\n{brand.visual_direction}\n</visual_direction>\n"
                  "Dirección visual sacada del moodboard de la marca en Pinterest: tenla en cuenta al "
                  "describir el visual de cada pieza.")
    return brief


REFERENCE_RULE = (
    "Si una pieza trae reference, su imagen es una plantilla que adapta esa idea de diseño a la marca: "
    "rellena fields con un valor para TODOS sus campos, sin dejar ninguno (name exacto, sin pasar "
    "max_chars), con los temas, datos y "
    "tono de la marca, nunca textos de la referencia original; headline, caption y cta cuentan el mismo "
    "mensaje. Su visual sigue el photo_hint. "
)


PIECE_KINDS = {"post": "Posts", "carousel": "Carruseles", "story": "Historias", "reel_script": "Guiones de Reel"}


def plan_month(brand: Brand, month: str, llm: LLMProvider, references: list[dict] | None = None) -> MonthPlan:
    """references: plantillas del moodboard (references.load_references); se asignan a los posts."""
    with progress.step("temas"):
        slots = build_slots(month, brand.plan)
        progress.detail(f"{len(slots)} espacios en el calendario de {month}")
        slot_json = json.dumps([{"id": s.id, "type": s.type, "date": s.date.isoformat()} for s in slots],
                               ensure_ascii=False)

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
        for kind, label in PIECE_KINDS.items():
            progress.metric(label, sum(1 for s in slots if s.type == kind))

    pieces = [
        Piece(id=s.id, type=s.type, date=s.date, pillar=by_slot[s.id].pillar,
              topic=by_slot[s.id].topic, angle=by_slot[s.id].angle)
        for s in slots
    ]

    if references:
        assign_references(brand, pieces, references, llm)
    else:
        progress.skip("asignar_referencias", "Sin plantillas del moodboard")
        progress.skip("campos", "Sin plantillas del moodboard")

    weeks: dict[int, list[Piece]] = defaultdict(list)
    for p in pieces:
        weeks[p.date.isocalendar().week].append(p)
    with progress.step("copy", total=len(weeks)):
        for week, week_pieces in sorted(weeks.items()):
            progress.detail(f"Semana {week} · {len(week_pieces)} piezas")
            log.info("Escribiendo semana %s (%s piezas)", week, len(week_pieces))
            _write_week(brand, week_pieces, llm, {r["id"]: r for r in references or []})
            progress.advance(item=f"Semana {week}", note=f"{len(week_pieces)} piezas")

    if references:
        fill_missing_fields(brand, pieces, {r["id"]: r for r in references}, llm)

    feed = [p for p in pieces if p.type in ("post", "carousel")]
    if feed:
        _add_hashtags(brand, feed, llm)
    else:
        progress.skip("hashtags", "Sin posts ni carruseles")

    return MonthPlan(client=brand.slug, month=month, pieces=pieces)


DESIGNED = ("post", "carousel", "story")


@progress.tracked("asignar_referencias")
def assign_references(brand: Brand, pieces: list[Piece], references: list[dict], llm: LLMProvider) -> None:
    """Reparte las referencias del moodboard entre todas las piezas con diseño (posts, carruseles, historias).

    Hay menos referencias que piezas, así que se repiten, pero de forma pareja y sin la misma en dos piezas
    seguidas. Las de temporada solo van en fechas que encajen. Si el modelo deja alguna pieza sin
    referencia, recibe la menos usada que no sea de temporada (o, si todas lo son, la menos usada).
    """
    designed = sorted((p for p in pieces if p.type in DESIGNED), key=lambda p: (p.date, p.id))
    if not designed or not references:
        return
    catalog = [{"id": r["id"], "concept": r["design"].concept, "adaptation": r["design"].adaptation,
                "season": r["design"].season} for r in references]
    items = [{"slot_id": p.id, "type": p.type, "date": p.date.isoformat(), "pillar": p.pillar, "topic": p.topic}
             for p in designed]
    result = llm.generate(
        f"{_brand_brief(brand)}\n\n"
        "Estas son ideas de diseño de referencia (pines del moodboard de la marca) ya adaptadas a su estilo y "
        "paleta. TODAS las piezas llevan una: asigna a cada pieza la referencia que mejor sirva para contar su "
        "tema. Hay menos referencias que piezas: repítelas de forma pareja y no pongas la misma en dos piezas "
        "seguidas. Las de temporada (season) van solo en fechas que encajen (navidad del 15 al 25 de diciembre, "
        "año nuevo del 26 de diciembre al 7 de enero, mundial durante el torneo). Si hace falta, ajusta topic "
        "para que el tema de la marca se cuente con esa idea (mismo pilar); si no, devuelve el topic igual.\n"
        f"<references>{json.dumps(catalog, ensure_ascii=False)}</references>\n"
        f"<pieces>{json.dumps(items, ensure_ascii=False)}</pieces>",
        ReferenceChoices, tier="quality", system=SYSTEM,
    )
    by_id = {r["id"]: r for r in references}
    choices = {c.slot_id: c for c in result.choices}
    uses = {r["id"]: 0 for r in references}
    previous = ""
    for piece in designed:
        choice = choices.get(piece.id)
        ref = choice.reference if choice and choice.reference in by_id else ""
        if not ref:
            # Primero que no sea de temporada (fuera de fecha se ve raro); después, que no repita la anterior.
            plain = [r for r in uses if not by_id[r]["design"].season]
            evergreen = [r for r in plain if r != previous] or plain or \
                [r for r in uses if r != previous] or list(uses)
            ref = min(evergreen, key=lambda r: (uses[r], r))
        elif choice.topic.strip():
            piece.topic = choice.topic.strip()
        piece.reference, piece.reference_url = ref, by_id[ref]["url"]
        uses[ref] += 1
        previous = ref
    log.info("Referencias del moodboard repartidas en %s piezas: %s", len(designed), uses)
    progress.detail(f"{len(designed)} piezas con plantilla, {len(uses)} referencias")
    for ref, n in uses.items():
        progress.metric(ref, n)


@progress.tracked("campos")
def fill_missing_fields(brand: Brand, pieces: list[Piece], references: dict, llm: LLMProvider) -> int:
    """Pide de nuevo los huecos de texto que quedaron vacíos o que se pasan de su max_chars.

    Un texto largo se recortaría con "…" en el diseño; es mejor pedir una versión que quepa.
    """
    pending = []
    for p in pieces:
        if p.reference not in references or not p.content:
            continue
        limits = {f.name: f.max_chars for f in references[p.reference]["design"].fields}
        given = {f.name for f in p.content.fields if f.value.strip() and len(" ".join(f.value.split())) <= limits.get(f.name, 10**6)}
        missing = [f for f in references[p.reference]["design"].fields if f.name not in given]
        if missing:
            pending.append((p, missing))
    if not pending:
        progress.detail("Todas las plantillas quedaron completas")
        return 0
    progress.detail(f"{len(pending)} piezas con campos vacíos")
    items = [{"slot_id": p.id, "headline": p.content.headline, "caption": p.content.caption[:400],
              "concept": references[p.reference]["design"].concept,
              "filled": [f.model_dump() for f in p.content.fields if f.value.strip()],
              "missing": [f.model_dump() for f in missing]} for p, missing in pending]
    result = llm.generate(
        f"{_brand_brief(brand)}\n\n"
        "Estas piezas usan una plantilla de diseño con huecos de texto; los de missing faltan o eran demasiado "
        "largos para su espacio. Escríbelos de nuevo (name exacto), contando caracteres: cada uno con como "
        "máximo su max_chars, espacios incluidos (más corto es mejor). Coherentes con lo ya escrito (filled), el "
        "headline y el caption, con datos de la marca.\n"
        f"<pieces>{json.dumps(items, ensure_ascii=False)}</pieces>",
        FieldBatch, tier="bulk", system=SYSTEM,
    )
    by_slot = {i.slot_id: i.fields for i in result.items}
    filled = 0
    for p, missing in pending:
        names = {f.name for f in missing}
        new = [v for v in by_slot.get(p.id, []) if v.name in names and v.value.strip()]
        p.content.fields = [f for f in p.content.fields if f.value.strip() and f.name not in names] + new
        filled += len(new)
        if len(new) < len(missing):
            log.warning("%s sigue con campos vacíos en su plantilla de referencia", p.id)
    return filled


def _write_week(brand: Brand, pieces: list[Piece], llm: LLMProvider, references: dict | None = None) -> None:
    references = references or {}
    items = []
    for p in pieces:
        item = {"slot_id": p.id, "type": p.type, "date": p.date.isoformat(), "pillar": p.pillar,
                "topic": p.topic, "angle": p.angle}
        if p.reference in references:
            design = references[p.reference]["design"]
            item["reference"] = {"concept": design.concept, "adaptation": design.adaptation,
                                 "photo_hint": design.photo_hint if design.photo else "",
                                 "fields": [f.model_dump() for f in design.fields]}
        items.append(item)
    result = llm.generate(
        f"{_brand_brief(brand)}\n\nEscribe el contenido completo de estas piezas.\n"
        f"Reglas por tipo:\n{TYPE_RULES}\n"
        "El cta es una sola frase de llamada a la acción. highlight: 1 o 2 palabras seguidas, copiadas "
        "tal cual del headline, que el diseño resaltará en color (la idea clave, no un artículo). "
        f"{VISUAL_RULE} "
        + (REFERENCE_RULE if any("reference" in i for i in items) else "")
        + "Devuelve una pieza por cada slot_id.\n"
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


@progress.tracked("hashtags")
def _add_hashtags(brand: Brand, pieces: list[Piece], llm: LLMProvider) -> None:
    progress.detail(f"{len(pieces)} posts y carruseles")
    items = [{"slot_id": p.id, "topic": p.topic, "headline": p.content.headline if p.content else ""} for p in pieces]
    result = llm.generate(
        f"Marca: {brand.name} ({brand.sector}). Audiencia: {brand.audience}.\n"
        "Para cada pieza propone de 8 a 12 hashtags en español, sin el símbolo #, mezclando "
        "temas del sector, la ciudad o región si aplica y el tema de la pieza.\n"
        + (f"No uses nombres de estas marcas: {', '.join(brand.competitors)}.\n" if brand.competitors else "")
        + f"<pieces>{json.dumps(items, ensure_ascii=False)}</pieces>",
        HashtagBatch,
        tier="bulk",
    )
    by_slot = {h.slot_id: h.hashtags for h in result.items}
    blocked = [b for b in (_compact(c) for c in brand.competitors) if b]
    for p in pieces:
        p.hashtags = [h for h in by_slot.get(p.id, []) if not any(b in _compact(h) for b in blocked)]


def _compact(value: str) -> str:
    """'Café Velvet' y '#cafevelvet' → 'cafevelvet', para comparar hashtags con nombres."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_value.lower())

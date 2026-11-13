"""Onboarding de clientes en Notion: formulario → fila → brand.yaml.

El cliente llena la vista Formulario de la base "Onboarding de clientes" (la crea `onboarding setup`).
`onboarding run` hace dos pasadas:
1. Filas Nuevo (o sin Estado): si traen el manual de marca en PDF, Gemini lo lee y rellena solo los
   campos vacíos, con las opciones de cada selección; la fila pasa a Por revisar.
2. Filas Aprobado: genera clients/<slug>/brand.yaml, descarga el logo y las deja en Activo
   (o en Error, con el motivo en "Notas del agente").
"""
from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from pathlib import Path
from typing import Callable

import httpx
import yaml

from . import progress
from .approval import API, _text, database_id_from
from .llm.base import LLMProvider
from .models import Brand, BrandIntake, ColorSpec

log = logging.getLogger(__name__)

TITLE = "Onboarding de clientes"
STATES = ["Nuevo", "Por revisar", "Aprobado", "Activo", "Error"]
SECTORS = [
    "Alimentos y bebidas", "Moda y belleza", "Salud y bienestar", "Educación y formación",
    "Marketing y servicios profesionales", "Tecnología", "Inmobiliaria", "Turismo y hotelería",
    "Comercio minorista", "Deportes y entretenimiento", "Otro",
]
NETWORKS = ["Instagram", "Facebook", "LinkedIn", "TikTok", "Pinterest", "YouTube"]
TONES = ["Educativo", "Formal", "Divertido", "Directo", "Inspirador", "Cercano"]
OBJECTIVES = ["Leads", "Ventas", "Reconocimiento", "Fidelización"]
# Propiedad de Notion → nombre del pilar en brand.yaml
PILLARS = {"Pilar educativo": "Educativo", "Pilar de venta": "Conversión", "Pilar de conexión": "Conexión"}


def _options(names: list[str]) -> dict:
    return {"options": [{"name": n} for n in names]}


# Una propiedad por pregunta del formulario (secciones A, B y C del documento de configuración).
PROPERTIES: dict[str, dict] = {
    "Marca": {"title": {}},
    "Sector": {"select": _options(SECTORS)},
    "Manual de marca": {"files": {}},
    "Logo": {"files": {}},
    "Colores": {"rich_text": {}},
    "Tipografías": {"rich_text": {}},
    "Sitio web": {"url": {}},
    "Instagram": {"rich_text": {}},
    "Redes activas": {"multi_select": _options(NETWORKS)},
    "Manejo actual": {"select": _options(["Interno", "Agencia", "Parcial", "Nadie"])},
    "Accesos Meta": {"select": _options(["Tiene Business Suite", "Solo IG profesional", "Ninguno"])},
    **{name: {"rich_text": {}} for name in PILLARS},
    "Pauta paga": {"select": _options(["No", "Continua", "Por campañas", "Ocasional"])},
    "Presupuesto mensual": {"number": {"format": "number"}},
    "Objetivos": {"multi_select": _options(OBJECTIVES)},
    "Cliente ideal": {"rich_text": {}},
    "Competidores": {"rich_text": {}},
    "Tono": {"multi_select": _options(TONES)},
    "Datos del negocio": {"rich_text": {}},
    # Uso interno: no van en la vista Formulario.
    "Estado": {"select": _options(STATES)},
    "Slug": {"rich_text": {}},
    "Completado por IA": {"rich_text": {}},
    "Notas del agente": {"rich_text": {}},
}

COLOR_LABELS = {
    "primary": ("primario", "principal", "primary"),
    "secondary": ("secundario", "secondary"),
    "background": ("fondo", "background"),
    "text": ("texto", "text"),
    "accent": ("acento", "accent"),
}
FONT_LABELS = {
    "heading": ("título", "titulo", "titular", "heading"),
    "body": ("texto", "cuerpo", "body"),
}
HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
# Grosores y estilos que los manuales añaden al nombre ("Lato Regular"); Google Fonts solo quiere la familia.
FONT_STYLE = re.compile(
    r"(\s+(thin|hairline|extra[- ]?light|ultra[- ]?light|light|regular|normal|book|medium|semi[- ]?bold|"
    r"demi[- ]?bold|bold|extra[- ]?bold|ultra[- ]?bold|black|heavy|italic|oblique|\d{3}))+$", re.I)


def font_family(name: str) -> str:
    return FONT_STYLE.sub("", name.strip())


class OnboardingError(ValueError):
    """La fila no tiene lo necesario para generar el brand.yaml; el mensaje va a Notas del agente."""


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def plain(prop: dict) -> str | float | list[str] | list[dict] | None:
    """Valor simple de una propiedad de Notion."""
    kind = prop["type"]
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(t["plain_text"] for t in value).strip()
    if kind == "select":
        return value["name"] if value else None
    if kind == "multi_select":
        return [o["name"] for o in value]
    if kind in ("url", "number", "files"):
        return value
    return None


def row_values(page: dict) -> dict:
    return {name: plain(prop) for name, prop in page["properties"].items()}


def _lines(text: str | None) -> list[str]:
    return [line.strip(" -•\t") for line in (text or "").splitlines() if line.strip(" -•\t")]


def _is_dark(hex_color: str) -> bool:
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b < 128


def parse_colors(text: str | None) -> dict[str, str]:
    """`primario #16a4c8, fondo #fff` → colores de brand.yaml. Los hex sin etiqueta se asignan en orden."""
    colors: dict[str, str] = {}
    unlabeled: list[str] = []
    for chunk in re.split(r"[,;\n]", text or ""):
        found = HEX.search(chunk)
        if not found:
            continue
        label = chunk[:found.start()].lower()
        key = next((k for k, words in COLOR_LABELS.items() if any(w in label for w in words)), None)
        if key and key not in colors:
            colors[key] = found.group(0).lower()
        else:
            unlabeled.append(found.group(0).lower())
    for key in COLOR_LABELS:
        if key not in colors and unlabeled:
            colors[key] = unlabeled.pop(0)
    if "primary" not in colors:
        raise OnboardingError("Falta al menos un color en hex en 'Colores' (p. ej. primario #16a4c8)")
    colors.setdefault("secondary", colors["primary"])
    colors.setdefault("background", "#ffffff")
    colors.setdefault("text", "#ffffff" if _is_dark(colors["background"]) else "#1f1f1f")
    colors.setdefault("accent", colors["secondary"])
    return {k: colors[k] for k in COLOR_LABELS}


def parse_fonts(text: str | None) -> dict[str, str]:
    """`títulos: Antonio, texto: Inter` → fuentes de brand.yaml. Con una sola fuente se usa para ambos."""
    fonts: dict[str, str] = {}
    unlabeled: list[str] = []
    for chunk in re.split(r"[,;\n]", text or ""):
        label, _, name = chunk.rpartition(":")
        name = font_family(name)
        if not name:
            continue
        key = next((k for k, words in FONT_LABELS.items() if any(w in label.lower() for w in words)), None)
        if key and key not in fonts:
            fonts[key] = name
        else:
            unlabeled.append(name)
    for key in FONT_LABELS:
        if key not in fonts and unlabeled:
            fonts[key] = unlabeled.pop(0)
    heading = fonts.get("heading") or fonts.get("body") or "Inter"
    return {"heading": heading, "body": fonts.get("body") or heading}


def _tone(tones: list[str]) -> str:
    if not tones:
        raise OnboardingError("Falta el 'Tono'")
    names = [t.lower() for t in tones]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} y {names[-1]}"
    return joined[0].upper() + joined[1:]


def brand_data(values: dict) -> dict:
    """Fila de onboarding → contenido de brand.yaml (sin postiz_integrations, que salen de Postiz)."""
    name = values.get("Marca") or ""
    if not name:
        raise OnboardingError("Falta 'Marca'")
    audience = values.get("Cliente ideal")
    if not audience:
        raise OnboardingError("Falta 'Cliente ideal'")
    pillars = [{"name": label, "goal": values[prop]} for prop, label in PILLARS.items() if values.get(prop)]
    if not pillars:
        raise OnboardingError("Falta al menos un pilar de contenido")

    facts = _lines(values.get("Datos del negocio"))
    if values.get("Sitio web"):
        facts.append(f"Sitio web: {values['Sitio web']}")
    handle = (values.get("Instagram") or "").strip()
    if handle and not handle.startswith("@"):
        handle = "@" + handle.rstrip("/").rsplit("/", 1)[-1]

    return {
        "name": name,
        "sector": values.get("Sector") or "Otro",
        "audience": audience,
        "tone": _tone(values.get("Tono") or []),
        "handle": handle or None,
        "plan": "plan1",
        "pillars": pillars,
        "facts": facts,
        "objectives": values.get("Objetivos") or [],
        "competitors": [c.strip() for line in _lines(values.get("Competidores")) for c in line.split(",") if c.strip()],
        "colors": parse_colors(values.get("Colores")),
        "fonts": parse_fonts(values.get("Tipografías")),
        "logo": None,
        "postiz_integrations": {},
    }


def file_url(item: dict) -> str:
    return item[item["type"]]["url"]


def file_suffix(item: dict) -> str:
    return Path(item.get("name") or file_url(item).split("?")[0]).suffix.lower()


def logo_name(item: dict) -> str:
    suffix = file_suffix(item)
    return f"logo{suffix if suffix in ('.png', '.svg', '.jpg', '.jpeg', '.webp') else '.png'}"


class OnboardingBoard:
    def __init__(self, token: str, database_id: str = "", client: httpx.Client | None = None):
        if not token:
            raise RuntimeError("Falta NOTION_TOKEN en .env")
        self.database_id = database_id_from(database_id) if database_id else ""
        self.http = client or httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=30,
        )

    def create_database(self, parent: str) -> dict:
        """Crea la base de datos dentro de una página compartida con la integración."""
        response = self.http.post("/databases", json={
            "parent": {"type": "page_id", "page_id": database_id_from(parent)},
            "title": _text(TITLE),
            "properties": PROPERTIES,
        })
        response.raise_for_status()
        return response.json()

    def schema(self) -> dict[str, dict]:
        response = self.http.get(f"/databases/{self.database_id}")
        response.raise_for_status()
        return response.json()["properties"]

    def ensure_properties(self) -> list[str]:
        """Recrea las propiedades que falten (quitar una pregunta del formulario la borra de la base) y
        añade las opciones nuevas del código a las selecciones, conservando las que se crearon en Notion."""
        schema = self.schema()
        changes: dict[str, dict] = {}
        for name, prop in PROPERTIES.items():
            kind = next(iter(prop))
            if name not in schema:
                changes[name] = prop
            elif kind in ("select", "multi_select"):
                current = [{"name": o["name"]} for o in schema[name][kind]["options"]]
                known = {o["name"] for o in current}
                new = [o for o in prop[kind]["options"] if o["name"] not in known]
                if new:
                    changes[name] = {kind: {"options": current + new}}
        if changes:
            response = self.http.patch(f"/databases/{self.database_id}", json={"properties": changes})
            response.raise_for_status()
            log.warning("Propiedades actualizadas en Notion: %s", ", ".join(changes))
        return list(changes)

    def options(self) -> dict[str, list[str]]:
        """Opciones actuales de cada selección: si alguien añade una en Notion, el agente la usa."""
        return {name: [o["name"] for o in prop[prop["type"]]["options"]]
                for name, prop in self.schema().items() if prop["type"] in ("select", "multi_select")}

    def rows(self, state: str) -> list[dict]:
        if not self.database_id:
            raise RuntimeError("Falta NOTION_ONBOARDING_DATABASE_ID en .env (créala con: social-agent onboarding setup)")
        condition: dict = {"property": "Estado", "select": {"equals": state}}
        if state == "Nuevo":
            # Las respuestas del formulario pueden llegar sin Estado.
            condition = {"or": [condition, {"property": "Estado", "select": {"is_empty": True}}]}
        body: dict = {"filter": condition}
        results = []
        while True:
            response = self.http.post(f"/databases/{self.database_id}/query", json=body)
            response.raise_for_status()
            data = response.json()
            results += data["results"]
            if not data.get("has_more"):
                return results
            body["start_cursor"] = data["next_cursor"]

    def update(self, page_id: str, state: str, notes: str = "", slug: str | None = None,
               extra: dict | None = None) -> None:
        properties: dict = {
            **(extra or {}),
            "Estado": {"select": {"name": state}},
            "Notas del agente": {"rich_text": _text(notes)},
        }
        if slug is not None:
            properties["Slug"] = {"rich_text": _text(slug)}
        response = self.http.patch(f"/pages/{page_id}", json={"properties": properties})
        response.raise_for_status()


def activate(page: dict, clients_dir: Path, fetch: Callable[[str], bytes]) -> tuple[str, str]:
    """Genera clients/<slug>/brand.yaml a partir de una fila aprobada. Devuelve (slug, notas)."""
    values = row_values(page)
    data = brand_data(values)
    slug = values.get("Slug") or slugify(data["name"])
    folder = clients_dir / slug
    if (folder / "brand.yaml").exists():
        raise OnboardingError(f"Ya existe clients/{slug}/brand.yaml; cambia el Slug o borra la carpeta")
    Brand(slug=slug, **data)  # misma validación que usa la parrilla, antes de escribir nada

    notes = ["Conecta sus redes en Postiz y completa postiz_integrations con: social-agent integrations"]
    logos = values.get("Logo") or []
    folder.mkdir(parents=True, exist_ok=True)
    if logos:
        name = logo_name(logos[0])
        # Los archivos subidos a Notion traen un enlace firmado que caduca en una hora.
        (folder / name).write_bytes(fetch(file_url(logos[0])))
        data["logo"] = name
    else:
        notes.insert(0, "Sin logo: súbelo en la fila o ponlo en la carpeta del cliente")

    header = f"# Generado desde el onboarding en Notion ({page.get('url', page['id'])}).\n"
    (folder / "brand.yaml").write_text(
        header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    return slug, ". ".join(notes)


INTAKE_SYSTEM = (
    "Eres estratega de marca. Lee el manual de marca adjunto y extrae solo lo que el documento dice. "
    "Si un dato no aparece, déjalo vacío: no lo deduzcas ni lo inventes. Responde en español."
)
SPANISH_COLOR = {"primary": "primario", "secondary": "secundario", "background": "fondo",
                 "text": "texto", "accent": "acento"}
# Pesos que piden las plantillas (templates/base.html.j2): una fuente sin ellos no carga.
FONT_WEIGHTS = {"heading": "600;700", "body": "400;500"}


def intake_prompt(values: dict, options: dict[str, list[str]]) -> str:
    def allowed(prop: str) -> str:
        return ", ".join(options.get(prop, [])) or "(sin opciones)"

    return (
        f"Marca: {values.get('Marca') or '(sin nombre)'}\n"
        "Extrae del manual de marca adjunto:\n"
        f"- sector: una de [{allowed('Sector')}]\n"
        f"- networks: redes que menciona, de [{allowed('Redes activas')}]\n"
        "- colors: cada color de la paleta con su name y los códigos tal como aparecen en el manual "
        "(hex, rgb como '0, 84, 164', cmyk en porcentaje como '0, 20, 60, 20', pantone). No conviertas "
        "entre sistemas. role: primary (color principal de la marca), secondary, background (fondo de "
        "las piezas), text (texto sobre ese fondo), accent (detalles), o vacío si el manual no lo indica\n"
        "- font_heading, font_body: tipografías de títulos y de texto tal como las nombra el manual. "
        "font_heading_alternative, font_body_alternative: si esas no son de Google Fonts, la que el manual "
        "indique para uso digital o web; si no indica ninguna, la más parecida de Google Fonts. Deben ser "
        "familias de Google Fonts: las de sistema (Arial, Helvetica, Times, Calibri) no sirven\n"
        "- website, instagram: sitio web y usuario de Instagram\n"
        "- pillar_educational (qué enseña la marca), pillar_sales (qué vende y a qué invita), "
        "pillar_connection (historia, equipo, valores, detrás de cámaras)\n"
        f"- objectives: de [{allowed('Objetivos')}]\n"
        "- audience: cliente ideal (edad, ubicación, necesidades)\n"
        "- competitors: competidores o referentes que nombre\n"
        f"- tone: de [{allowed('Tono')}]\n"
        "- business_facts: datos del negocio que sirvan para escribir publicaciones (productos, servicios, "
        "precios, historia, misión, valores, horarios, ubicación), uno por elemento. Excluye "
        "especificaciones de diseño o impresión: tamaños mínimos, márgenes, áreas de seguridad, códigos de "
        "color, tipografías, usos incorrectos del logo, formatos de archivo\n"
        "- notes: lo que no pudiste extraer o aclaraciones para quien revise"
    )


def _numbers(text: str) -> list[float]:
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]


def color_hex(spec: ColorSpec) -> tuple[str | None, str]:
    """(hex, de dónde salió). RGB y CMYK se convierten; Pantone necesita su tabla oficial, así que no."""
    if found := HEX.search(spec.hex):
        value = found.group(0).lower()
        if len(value) == 4:
            value = "#" + "".join(c * 2 for c in value[1:])
        return value, "hex"
    rgb = _numbers(spec.rgb)
    if len(rgb) == 3 and all(0 <= n <= 255 for n in rgb):
        return "#" + "".join(f"{round(n):02x}" for n in rgb), f"RGB {spec.rgb.strip()}"
    cmyk = _numbers(spec.cmyk)
    if len(cmyk) == 4 and all(0 <= n <= 100 for n in cmyk):
        scale = 1 if all(n <= 1 for n in cmyk) and any(0 < n < 1 for n in cmyk) else 100
        c, m, y, k = (n / scale for n in cmyk)
        return "#" + "".join(f"{round(255 * (1 - v) * (1 - k)):02x}" for v in (c, m, y)), f"CMYK {spec.cmyk.strip()}"
    return None, ""


def palette(colors: list[ColorSpec]) -> tuple[str, list[str]]:
    """Texto para la propiedad Colores y notas sobre conversiones y colores sin hex."""
    by_role: dict[str, str] = {}
    unlabeled: list[str] = []
    notes: list[str] = []
    for spec in colors:
        value, origin = color_hex(spec)
        label = spec.name.strip() or spec.role or "color"
        if not value:
            if spec.pantone.strip():
                notes.append(f"{label}: solo viene en Pantone {spec.pantone.strip()}; añade su hex a mano")
            continue
        if origin != "hex":
            notes.append(f"{label} {value} calculado desde {origin} (aproximado en pantalla)")
        role = spec.role.strip().lower()
        if role in SPANISH_COLOR and role not in by_role:
            by_role[role] = value
        else:
            unlabeled.append(value)
    parts = [f"{SPANISH_COLOR[role]} {by_role[role]}" for role in SPANISH_COLOR if role in by_role]
    return ", ".join(parts + unlabeled), notes


def google_font_exists(name: str, weights: str) -> bool:
    """La misma petición que hacen las plantillas: si Google Fonts responde 200, la fuente carga."""
    family = name.strip().replace(" ", "+")
    response = httpx.get(f"https://fonts.googleapis.com/css2?family={family}:wght@{weights}", timeout=20)
    return response.status_code == 200


def choose_font(role: str, candidates: list[str], font_exists: Callable[[str, str], bool]) -> tuple[str, str]:
    """(primera fuente que carga en Google Fonts, nota si se cambió la del manual)."""
    names = list(dict.fromkeys(f for f in (font_family(c) for c in candidates) if f))
    for name in names:
        if font_exists(name, FONT_WEIGHTS[role]):
            replaced = names[:names.index(name)]
            return name, (f"{', '.join(replaced)} no está en Google Fonts con los pesos {FONT_WEIGHTS[role]}: "
                          f"se usa {name}") if replaced else ""
    return "", (f"Ninguna tipografía de {'títulos' if role == 'heading' else 'texto'} del manual "
                f"({', '.join(names)}) está en Google Fonts: elige una a mano") if names else ""


def _pick(found: list[str], allowed: list[str]) -> tuple[list[str], list[str]]:
    """(opciones válidas con su nombre exacto en Notion, valores descartados)."""
    by_lower = {a.lower(): a for a in allowed}
    picked = [by_lower[f.strip().lower()] for f in found if f.strip().lower() in by_lower]
    return list(dict.fromkeys(picked)), [f for f in found if f.strip().lower() not in by_lower]


def intake_properties(intake: BrandIntake, values: dict, options: dict[str, list[str]],
                      font_exists: Callable[[str, str], bool]) -> tuple[dict, list[str], list[str]]:
    """Propiedades a escribir (solo las vacías en la fila), sus nombres y notas para quien revise."""
    discarded: list[str] = []

    def choices(prop: str, found: list[str]) -> list[str]:
        picked, rejected = _pick(found, options.get(prop, []))
        discarded.extend(f"{r} ({prop})" for r in rejected)
        return picked

    colors, notes = palette(intake.colors)
    # Si un rol no tiene alternativa válida, sirve la del otro (muchos manuales dan una sola fuente digital).
    heading, heading_note = choose_font("heading", [intake.font_heading, intake.font_heading_alternative,
                                                    intake.font_body_alternative], font_exists)
    body, body_note = choose_font("body", [intake.font_body, intake.font_body_alternative,
                                           intake.font_heading_alternative], font_exists)
    notes += [n for n in (heading_note, body_note) if n]
    fonts = ", ".join(f"{label}: {name}" for label, name in (("títulos", heading), ("texto", body)) if name)
    website = intake.website.strip()
    if website and not website.startswith("http"):
        website = f"https://{website}"

    text_fields = {
        "Colores": colors,
        "Tipografías": fonts,
        "Instagram": intake.instagram.strip(),
        "Pilar educativo": intake.pillar_educational.strip(),
        "Pilar de venta": intake.pillar_sales.strip(),
        "Pilar de conexión": intake.pillar_connection.strip(),
        "Cliente ideal": intake.audience.strip(),
        "Competidores": "\n".join(c.strip() for c in intake.competitors if c.strip()),
        "Datos del negocio": "\n".join(f.strip() for f in intake.business_facts if f.strip()),
    }
    candidates: dict[str, dict] = {
        name: {"rich_text": _text(value)} for name, value in text_fields.items() if value}
    if website:
        candidates["Sitio web"] = {"url": website}
    if sector := choices("Sector", [intake.sector] if intake.sector else []):
        candidates["Sector"] = {"select": {"name": sector[0]}}
    for prop, found in (("Redes activas", intake.networks), ("Objetivos", intake.objectives), ("Tono", intake.tone)):
        if picked := choices(prop, found):
            candidates[prop] = {"multi_select": [{"name": p} for p in picked]}

    if discarded:
        notes.append("Valores del manual que no están entre las opciones: " + ", ".join(discarded))
    # Nunca se pisa lo que respondió el cliente.
    properties = {name: prop for name, prop in candidates.items() if not values.get(name)}
    return properties, list(properties), notes


def autofill(board: OnboardingBoard, page: dict, llm: LLMProvider, options: dict[str, list[str]],
             fetch: Callable[[str], bytes], font_exists: Callable[[str, str], bool]) -> list[str]:
    """Rellena la fila con el manual de marca y la deja en Por revisar. Devuelve los campos completados."""
    values = row_values(page)
    manuals = values.get("Manual de marca") or []
    pdfs = [m for m in manuals if file_suffix(m) == ".pdf"]
    if not pdfs:
        notes = "Sin manual en PDF: completa la fila a mano" if not manuals else "El manual debe ser un PDF"
        board.update(page["id"], "Por revisar", notes)
        return []

    intake = llm.generate(intake_prompt(values, options), BrandIntake, system=INTAKE_SYSTEM,
                          attachments=[(fetch(file_url(pdfs[0])), "application/pdf")])
    properties, filled, review_notes = intake_properties(intake, values, options, font_exists)
    notes = [n.strip().rstrip(".") for n in [intake.notes, *review_notes] if n.strip()]
    properties["Completado por IA"] = {"rich_text": _text(", ".join(filled) or "Nada: el manual no aportó campos vacíos")}
    board.update(page["id"], "Por revisar", "".join(f"{n}. " for n in notes).strip(), extra=properties)
    return filled


def _download(url: str) -> bytes:
    # Sin las cabeceras de Notion: el enlace del archivo apunta a su almacenamiento, no a la API.
    response = httpx.get(url, timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.content


def run(board: OnboardingBoard, clients_dir: Path, llm_factory: Callable[[], LLMProvider],
        fetch: Callable[[str], bytes] = _download,
        font_exists: Callable[[str, str], bool] = google_font_exists) -> dict[str, int]:
    """Rellena las filas nuevas con su manual y activa las aprobadas. Devuelve cuántas de cada una."""
    with progress.step("preparar"):
        board.ensure_properties()
        new_rows = board.rows("Nuevo")
    reviewed = 0
    if new_rows:
        with progress.step("nuevas", total=len(new_rows)):
            llm, options = llm_factory(), board.options()
            for page in new_rows:
                try:
                    filled = autofill(board, page, llm, options, fetch, font_exists)
                except Exception as exc:
                    log.exception("Falló el relleno con el manual de %s", page["id"])
                    board.update(page["id"], "Error", f"No se pudo leer el manual: {exc}")
                    progress.advance(item=page["id"], status="error", note=str(exc)[:200])
                    continue
                log.info("Onboarding %s: completado por IA %s", page["id"], filled or "nada")
                reviewed += 1
                progress.advance(item=page["id"], note=f"{filled or 'nada'}"[:200])
    else:
        progress.skip("nuevas", "No hay filas nuevas")

    activated = 0
    approved = board.rows("Aprobado")
    if not approved:
        progress.skip("activar", "No hay filas aprobadas")
    with (progress.step("activar", total=len(approved)) if approved else contextlib.nullcontext()):
        for page in approved:
            try:
                slug, notes = activate(page, clients_dir, fetch)
            except OnboardingError as exc:
                log.warning("Onboarding %s: %s", page["id"], exc)
                board.update(page["id"], "Error", str(exc))
                progress.advance(item=page["id"], status="error", note=str(exc)[:200])
                continue
            except Exception as exc:
                log.exception("Falló el onboarding de %s", page["id"])
                board.update(page["id"], "Error", f"Error inesperado: {exc}")
                progress.advance(item=page["id"], status="error", note=str(exc)[:200])
                continue
            board.update(page["id"], "Activo", notes, slug=slug)
            log.info("Cliente activo: clients/%s", slug)
            activated += 1
            progress.advance(item=slug)
    return {"por_revisar": reviewed, "activos": activated}

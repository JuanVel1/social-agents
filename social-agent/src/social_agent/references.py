"""Referencias de Pinterest convertidas en el estilo de las piezas de la marca.

Cada pin del moodboard (o una imagen suelta) es un estilo de diseño: por ejemplo, "Papá Noel escribe por
chat que este año estuvo ocupado". Gemini mira la imagen y escribe una plantilla Jinja que recrea su estilo
(fondo, composición, tipografía, recursos gráficos) con la paleta de la marca en lugar de la del pin, la
imagen de la persona de la marca y huecos de texto propios. La misma plantilla sirve para
post, historia y lámina de carrusel (variable kind). No se reutilizan imágenes, logos ni textos del pin.

Antes de guardarla, el agente la renderiza en los tres formatos con textos de ejemplo, comprueba que nada
se salga, se corte, se tape o pierda contraste, y le pide a Gemini que la compare con el pin; si algo falla,
la rehace una vez con esos problemas. Las aprobadas quedan en clients/<slug>/referencias/<id>/ y la
parrilla las reparte entre todas las piezas con diseño (planner.assign_references).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from pathlib import Path

from PIL import Image

from . import progress
from .llm.base import LLMProvider
from .models import Brand, PinSubject, ReferenceDesign, ReferenceReview

log = logging.getLogger(__name__)

FOLDER = "referencias"
ATTEMPTS = 3
FONT_NAME = re.compile(r"^[A-Z][A-Za-z0-9 ]{1,40}$")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
USED_FIELDS = re.compile(r"\bf\.([a-zA-Z_][a-zA-Z0-9_]*)")
FORBIDDEN = re.compile(r"<script|javascript:|https?://|@import|<iframe|<object|<embed|url\((?!#)", re.I)

DESIGN_SYSTEM = (
    "Eres director de arte y desarrollador front-end. Adaptas ideas de diseño de referencia a la identidad "
    "de una marca concreta y las escribes como plantillas HTML/CSS limpias que se renderizan con Chromium."
)

BASE = "referencia_base.html.j2"
# Formatos en que se usa cada plantilla: el post (y la portada del carrusel), la historia y las láminas
# internas del carrusel.
SIZES = {"post": (1080, 1350), "story": (1080, 1920), "slide": (1080, 1350)}
SIZE = SIZES["post"]
SAMPLE_SLIDE = {"heading": "Automatiza tus respuestas", "body": "Un flujo con IA contesta las preguntas "
                "frecuentes y te deja solo los clientes listos para comprar."}

# Lo que garantiza referencia_base.html.j2 y lo que la plantilla puede usar.
CONTRACT = """\
Contrato de la plantilla (Jinja2, renderizada con Chromium):
- Empieza con {{% extends "referencia_base.html.j2" %}} y define solo {{% block style %}}…{{% endblock %}} (CSS)
  y {{% block content %}}…{{% endblock %}} (HTML). Nada fuera de esos bloques.
- ESTILO DEL PIN: recrea el estilo visual de la referencia: su fondo (degradados, texturas, patrones,
  cuadrículas, una cancha, una mesa, papel…, hechos con CSS y SVG inline: gradients, feTurbulence,
  patrones), su composición, su jerarquía tipográfica, sus recursos gráficos (burbujas de chat, tarjetas de
  app, recortes tipo collage, cintas, etiquetas, marcos, sombras) y su tratamiento. Quien conozca el pin debe
  reconocerlo. El base pinta el lienzo en blanco y nada más: el fondo lo pintas tú, a página completa.
- PALETA DE LA MARCA: cambia los colores de la referencia por los de la marca, por papel: {colors}. Están como
  variables CSS (var(--primary), var(--secondary), var(--accent), var(--background), var(--text)…). Puedes
  usar sus tonos más claros u oscuros (color-mix(in srgb, var(--primary) 30%, white)), blanco, negro y grises.
  Ningún color dominante de la referencia que no sea de la marca (ni el rojo, ni el verde, ni el rosa…).
- Tipografía: var(--display) (titulares condensados de la marca), var(--body), var(--hand) (manuscrita). Si
  el pin usa una tipografía de carácter distinto (serif elegante, redondeada, script…), pon en extra_font el
  nombre exacto de una familia de Google Fonts parecida y úsala como var(--extra).
- Firma: el base pone .handle arriba a la derecha; dale un color que contraste con tu fondo y no pongas nada
  debajo de ella (franja superior derecha de 120 px).
- TRES FORMATOS con la misma plantilla, según la variable kind:
  * kind == "post" (1080x1350) y kind == "story" (1080x1920): la composición principal con tus campos
    ({{{{ f.nombre }}}}) y la imagen de la marca. Usa height (1350 o 1920) para repartir el espacio: en story
    deja libre una franja de 260 px sobre el borde inferior (ahí va un sticker interactivo).
  * kind == "slide" (1080x1350): lámina interna de un carrusel con el mismo estilo (fondo, recursos, paleta),
    sin la imagen de la marca y con estas variables: {{{{ slide.heading }}}} (máx. 60 caracteres),
    {{{{ slide.body }}}} (máx. 260), {{{{ index }}}} y {{{{ total }}}} (número de lámina) y last (true en la última).
  Escribe {{% if kind == "slide" %}}…{{% else %}}…{{% endif %}} donde cambie el contenido.
- Textos: solo los de tus campos (f.*) y los de slide. Define cada campo en fields (name en minúsculas con
  guiones bajos, max_chars realista para que quepa) y un ejemplo en example con textos de la marca. Sin texto
  fijo, salvo microtextos genéricos de interfaz ("En línea", "Enviar", una hora). Cada campo se muestra
  siempre (ningún hueco vacío). No inventes teléfonos, cifras ni datos en example: si el pin muestra un
  contacto, usa el sitio web o la firma de la marca.
- Imagen de la marca (post y story): si photo es true, ponla una sola vez como
  {{% if photo %}}<img class="photo" src="{{{{ photo }}}}" alt="">{{% endif %}}: un recorte PNG transparente de
  {subject}. Elige en photo_file el file exacto de la pose del catálogo de poses (al final) que mejor haga el
  papel del sujeto principal de la referencia. Si el sujeto del pin es un animal, un objeto o un recorte
  (labios, un balón), puedes dibujarlo con SVG/CSS o reemplazarlo por la persona. La imagen ocupa el lugar y
  el tamaño aproximado del sujeto del pin; .photo tiene position:absolute y bottom:0: fija su height (600-1000
  px en post, hasta 1300 en story) y left/right. Nada tapa su cabeza ni su cara. Las poses pueden venir
  cortadas por abajo o por un lado: apóyalas en el borde inferior y, si el corte es lateral, pégalas a ese borde.
- Contraste: cada texto con un color explícito que contraste con lo que tiene detrás.
- Prohibido: <script>, <img> que no sea la de la marca, URLs, @import, logos o nombres de otras marcas,
  textos o cifras copiados de la referencia.
- Todo dentro del lienzo con márgenes de al menos 60 px; textos de 28 px o más. Cintas, franjas o textos
  decorativos que crucen el lienzo a propósito llevan class="deco" (pueden salirse).
"""

EXAMPLE_TEMPLATE = """\
{% extends "referencia_base.html.j2" %}
{% block style %}
  body { background: linear-gradient(160deg, var(--secondary), color-mix(in srgb, var(--secondary) 60%, black)); }
  .handle { color: #fff; }
  .main { position: absolute; top: 170px; left: 80px; right: 80px; display: grid; gap: 36px; }
  h1 { font: 400 {{ 110 if kind != 'slide' else 84 }}px/1 var(--display); color: #fff; text-transform: uppercase; }
  .tag { justify-self: start; background: var(--accent); color: #0f172a; font: 700 34px var(--body); padding: 10px 28px; border-radius: 40px; }
  .body { font: 400 40px/1.4 var(--body); color: #e6f4fb; }
  .photo { height: {{ 820 if kind == 'post' else 1150 }}px; right: 0; }
{% endblock %}
{% block content %}
  <div class="main">
  {% if kind == "slide" %}
    <span class="tag">{{ index }} / {{ total }}</span>
    <h1>{{ slide.heading }}</h1>
    <p class="body">{{ slide.body }}</p>
  {% else %}
    <span class="tag">{{ f.etiqueta }}</span>
    <h1>{{ f.titulo }}</h1>
  {% endif %}
  </div>
  {% if kind != "slide" and photo %}<img class="photo" src="{{ photo }}" alt="">{% endif %}
{% endblock %}"""

# Textos que se salen del lienzo, se desbordan de su caja o no se leen sobre su fondo. Lo decorativo
# (class="deco") puede cruzar el borde.
OVERFLOW_JS = """() => {
  const W = innerWidth, H = innerHeight, bad = [];
  // Píxeles visibles (no transparentes) de la imagen de la marca, para saber si tapa algún texto.
  const photo = document.querySelector('img.photo');
  let covered = () => 0;
  if (photo && photo.complete && photo.naturalWidth) {
    const pr = photo.getBoundingClientRect(), cv = document.createElement('canvas');
    cv.width = Math.max(1, Math.round(pr.width)); cv.height = Math.max(1, Math.round(pr.height));
    const ctx = cv.getContext('2d'); ctx.drawImage(photo, 0, 0, cv.width, cv.height);
    const alpha = ctx.getImageData(0, 0, cv.width, cv.height).data;
    // Un texto se ve mal si la imagen queda encima de él, o si está suelto (sin panel propio) sobre ella.
    covered = (r, el) => {
      const panel = (() => { for (let e = el; e && e !== document.body; e = e.parentElement) {
        const c = getComputedStyle(e).backgroundColor.match(/[\\d.]+/g) || [];
        if (c.length >= 3 && (c.length < 4 || Number(c[3]) > .5)) return true; } return false; })();
      let hit = 0, n = 0;
      for (let x = r.left + 2; x < r.right - 2; x += Math.max(3, r.width / 24))
        for (let y = r.top + 2; y < r.bottom - 2; y += Math.max(3, r.height / 8)) {
          n++;
          const px = Math.floor(x - pr.left), py = Math.floor(y - pr.top);
          if (px < 0 || py < 0 || px >= cv.width || py >= cv.height || alpha[(py * cv.width + px) * 4 + 3] <= 128) continue;
          if (document.elementFromPoint(x, y) === photo || !panel) hit++;
        }
      return n ? hit / n : 0;
    };
  }
  const rgba = c => (c.match(/[\\d.]+/g) || []).map(Number);
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
                               return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  const background = el => {
    for (let e = el; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.backgroundImage !== 'none') return null;  // degradado o imagen: no se puede medir
      const c = rgba(s.backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > .5)) return c;
    }
    return null;
  };
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('svg') || el.classList.contains('photo')) continue;
    const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
    if (!text) continue;
    const label = '"' + text.slice(0, 40) + '"';
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (!el.closest('.deco')) {
      if (r.width && (r.left < -2 || r.top < -2 || r.right > W + 2 || r.bottom > H + 2)) bad.push('fuera del lienzo: ' + label);
      if ((el.scrollWidth > el.clientWidth + 4 || el.scrollHeight > el.clientHeight + 4)
          && (s.overflow !== 'visible' || s.textOverflow === 'ellipsis')) bad.push('texto cortado: ' + label);
      if (covered(r, el) > .12) bad.push('la imagen de la marca se cruza con el texto ' + label + ' (sepáralos)');
      const sign = document.querySelector('.handle');
      if (sign && el !== sign && !sign.contains(el)) {
        const h = sign.getBoundingClientRect();
        if (r.left < h.right && r.right > h.left && r.top < h.bottom && r.bottom > h.top)
          bad.push('el texto ' + label + ' se cruza con la firma (deja libre la esquina superior derecha)');
      }
    }
    const bg = background(el), fg = rgba(s.color);
    if (bg && fg.length >= 3 && s.visibility !== 'hidden' && Number(s.opacity) > .2) {
      const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
      if ((a + .05) / (b + .05) < 2.5) bad.push('texto ilegible (color parecido al fondo de su panel): ' + label);
    }
  }
  return bad;
}"""


def references_dir(clients_dir: Path, slug: str) -> Path:
    return clients_dir / slug / FOLDER


def local_id(data: bytes) -> str:
    return "local-" + hashlib.sha1(data).hexdigest()[:10]


def load_references(clients_dir: Path, slug: str) -> list[dict]:
    """Referencias aprobadas: [{id, url, title, design: ReferenceDesign}]."""
    folder = references_dir(clients_dir, slug)
    found = []
    for path in sorted(folder.glob("*/design.json")) if folder.exists() else []:
        data = json.loads(path.read_text(encoding="utf-8"))
        template_ok = bool(data.get("ok")) and (path.parent / "template.html.j2").exists()
        if template_ok or data.get("design", {}).get("image_prompt"):
            found.append({"id": path.parent.name, "url": data.get("url", ""), "title": data.get("title", ""),
                          "template_ok": template_ok, "design": ReferenceDesign.model_validate(data["design"])})
    return found


def field_values(design: ReferenceDesign, values: list) -> dict[str, str]:
    """{campo: texto} con todos los campos de la plantilla, recortados a su max_chars."""
    given = {v.name: v.value for v in values}
    return {f.name: _cut(given.get(f.name, ""), f.max_chars) for f in design.fields}


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" .,;:") + "…"


IMPORT = re.compile(r"@import\s+url\([^)]*\)\s*;?|@import\s+['\"][^'\"]*['\"]\s*;?", re.I)
GOOGLE_FAMILY = re.compile(r"family=([A-Za-z0-9+ ]+)")


def tidy(design: ReferenceDesign) -> ReferenceDesign:
    """Arregla lo que el modelo suele hacer mal y tiene arreglo seguro, en vez de rechazar la plantilla.

    Quita los @import (las fuentes las carga la base) y, si importaba una de Google Fonts y no dio
    extra_font, la usa como extra_font. extra_font queda solo con el nombre de la familia, sin pesos.
    """
    template, extra = design.template, design.extra_font
    for match in IMPORT.findall(template):
        family = GOOGLE_FAMILY.search(match)
        if family and not extra:
            extra = family.group(1).replace("+", " ")
    template = IMPORT.sub("", template)
    # La herencia tiene una sola forma válida: si el modelo apuntó a otra base o la olvidó, se corrige.
    extends = f'{{% extends "{BASE}" %}}'
    if re.search(r"\{%-?\s*extends\s", template):
        template = re.sub(r"\{%-?\s*extends\s+[^%]*%\}", lambda _: extends, template, count=1)
    elif "{% block" in template:
        template = extends + "\n" + template
    extra = re.split(r"[:&@,;]", extra or "")[0].replace("+", " ").strip().strip("'\"")
    return design.model_copy(update={"template": template, "extra_font": extra})


def check_template(design: ReferenceDesign) -> list[str]:
    """Problemas de forma de la plantilla, antes de renderizarla."""
    problems = []
    template = design.template
    if f'{{% extends "{BASE}" %}}' not in template:
        problems.append(f'no empieza con {{% extends "{BASE}" %}}')
    if FORBIDDEN.search(template):
        problems.append(f"contiene algo prohibido: {FORBIDDEN.search(template).group(0)}")
    names = {f.name for f in design.fields}
    if bad := [f.name for f in design.fields if not FIELD_NAME.match(f.name)]:
        problems.append(f"nombres de campo inválidos: {bad}")
    if undeclared := sorted(set(USED_FIELDS.findall(template)) - names):
        problems.append(f"usa campos que no declaró en fields: {undeclared}")
    if not design.fields:
        problems.append("no tiene campos de texto")
    if design.photo and "{{ photo }}" not in template.replace("{{photo}}", "{{ photo }}"):
        problems.append("photo es true pero la plantilla no usa {{ photo }}")
    if design.extra_font and not FONT_NAME.match(design.extra_font):
        problems.append(f"extra_font no parece un nombre de Google Fonts: {design.extra_font!r}")
    return problems


def _jpeg(data: bytes, size: int = 768) -> bytes:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((size, size))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def contact_sheet(images: dict[str, bytes], height: int = 760) -> bytes:
    """Los formatos renderizados uno al lado del otro (post, historia, lámina), para revisarlos juntos."""
    opened = [Image.open(io.BytesIO(data)).convert("RGB") for data in images.values()]
    for image in opened:
        image.thumbnail((10_000, height))
    sheet = Image.new("RGB", (sum(i.width for i in opened) + 20 * (len(opened) - 1), height), "white")
    x = 0
    for image in opened:
        sheet.paste(image, (x, 0))
        x += image.width + 20
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def design_prompt(brand: Brand, title: str, subject: str, poses: list[dict], feedback: str = "") -> str:
    contract = CONTRACT.format(colors=json.dumps(brand.colors, ensure_ascii=False), subject=subject)
    catalog = [{"file": p["file"], "description": p.get("description", ""), "framing": p.get("framing", "")}
               for p in poses]
    return (
        f"Marca: {brand.name} ({brand.sector}). Tono: {brand.tone}. Audiencia: {brand.audience}.\n"
        + f"La imagen adjunta es un pin de Pinterest que la marca guardó como referencia ({title or 'sin título'}).\n"
        "Adapta ESTE estilo a la marca: misma estética y composición que el pin, con la paleta, los textos y la "
        "persona de la marca.\n"
        "1. concept: la idea creativa del pin en una frase (qué la hace funcionar), sin nombrar a su autor.\n"
        "2. adaptation: cómo se lleva a esta marca y a sus temas (marketing digital, IA, cursos, servicios).\n"
        "3. season: la temporada o fecha a la que está atada la idea (navidad, mundial, año nuevo…) o vacío. "
        "Si es de temporada, conserva sus elementos (adornos navideños, balón, confeti) dibujados con SVG/CSS "
        "y en los colores de la marca.\n"
        "4. photo, photo_hint, photo_file, extra_font, fields, example y template según este contrato.\n"
        f"{contract}\n"
        f"Ejemplo de plantilla válida (otro estilo):\n{EXAMPLE_TEMPLATE}\n"
        f"<poses>{json.dumps(catalog, ensure_ascii=False)}</poses>\n"
        + (f"\nTu intento anterior fue rechazado; la segunda imagen adjunta muestra cómo se vio (post, historia y "
           f"lámina). Corrige estos problemas sin romper lo que estaba bien:\n{feedback}\n" if feedback else "")
    )


def review_design(llm: LLMProvider, brand: Brand, reference: bytes, sheet: bytes, design: ReferenceDesign) -> ReferenceReview:
    return llm.generate(
        f"La primera imagen es un pin de referencia. La segunda muestra su adaptación a la marca {brand.name}, de "
        f"izquierda a derecha: post, historia y, si la hay, lámina interna de carrusel (idea: {design.concept}).\n"
        f"Colores de la marca: {json.dumps(brand.colors, ensure_ascii=False)}.\n"
        "ok = quien conozca el pin reconoce su estilo (composición, jerarquía, tipo de fondo y recursos "
        "gráficos), con la paleta de la marca OBLIGATORIAMENTE en lugar de la del pin (no pidas los colores del "
        "pin); la persona de la marca, si "
        "aparece, hace el papel del sujeto del pin; se ve profesional; todo el texto se lee completo (nada "
        "cortado, tapado ni encimado, buen contraste); no hay elementos rotos, huecos vacíos ni grandes zonas "
        "sin nada; no copia logos, marcas ni textos del pin y nada tapa la cara de la persona. No es un defecto: "
        "que la persona sea una ilustración, el borde recto donde viene cortada si toca el borde del lienzo, un "
        "ángulo de cámara distinto si la idea se reconoce, que la lámina no lleve a la persona, que la tipografía "
        "sea parecida y no idéntica, ni que falten elementos fotográficos del pin que no se pueden dibujar "
        "(labios, animales, objetos reales) si su papel lo cumple un recurso gráfico o la persona. Si no está ok, "
        "explica en issues qué corregir en la plantilla y en qué formato, de forma concreta.",
        ReferenceReview, tier="quality",
        attachments=[(_jpeg(reference), "image/jpeg"), (_jpeg(sheet, 1600), "image/jpeg")],
    )


def supports_slide(design: ReferenceDesign) -> bool:
    """Si la plantilla trae el formato de lámina interna; si no, esas láminas salen con el estilo estándar."""
    return "slide.heading" in design.template and "slide.body" in design.template


def kinds_for(design: ReferenceDesign) -> list[str]:
    return [k for k in SIZES if k != "slide" or supports_slide(design)]


def sample_context(design: ReferenceDesign, kind: str) -> dict:
    """Lo que recibe la plantilla en cada formato al probarla: los textos de ejemplo y una lámina 2 de 6."""
    return {"kind": kind, "f": field_values(design, design.example), "slide": SAMPLE_SLIDE, "index": 2,
            "total": 6, "first": False, "last": False}


REAL_SUBJECT = ("la persona de la marca en una fotografía real (no una ilustración); el pin muestra la foto de "
                "una persona y ella ocupa su lugar")


def detect_real_subject(llm: LLMProvider, image: bytes, real_photos: list[dict]) -> dict | None:
    """La foto real del banco que reemplaza a la persona fotografiada del pin, o None si el pin no trae una.

    Solo elige entre fotos que ya existen: la imagen de la persona nunca se genera ni se retoca.
    """
    if not real_photos:
        return None
    catalog = [{"file": p["file"], "description": p.get("description", ""), "framing": p.get("framing", "")}
               for p in real_photos]
    result = llm.generate(
        "La imagen adjunta es un pin de Pinterest que una marca guardó como referencia de diseño.\n"
        "realistic_person = true solo si su sujeto principal es la FOTOGRAFÍA de una persona real (retrato, "
        "medio cuerpo o cuerpo entero). Es false si la persona es una ilustración, caricatura, dibujo, render "
        "3D o avatar, si solo se ven manos u otra parte suelta, o si el sujeto es un animal, un objeto o texto.\n"
        "pose: el gesto, la postura y el encuadre de esa persona, en una frase.\n"
        "photo_file: si realistic_person es true, el file exacto del catálogo cuya foto puede ocupar el lugar "
        "de esa persona: primero el mismo encuadre (cara, medio cuerpo, cuerpo entero), después el gesto más "
        "parecido. Si es false, vacío.\n"
        f"<photos>{json.dumps(catalog, ensure_ascii=False)}</photos>",
        PinSubject, tier="bulk", attachments=[(_jpeg(image, 1024), "image/jpeg")],
    )
    if not result.realistic_person:
        return None
    by_file = {p["file"]: p for p in real_photos}
    if result.photo_file in by_file:
        return by_file[result.photo_file]
    # El modelo no devolvió un file del catálogo: la del encuadre que describió o la primera útil.
    pose = result.pose.lower()
    same = [p for p in real_photos if p.get("framing") and p["framing"].lower() in pose]
    return (same or real_photos)[0]


def build_reference(folder: Path, brand: Brand, image: bytes, llm: LLMProvider, renderer, *, title: str = "",
                    url: str = "", poses: list[dict] = (), subject: str = "la persona de la marca",
                    real_photo: bool = False) -> dict:
    """Diseña, prueba en los tres formatos y guarda la plantilla de una referencia (design.json)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reference.jpg").write_bytes(_jpeg(image, 1080))
    feedback, record, last_sheet = "", {}, b""
    for attempt in range(1, ATTEMPTS + 1):
        progress.detail(f"{folder.name}: intento {attempt} de {ATTEMPTS}")
        progress.activity("Diseñando la plantilla")
        # En los reintentos, el modelo también ve cómo quedó su intento anterior, no solo la lista de problemas.
        attachments = [(_jpeg(image, 1024), "image/jpeg")]
        if last_sheet:
            attachments.append((_jpeg(last_sheet, 1600), "image/jpeg"))
        design = llm.generate(design_prompt(brand, title, subject, list(poses), feedback), ReferenceDesign,
                              tier="quality", system=DESIGN_SYSTEM, attachments=attachments)
        design = tidy(design)
        problems = check_template(design)
        files = [p["file"] for p in poses]
        if design.photo and files and design.photo_file not in files:
            problems.append(f"photo_file {design.photo_file!r} no es un file del catálogo de poses")
        renders: dict[str, bytes] = {}
        if not problems:
            (folder / "template.html.j2").write_text(design.template, encoding="utf-8")
            for kind in kinds_for(design):
                try:
                    png, issues = renderer(f"{folder.name}/template.html.j2", design, sample_context(design, kind),
                                           design.photo_file or None)
                except Exception as exc:  # Jinja o Chromium: se le devuelve el error al modelo
                    problems.append(f"[{kind}] no se pudo renderizar: {exc}")
                    continue
                renders[kind] = png
                problems += [f"[{kind}] {issue}" for issue in issues]
        for kind, png in renders.items():
            (folder / ("preview.png" if kind == "post" else f"preview-{kind}.png")).write_bytes(png)
        if renders:
            last_sheet = contact_sheet(renders)
        if renders and len(renders) == len(kinds_for(design)) and not problems:
            progress.activity("Revisión visual de la plantilla")
            verdict = review_design(llm, brand, image, last_sheet, design)
            if not verdict.ok:
                problems.append(verdict.issues or "la revisión no la aprobó")
        record = {"ok": not problems, "title": title, "url": url, "attempts": attempt,
                  "issues": "; ".join(problems), "real_photo": real_photo, "design": design.model_dump()}
        (folder / "design.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        if not problems:
            log.info("Referencia %s lista: %s", folder.name, design.concept)
            return record
        log.info("Referencia %s, intento %s rechazado: %s", folder.name, attempt, record["issues"])
        feedback = record["issues"]
    return record


def needs_design(target: Path, force: bool = False, retry: bool = False) -> bool:
    """Si la referencia hay que diseñarla: nueva, forzada o descartada antes y se pide reintentar."""
    path = target / "design.json"
    if force or not path.exists():
        return True
    return retry and not json.loads(path.read_text(encoding="utf-8")).get("ok")


def real_subjects(clients_dir: Path, brand: Brand, llm: LLMProvider, items: list[dict],
                  real_photos: list[dict]) -> dict[str, dict]:
    """Paso opcional (style.real_photo_references): {id del pin: foto real} para los pines que muestran la
    fotografía de una persona. Los demás eligen la pose del catálogo."""
    if not (brand.style.real_photo_references or brand.style.ai_images):
        progress.skip("personas", "Opción desactivada (style.real_photo_references)")
        return {}
    if not real_photos:
        progress.skip("personas", "No hay fotos reales en el banco")
        log.warning("%s tiene real_photo_references pero su banco no tiene fotos reales útiles: "
                    "social-agent fotos --client %s --importar", brand.slug, brand.slug)
        return {}
    if not items:
        progress.skip("personas", "No hay referencias nuevas")
        return {}
    from .photos import bank_dir

    chosen: dict[str, dict] = {}
    with progress.step("personas", total=len(items)):
        for item in items:
            progress.activity(f"Mirando el pin {item['id']}")
            try:
                photo = detect_real_subject(llm, item["image"], real_photos)
            except Exception as exc:  # sin respuesta del modelo, la plantilla elige la pose del catálogo
                log.warning("No se pudo revisar si el pin %s trae una persona real: %s", item["id"], exc)
                progress.advance(item=item["id"], status="error", note=str(exc)[:200])
                continue
            if photo:
                chosen[item["id"]] = photo
                progress.output(bank_dir(clients_dir, brand.slug) / photo["file"])
                log.info("El pin %s trae una persona real: lleva la foto %s", item["id"], photo["file"])
            progress.advance(item=item["id"], status="done" if photo else "skipped",
                             note=f"foto real: {photo['file']}" if photo else "sin persona real")
            progress.metric("Con persona real", len(chosen))
    return chosen


def refresh_references(clients_dir: Path, brand: Brand, llm: LLMProvider, items: list[dict], renderer, *,
                       force: bool = False, retry: bool = False, real_photos: list[dict] = (), **build) -> dict:
    """Crea la plantilla de cada referencia nueva. items: [{id, image (bytes), title, url}]."""
    folder = references_dir(clients_dir, brand.slug)
    counts = {"nuevas": 0, "descartadas": 0, "existentes": 0}
    real = real_subjects(clients_dir, brand, llm,
                         [i for i in items if needs_design(folder / i["id"], force, retry)], list(real_photos))
    with progress.step("plantillas", total=len(items)):
        for item in items:
            target = folder / item["id"]
            if not needs_design(target, force, retry):
                counts["existentes"] += 1
                progress.advance(item=item["id"], status="skipped", note="ya existía")
                continue
            item_build = build
            if photo := real.get(item["id"]):
                # La elegida va primero; el resto de fotos reales quedan como alternativa del mismo tipo.
                item_build = {**build, "poses": [photo] + [p for p in real_photos if p["file"] != photo["file"]],
                              "subject": REAL_SUBJECT, "real_photo": True}
            try:
                record = build_reference(target, brand, item["image"], llm, renderer,
                                         title=item.get("title", ""), url=item.get("url", ""), **item_build)
            except Exception as exc:
                log.exception("No se pudo adaptar la referencia %s", item["id"])
                counts["descartadas"] += 1
                progress.advance(item=item["id"], status="error", note=str(exc)[:200])
                continue
            if photo and brand.style.ai_images and record.get("design", {}).get("fields"):
                record = add_image_prompt(target, brand, llm, item["image"], record)
            usable = record["ok"] or bool(record["design"].get("image_prompt"))
            counts["nuevas" if usable else "descartadas"] += 1
            if (target / "preview.png").exists():
                progress.output(target / "preview.png")
            prompt_note = " · con prompt de imagen" if record["design"].get("image_prompt") else ""
            progress.advance(item=item["id"], status="done" if usable else "error",
                             note=((record.get("issues") or "")[:200] if not record["ok"] else
                                   f"{record['attempts']} intento(s)"
                                   + (" · foto real" if record.get("real_photo") else "")) + prompt_note)
            for key, n in counts.items():
                progress.metric(key.capitalize(), n)
    return counts


def add_image_prompt(folder: Path, brand: Brand, llm: LLMProvider, image: bytes, record: dict) -> dict:
    """Escribe el prompt detallado de la referencia (covers.py) y lo guarda en su design.json."""
    from .covers import write_image_prompt

    progress.activity("Escribiendo el prompt de imagen")
    try:
        prompt = write_image_prompt(llm, brand, image, ReferenceDesign.model_validate(record["design"]))
    except Exception as exc:  # la referencia sigue sirviendo con su plantilla
        log.warning("No se pudo escribir el prompt de imagen de %s: %s", folder.name, exc)
        return record
    if prompt:
        record["design"]["image_prompt"] = prompt
        (folder / "design.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("Prompt de imagen de %s: %s", folder.name, prompt)
    return record


def apply_reference_photos(pieces: list, references: list[dict], bank: Path, photos: list[dict] = ()) -> int:
    """Ajusta la pose de las piezas que adaptan una referencia a la plantilla.

    Cada pieza conserva la pose que le eligió assign_photos si tiene el mismo encuadre (medio cuerpo, cuerpo
    entero) que la pose para la que se dimensionó la plantilla; si no, o si no tiene pose, lleva esa pose.
    Así varias piezas con la misma referencia no repiten imagen. Devuelve cuántas cambió.
    """
    by_id = {r["id"]: r["design"] for r in references}
    framing = {p["file"]: p.get("framing", "") for p in photos}
    changed = 0
    for piece in pieces:
        design = by_id.get(piece.reference)
        if not (design and design.photo and design.photo_file and (bank / design.photo_file).exists()):
            continue
        wanted = framing.get(design.photo_file, "")
        if piece.photo and wanted and framing.get(piece.photo) == wanted:
            continue
        if piece.photo != design.photo_file:
            piece.photo = design.photo_file
            changed += 1
    return changed


@progress.tracked("revalidar")
def revalidate(clients_dir: Path, brand: Brand, renderer) -> list[str]:
    """Vuelve a pasar las comprobaciones automáticas a las plantillas aprobadas (sin el modelo).

    Las que ahora fallan quedan descartadas con el motivo, para que --reintentar-descartadas las rehaga.
    """
    failed = []
    for ref in load_references(clients_dir, brand.slug):
        design = ref["design"]
        problems = check_template(design)
        for kind in kinds_for(design) if not problems else ():
            _, issues = renderer(f"{ref['id']}/template.html.j2", design, sample_context(design, kind),
                                 design.photo_file or None)
            problems += [f"[{kind}] {issue}" for issue in issues]
        if problems:
            path = references_dir(clients_dir, brand.slug) / ref["id"] / "design.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record.update(ok=False, issues="; ".join(problems))
            path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            failed.append(f"{ref['id']}: {record['issues']}")
    return failed

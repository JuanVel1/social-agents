"""Imágenes completas generadas con IA a partir de una referencia de Pinterest (style.ai_images).

Cada referencia cuyo pin muestra la fotografía de una persona guarda un prompt detallado (design.json →
image_prompt): la persona de la marca con la ropa, los objetos, la pose y la composición del pin, y un hueco
[[campo]] por cada texto. Al armar la parrilla, cada pieza rellena esos huecos con sus textos y el modelo de
imágenes genera la publicación entera, con el pin y las fotos reales de la persona como referencia. Un modelo
revisa que los textos estén bien escritos; si la imagen no pasa o no hay cuota, la pieza sale con su plantilla.

El prompt de cada referencia se puede retocar a mano en su design.json.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from pathlib import Path

from PIL import Image, ImageOps

from . import progress
from .llm.base import LLMProvider, NoQuotaError
from .models import Brand, ImagePrompt, MonthPlan, Piece, ReferenceDesign, ReferenceReview

log = logging.getLogger(__name__)

ATTEMPTS = 2
IDENTITY_PHOTOS = 3
PLACEHOLDER = re.compile(r"\[\[([a-zA-Z_][a-zA-Z0-9_]*)\]\]")
# Formato de cada tipo de pieza: tamaño final y proporción que se le pide al modelo de imágenes.
FORMATS = {"post": ((1080, 1350), "4:5"), "carousel": ((1080, 1350), "4:5"), "story": ((1080, 1920), "9:16")}

PROMPT_SYSTEM = (
    "Eres director de arte y escribes prompts para un modelo generador de imágenes fotorrealistas. Tus prompts "
    "son precisos y visuales: describen lo que se ve, no lo que se quiere transmitir."
)

EXAMPLE = (
    "Photorealistic magazine cover, featuring the exact face and precise facial features of the specific man "
    "from the identity reference photos (clean-cut fade haircut, exact facial structure, nose shape, short "
    "trimmed dark beard), wearing round black-rimmed glasses, a beige corduroy cap with a blue brim, a brown "
    "corduroy blazer over a white dress shirt and dark tie. He is holding an orange ceramic mug and drinking "
    "from it, looking directly at the camera with a confident expression, perfectly matching the exact "
    "composition, pose and layout of the layout reference image. The background features large bold orange "
    'typography that reads "[[titulo]]", subheadings "[[subtitulo_1]]" and "[[subtitulo_2]]", and a faint '
    'background watermark repeating "EMPRENDE CON VIC". In the foreground, a yellow sticky note on the lapel '
    'reads "[[nota]]", and a red binder clip holds documents at the bottom with the text "emprendeconvic.com". '
    "Professional editorial lighting, high-end corporate magazine aesthetic, 8k resolution."
)


def write_image_prompt(llm: LLMProvider, brand: Brand, pin: bytes, design: ReferenceDesign) -> str:
    """El prompt detallado de una referencia, con un hueco [[campo]] por cada texto. Vacío si no salió válido."""
    fields = [{"name": f.name, "description": f.description, "max_chars": f.max_chars} for f in design.fields]
    names = {f.name for f in design.fields}
    fixed = [t for t in (brand.name, brand.handle, (brand.website or "").removeprefix("https://")) if t]
    feedback = ""
    for _ in range(2):
        result = llm.generate(
            f"Marca: {brand.name} ({brand.sector}).\n"
            "La imagen adjunta es un pin de Pinterest que la marca quiere recrear con su propia persona. Escribe "
            "en inglés el prompt para un generador de imágenes que produzca ESA MISMA imagen con la persona de la "
            "marca en lugar de la del pin. El generador recibirá el pin (layout reference image) y fotos reales "
            "de la persona (identity reference photos).\n"
            "El prompt, en un solo párrafo, describe en este orden:\n"
            "1. Tipo de imagen y estilo (portada editorial, anuncio, retrato de estudio…), fotorrealista.\n"
            "2. La persona: 'the exact face and precise facial features of the specific man from the identity "
            f"reference photos' y, entre paréntesis, sus rasgos fijos: {brand.style.person or 'los que se ven en sus fotos'}. "
            "No describas la cara de la persona del pin.\n"
            "3. Ropa y accesorios tal como están en el pin, prenda por prenda, con material y color.\n"
            "4. Pose, gesto, mirada, objetos que sostiene y encuadre, y la frase 'perfectly matching the exact "
            "composition, pose and layout of the layout reference image'.\n"
            "5. Fondo y tipografía: dónde va cada texto, su tamaño, peso y color. Cada texto variable va entre "
            "comillas dobles como hueco [[name]], con el name exacto de esta lista de campos (usa todos, una "
            f"vez cada uno): {json.dumps(fields, ensure_ascii=False)}\n"
            "6. Objetos del primer plano con texto (notas, tazas, etiquetas, papeles).\n"
            "7. Luz y acabado (editorial lighting, 8k…).\n"
            "Textos fijos: ningún nombre, usuario, logo o marca del pin. Donde el pin muestre su marca, su "
            f"usuario o su web, usa los de esta marca, escritos tal cual: {json.dumps(fixed, ensure_ascii=False)}. "
            "No inventes otros textos, cifras ni teléfonos.\n"
            f"Ejemplo de prompt bien escrito (otro pin):\n{EXAMPLE}\n"
            + (f"\nTu intento anterior no sirvió: {feedback}\n" if feedback else ""),
            ImagePrompt, tier="quality", system=PROMPT_SYSTEM, attachments=[(_jpeg(pin), "image/jpeg")],
        )
        prompt = " ".join(result.prompt.split())
        used = set(PLACEHOLDER.findall(prompt))
        if prompt and used and used <= names:
            return prompt
        feedback = (f"usa huecos que no existen: {sorted(used - names)}" if used - names
                    else "no tiene ningún hueco [[name]] para los textos")
    log.warning("No se pudo escribir un prompt de imagen válido (%s)", feedback)
    return ""


def fill_prompt(design: ReferenceDesign, piece: Piece) -> str | None:
    """El prompt de la referencia con los textos de la pieza. None si a la pieza le falta alguno."""
    from .references import field_values

    values = field_values(design, piece.content.fields if piece.content else [])
    used = PLACEHOLDER.findall(design.image_prompt)
    if any(not values.get(name, "").strip() for name in used):
        return None
    # Las comillas dobles delimitan cada texto en el prompt: dentro del texto pasan a simples.
    return PLACEHOLDER.sub(lambda m: values[m.group(1)].replace('"', "'"), design.image_prompt)


def texts_of(design: ReferenceDesign, piece: Piece) -> list[str]:
    from .references import field_values

    values = field_values(design, piece.content.fields if piece.content else [])
    return [values[name] for name in dict.fromkeys(PLACEHOLDER.findall(design.image_prompt)) if values.get(name)]


def _jpeg(data: bytes, side: int = 1024) -> bytes:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((side, side))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _identity(photo: Path) -> bytes:
    """La foto de la persona sobre gris claro (los recortes del banco son PNG transparentes)."""
    cut = Image.open(photo).convert("RGBA")
    cut.thumbnail((1024, 1024))
    canvas = Image.new("RGB", cut.size, (225, 225, 225))
    canvas.paste(cut, mask=cut)
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def identity_photos(photos: list[dict], preferred: str = "") -> list[dict]:
    """Las fotos reales que fijan la identidad: la que eligió la referencia y, después, las de cara más cerca."""
    order = {"cara": 0, "medio cuerpo": 1, "cuerpo entero": 2}
    ranked = sorted(photos, key=lambda p: (p["file"] != preferred, order.get(p.get("framing", ""), 3), p["file"]))
    return ranked[:IDENTITY_PHOTOS]


def review_cover(llm: LLMProvider, image: bytes, texts: list[str]) -> ReferenceReview:
    return llm.generate(
        "Revisa esta imagen generada para una publicación de redes sociales.\n"
        f"Debe mostrar estos textos, escritos exactamente así (mismas letras y tildes): {json.dumps(texts, ensure_ascii=False)}\n"
        "ok = todos esos textos se leen completos y sin errores; no hay otros textos con letras deformes o "
        "palabras sin sentido en un lugar destacado; aparece una sola persona, con cara y manos naturales (sin "
        "dedos de más ni rasgos deformes); la imagen se ve profesional. Si no está ok, di en issues qué falla.",
        ReferenceReview, tier="bulk", attachments=[(_jpeg(image, 1400), "image/jpeg")],
    )


def _fit(data: bytes, size: tuple[int, int]) -> bytes:
    image = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), size, Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def generate_covers(plan: MonthPlan, brand: Brand, clients_dir: Path, references: list[dict],
                    llm: LLMProvider) -> dict:
    """Genera con IA la imagen completa de cada pieza cuya referencia tiene prompt (piece.generated).

    Las ya generadas con los mismos textos se reutilizan. Sin cuota, se detiene y el resto de piezas sale con
    su plantilla.
    """
    from .photos import GENERATED, PHOTO, bank_dir, usable_photos
    from .references import references_dir

    by_id = {r["id"]: r["design"] for r in references}
    pieces = [p for p in plan.pieces if p.type in FORMATS and p.content
              and p.reference in by_id and by_id[p.reference].image_prompt]
    counts = {"generadas": 0, "reutilizadas": 0, "con_plantilla": 0}
    photos = usable_photos(clients_dir, brand.slug, PHOTO)
    if not pieces or not photos:
        progress.skip("imagenes_ia", "Ninguna pieza usa una referencia con prompt de imagen" if not pieces
                      else "No hay fotos reales de la persona en el banco")
        return counts
    folder = bank_dir(clients_dir, brand.slug)
    refs = references_dir(clients_dir, brand.slug)
    with progress.step("imagenes_ia", total=len(pieces)):
        for n, piece in enumerate(pieces):
            design = by_id[piece.reference]
            prompt = fill_prompt(design, piece)
            if prompt is None:
                counts["con_plantilla"] += 1
                progress.advance(item=piece.id, status="skipped", note="le faltan textos: sale con su plantilla")
                continue
            size, aspect = FORMATS[piece.type]
            digest = hashlib.sha1(f"{prompt}|{aspect}".encode()).hexdigest()[:8]
            target = folder / GENERATED / plan.month / f"{piece.id}-{digest}.png"
            if target.exists():
                piece.generated = str(target)
                counts["reutilizadas"] += 1
                progress.output(target)
                progress.advance(item=piece.id, note="reutilizada")
                continue
            people = identity_photos(photos, design.photo_file)
            attachments = [(_jpeg((refs / piece.reference / "reference.jpg").read_bytes()), "image/jpeg")]
            attachments += [(_identity(folder / p["file"]), "image/jpeg") for p in people]
            full = (
                "Reference image 1 is the layout reference: copy its composition, pose, framing, wardrobe and "
                f"graphic layout. Reference images 2 to {len(attachments)} are identity reference photos of the "
                "same man: his face must be exactly his, not the face from image 1.\n"
                f"{prompt}\n"
                "Render every quoted text exactly as written, in Spanish, with correct spelling and accents, and "
                "no other readable text, logos or watermarks."
                + (" Vertical 9:16 story: keep the bottom 15% of the image free of text." if piece.type == "story" else "")
            )
            issues = ""
            try:
                for attempt in range(1, ATTEMPTS + 1):
                    progress.activity(f"Generando {piece.id} (intento {attempt} de {ATTEMPTS})")
                    extra = f"\nFix these problems from the previous attempt: {issues}" if issues else ""
                    try:
                        image = _fit(llm.generate_image(full + extra, attachments, aspect), size)
                    except NoQuotaError:
                        raise
                    except Exception as exc:
                        issues = str(exc)
                        log.warning("No se generó la imagen de %s (intento %s): %s", piece.id, attempt, exc)
                        continue
                    progress.activity(f"Revisando los textos de {piece.id}")
                    verdict = review_cover(llm, image, texts_of(design, piece))
                    if verdict.ok:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(image)
                        piece.generated = str(target)
                        break
                    issues = verdict.issues or "la revisión no la aprobó"
                    log.info("Imagen de %s descartada (intento %s): %s", piece.id, attempt, issues)
            except NoQuotaError as exc:
                rest = len(pieces) - n
                counts["con_plantilla"] += rest
                log.warning("Sin cuota para generar imágenes (%s): %s piezas salen con su plantilla", exc, rest)
                progress.detail("Sin cuota del modelo de imágenes: el resto sale con su plantilla")
                progress.advance(item=piece.id, status="error", note="sin cuota")
                break
            if piece.generated:
                counts["generadas"] += 1
                progress.output(target)
                progress.advance(item=piece.id, note=f"ref {piece.reference}")
            else:
                counts["con_plantilla"] += 1
                progress.advance(item=piece.id, status="error", note=f"sale con su plantilla: {issues}"[:200])
            for key, value in counts.items():
                progress.metric(key.replace("_", " ").capitalize(), value)
    log.info("Imágenes con IA de %s %s: %s", brand.slug, plan.month, counts)
    return counts

"""Banco de fotos del cliente a partir de sus publicaciones de Instagram.

Lee las publicaciones con la conexión que ya tiene Postiz (el token del canal Instagram Standalone, en
su base de datos), recorta a la persona (modelo u2net de rembg), deja que el modelo descarte los recortes malos y
guarda clients/<slug>/fotos/<id>.png y fotos.json. Luego cada pieza de la parrilla elige una foto.
"""
from __future__ import annotations

import io
import json
import logging
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Callable

import httpx
import numpy as np
from PIL import Image, ImageOps

from .config import Settings
from .llm.base import LLMProvider, NoQuotaError
from .models import Brand, MonthPlan, PhotoChoices, PhotoReview, PhotoReviewBatch, Piece, VisualBatch

log = logging.getLogger(__name__)

GRAPH = "https://graph.instagram.com/v23.0"
MEDIA_FIELDS = "id,media_type,media_url,thumbnail_url,permalink,timestamp,children{media_type,media_url,thumbnail_url}"
# Piezas con diseño: los guiones de Reel son solo texto.
PHOTO_TYPES = ("post", "carousel", "story")
REVIEW_BATCH = 8
MIN_FIGURE_SHARE = 0.04  # menos del 4 % de la imagen: no hay persona o es un detalle
PHOTO = "foto"
CHARACTER = "personaje"  # poses del personaje 3D (character.py), en fotos/personaje/

REVIEW_SYSTEM = (
    "Revisas recortes automáticos (fondo quitado) de fotos de una marca personal para usarlos en "
    "diseños de redes sociales. Sé estricto: un recorte que se vea mal en una publicación no sirve."
)


def bank_dir(clients_dir: Path, slug: str) -> Path:
    return clients_dir / slug / "fotos"


def load_bank(clients_dir: Path, slug: str) -> dict:
    path = bank_dir(clients_dir, slug) / "fotos.json"
    if not path.exists():
        return {"photos": [], "seen": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_bank(clients_dir: Path, slug: str, bank: dict) -> None:
    path = bank_dir(clients_dir, slug) / "fotos.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")


def usable_photos(clients_dir: Path, slug: str, kind: str = PHOTO) -> list[dict]:
    """Fotos útiles del banco: las reales (kind "foto") o las poses del personaje (kind "personaje")."""
    folder = bank_dir(clients_dir, slug)
    return [p for p in load_bank(clients_dir, slug)["photos"]
            if p["usable"] and p.get("kind", PHOTO) == kind and (folder / p["file"]).exists()]


def instagram_token(settings: Settings, brand: Brand,
                    run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Token del canal de Instagram del cliente, tal como lo guarda Postiz (solo para leer publicaciones)."""
    integration = brand.postiz_integrations.get("instagram-standalone")
    if not integration:
        raise RuntimeError(f"{brand.slug} no tiene canal instagram-standalone en postiz_integrations: "
                           "el banco de fotos lee las publicaciones con esa conexión")
    if not re.fullmatch(r"[a-z0-9]+", integration):
        raise ValueError(f"ID de integración inesperado: {integration!r}")
    query = f"""select token from "Integration" where id = '{integration}' and "deletedAt" is null"""
    result = run(["docker", "exec", settings.postiz_db_container, "psql", "-U", settings.postiz_db_user,
                  "-d", settings.postiz_db_name, "-At", "-c", query], capture_output=True, text=True, timeout=60)
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeError(f"No se pudo leer el token de Instagram en Postiz: {result.stderr.strip() or 'canal no encontrado'}")
    return token


def fetch_media(token: str, http: httpx.Client | None = None) -> list[dict]:
    http = http or httpx.Client(timeout=30)
    posts: list[dict] = []
    url: str | None = f"{GRAPH}/me/media"
    params: dict | None = {"fields": MEDIA_FIELDS, "limit": 50, "access_token": token}
    while url:
        response = http.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        posts += data.get("data", [])
        url, params = data.get("paging", {}).get("next"), None
    return posts


def media_images(post: dict) -> list[tuple[str, str]]:
    """(id, URL) de cada imagen de la publicación: láminas de carrusel, imagen suelta o portada del Reel."""
    items = post.get("children", {}).get("data") or [post]
    images = []
    for k, item in enumerate(items):
        url = item.get("thumbnail_url") if item.get("media_type") == "VIDEO" else item.get("media_url")
        if url:
            images.append((f"{post['id']}-{k}", url))
    return images


def largest_figure(rgba: np.ndarray) -> np.ndarray | None:
    """Deja solo la figura más grande del recorte (el modelo a veces arrastra restos del fondo)."""
    from scipy import ndimage

    mask = rgba[..., 3] > 128
    labels, count = ndimage.label(mask)
    if not count:
        return None
    sizes = ndimage.sum(mask, labels, range(1, count + 1))
    keep = labels == int(np.argmax(sizes)) + 1
    if keep.sum() < MIN_FIGURE_SHARE * mask.size:
        return None
    out = rgba.copy()
    out[..., 3] = np.where(ndimage.binary_dilation(keep, iterations=2), out[..., 3], 0)
    return out


MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/{name}.onnx"


def _model_path(name: str) -> Path:
    """El modelo en la carpeta de rembg (~/.rembg/models o la antigua ~/.u2net); si no está, lo descarga."""
    candidates = [Path(f"~/.rembg/models/{name}/{name}.onnx").expanduser(), Path(f"~/.u2net/{name}.onnx").expanduser()]
    for path in candidates:
        if path.exists():
            return path
    path = candidates[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("Descargando el modelo de recorte %s (~170 MB) en %s", name, path)
    with httpx.stream("GET", MODEL_URL.format(name=name), follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with open(path.with_suffix(".part"), "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    path.with_suffix(".part").replace(path)
    return path


class Cutter:
    """Quita el fondo con un modelo u2net de rembg, ejecutado directamente con onnxruntime.

    u2net_human_seg (fotos reales) recorta solo a la persona; u2net (personaje) recorta el sujeto
    completo, con lo que tenga en las manos.

    No importa rembg: este importa pymatting y numba, y el Control inteligente de aplicaciones de
    Windows bloquea la DLL de numba. El resultado es el mismo que rembg.remove(post_process_mask=True).
    """

    MEAN, STD, SIZE = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225), (320, 320)

    def __init__(self, model: str = "u2net_human_seg"):
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(_model_path(model)), providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def mask(self, image: Image.Image) -> np.ndarray:
        from scipy import ndimage

        small = np.array(image.convert("RGB").resize(self.SIZE, Image.Resampling.LANCZOS), dtype=np.float64)
        small /= max(small.max(), 1e-6)
        small = (small - self.MEAN) / self.STD
        pred = self.session.run(None, {self.input: small.transpose(2, 0, 1)[None].astype(np.float32)})[0][0, 0]
        pred = (pred - pred.min()) / max(pred.max() - pred.min(), 1e-6)
        mask = np.array(Image.fromarray((pred * 255).astype("uint8"), mode="L")
                        .resize(image.size, Image.Resampling.LANCZOS))
        # Mismo suavizado de bordes que rembg (apertura con un disco de radio 1 + desenfoque + umbral).
        disk = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)
        mask = ndimage.grey_opening(mask, footprint=disk)
        mask = ndimage.gaussian_filter(mask.astype(np.float64), sigma=2)
        return np.where(mask < 127, 0, 255).astype(np.uint8)

    def __call__(self, data: bytes) -> bytes | None:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        rgba = np.dstack([np.array(image), self.mask(image)])
        figure = largest_figure(rgba)
        if figure is None:
            return None
        cut = Image.fromarray(figure)
        buffer = io.BytesIO()
        cut.crop(cut.getbbox()).save(buffer, format="PNG")
        return buffer.getvalue()


def _preview(png: bytes) -> bytes:
    """El recorte sobre gris medio, en JPEG: así el modelo ve los bordes y pesa poco."""
    cut = Image.open(io.BytesIO(png)).convert("RGBA")
    cut.thumbnail((512, 512))
    canvas = Image.new("RGB", cut.size, (128, 128, 128))
    canvas.paste(cut, mask=cut)
    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def review(llm: LLMProvider, cutouts: list[tuple[str, bytes]],
           subject: str = "una sola persona (la de la marca)") -> dict[str, PhotoReview]:
    reviews: dict[str, PhotoReview] = {}
    for start in range(0, len(cutouts), REVIEW_BATCH):
        batch = cutouts[start:start + REVIEW_BATCH]
        ids = [photo_id for photo_id, _ in batch]
        result = llm.generate(
            f"Revisa estos {len(batch)} recortes, en el mismo orden que la lista de ids de abajo.\n"
            f"usable = {subject} recortada limpia, sin restos de fondo, muebles, "
            "texto o gráficos pegados, sin partes del cuerpo cortadas de forma rara y con calidad suficiente "
            "para ocupar media publicación. Si no sirve, explica por qué en reason.\n"
            "mirrored_text = hay letras o números al revés (foto de cámara frontal).\n"
            "description: gesto, expresión, ropa, objetos y escena, en una frase.\n"
            "framing: cara, medio cuerpo o cuerpo entero.\n"
            f"<photos>{json.dumps(ids)}</photos>",
            PhotoReviewBatch, tier="bulk", system=REVIEW_SYSTEM,
            attachments=[(_preview(png), "image/jpeg") for _, png in batch],
        )
        for photo_id, item in zip(ids, result.reviews):
            reviews[photo_id] = item.model_copy(update={"id": photo_id})
    return reviews


def refresh_bank(settings: Settings, brand: Brand, llm: LLMProvider, *, token: str | None = None,
                 http: httpx.Client | None = None, cutter: Callable[[bytes], bytes | None] | None = None) -> dict:
    """Añade al banco las imágenes de las publicaciones que aún no se han procesado."""
    http = http or httpx.Client(timeout=60, follow_redirects=True)
    token = token or instagram_token(settings, brand)
    bank = load_bank(settings.clients_dir, brand.slug)
    seen = set(bank["seen"])
    folder = bank_dir(settings.clients_dir, brand.slug)
    folder.mkdir(parents=True, exist_ok=True)

    new: list[tuple[str, bytes, dict]] = []
    for post in fetch_media(token, http):
        for image_id, url in media_images(post):
            if image_id in seen:
                continue
            seen.add(image_id)
            cutter = cutter or Cutter()
            png = cutter(http.get(url).raise_for_status().content)
            if png:
                new.append((image_id, png, post))
    reviews = review(llm, [(image_id, png) for image_id, png, _ in new]) if new else {}

    for image_id, png, post in new:
        verdict = reviews.get(image_id, PhotoReview(id=image_id, reason="sin revisión"))
        cut = Image.open(io.BytesIO(png))
        if verdict.mirrored_text:
            cut = ImageOps.mirror(cut)  # la cámara frontal invierte la imagen: así el texto se lee bien
        cut.save(folder / f"{image_id}.png")
        bank["photos"].append({
            "id": image_id, "file": f"{image_id}.png", "source": post.get("permalink", ""),
            "date": post.get("timestamp", "")[:10], "usable": verdict.usable, "reason": verdict.reason,
            "description": verdict.description, "framing": verdict.framing,
        })
    bank["seen"] = sorted(seen)
    save_bank(settings.clients_dir, brand.slug, bank)
    usable = sum(1 for p in bank["photos"] if p["usable"])
    log.info("Banco de fotos de %s: %s recortes nuevos, %s útiles en total", brand.slug, len(new), usable)
    return {"nuevas": len(new), "utiles_nuevas": sum(1 for i, _, _ in new if reviews.get(i) and reviews[i].usable),
            "utiles": usable}


GENERATED = "generadas"
GENERATE_ATTEMPTS = 2
GENERATE_PROMPT = (
    "Fotografía nueva y realista de la misma persona de las imágenes de referencia: conserva exactamente "
    "su cara, barba, corte de pelo, tono de piel y complexión. {visual}. Una sola persona, encuadre de "
    "medio cuerpo, fondo liso gris claro sin objetos, luz de estudio suave. Sin texto, sin logos, sin "
    "marcas de agua y sin pantallas legibles."
)


def is_generated(photo: str) -> bool:
    return photo.startswith(f"{GENERATED}/")


def describe_visuals(plan: MonthPlan, brand: Brand, llm: LLMProvider) -> int:
    """Rellena `visual` en planes escritos antes de que existiera el campo. Devuelve cuántas completó."""
    from .planner import VISUAL_RULE

    missing = [p for p in plan.pieces if p.type in PHOTO_TYPES and p.content and not p.content.visual]
    if not missing:
        return 0
    items = [{"slot_id": p.id, "type": p.type, "pillar": p.pillar, "headline": p.content.headline,
              "caption": p.content.caption[:300]} for p in missing]
    result = llm.generate(
        f"Marca: {brand.name} ({brand.sector}). Tono: {brand.tone}.\n"
        f"Para cada pieza escribe su campo {VISUAL_RULE}\n"
        f"<pieces>{json.dumps(items, ensure_ascii=False)}</pieces>",
        VisualBatch, tier="bulk",
    )
    by_slot = {v.slot_id: v.visual.strip() for v in result.items if v.visual.strip()}
    for piece in missing:
        piece.content.visual = by_slot.get(piece.id, "")
    return sum(1 for p in missing if p.content.visual)


def generate_photos(plan: MonthPlan, brand: Brand, clients_dir: Path, llm: LLMProvider,
                    cutter: Callable[[bytes], bytes | None] | None = None) -> dict:
    """Cambia la foto asignada de cada pieza por una nueva generada con IA a partir de ella.

    Referencias: la foto que eligió assign_photos y dos primeros planos del banco (para la cara).
    Cada imagen se recorta y la revisa el modelo; tras GENERATE_ATTEMPTS intentos sin un recorte útil,
    la pieza se queda con la foto original. Las ya generadas (mismo mes y pieza) no se repiten.
    """
    folder = bank_dir(clients_dir, brand.slug)
    photos = usable_photos(clients_dir, brand.slug)
    faces = [p["file"] for p in photos if p["framing"] == "cara"] or [p["file"] for p in photos]
    counts = Counter()
    pending = []
    for piece in plan.pieces:
        if piece.type not in PHOTO_TYPES or not piece.photo or not (piece.content and piece.content.visual):
            continue
        target = f"{GENERATED}/{plan.month}/{piece.id}.png"
        if (folder / target).exists():
            piece.photo = target
            counts["reutilizadas"] += 1
        elif not is_generated(piece.photo):
            pending.append(piece)

    for attempt in range(GENERATE_ATTEMPTS):
        if not pending:
            break
        candidates: list[tuple[Piece, bytes]] = []
        for piece in pending:
            references = [piece.photo] + [f for f in faces if f != piece.photo][:2]
            try:
                image = llm.generate_image(GENERATE_PROMPT.format(visual=piece.content.visual.rstrip(".")),
                                           [((folder / f).read_bytes(), "image/png") for f in references])
            except NoQuotaError:
                raise
            except Exception as exc:
                log.warning("No se generó la foto de %s (intento %s): %s", piece.id, attempt + 1, exc)
                continue
            cutter = cutter or Cutter()
            png = cutter(image)
            if png:
                candidates.append((piece, png))
        verdicts = review(llm, [(p.id, png) for p, png in candidates]) if candidates else {}
        done = set()
        for piece, png in candidates:
            verdict = verdicts.get(piece.id)
            if not (verdict and verdict.usable):
                log.info("Foto generada de %s descartada: %s", piece.id, verdict.reason if verdict else "sin revisión")
                continue
            target = f"{GENERATED}/{plan.month}/{piece.id}.png"
            (folder / target).parent.mkdir(parents=True, exist_ok=True)
            cut = Image.open(io.BytesIO(png))
            (ImageOps.mirror(cut) if verdict.mirrored_text else cut).save(folder / target)
            piece.photo = target
            done.add(piece.id)
            counts["generadas"] += 1
        pending = [p for p in pending if p.id not in done]

    counts["originales"] += len(pending)
    for piece in pending:
        log.warning("%s se queda con la foto original %s", piece.id, piece.photo)
    log.info("Fotos con IA de %s %s: %s", brand.slug, plan.month, dict(counts))
    return {"generadas": counts["generadas"], "reutilizadas": counts["reutilizadas"], "originales": counts["originales"]}


def assign_photos(plan: MonthPlan, photos: list[dict], llm: LLMProvider) -> None:
    """Elige una foto del banco para cada post, carrusel e historia, sin repetir dentro de la semana."""
    pieces = [p for p in plan.pieces if p.type in PHOTO_TYPES and p.content]
    if not photos or not pieces:
        return
    catalog = [{"file": p["file"], "framing": p["framing"], "description": p["description"]} for p in photos]
    items = [{"slot_id": p.id, "type": p.type, "pillar": p.pillar, "topic": p.topic,
              "headline": p.content.headline} for p in pieces]
    result = llm.generate(
        "Elige para cada pieza la foto de la persona de la marca que mejor acompaña su tema y tono: "
        "un gesto de explicar para lo educativo, una sonrisa o escena personal para la conexión, una "
        "pose segura para la venta. No repitas una foto en la misma semana y reparte el uso del banco. "
        "Devuelve el file exacto del catálogo para cada slot_id.\n"
        f"<photos>{json.dumps(catalog, ensure_ascii=False)}</photos>\n"
        f"<pieces>{json.dumps(items, ensure_ascii=False)}</pieces>",
        PhotoChoices, tier="bulk",
    )
    valid = {p["file"] for p in photos}
    chosen = {c.slot_id: c.photo for c in result.choices if c.photo in valid}
    uses = Counter(chosen.values())
    for piece in pieces:
        # Si el modelo no eligió o eligió algo que no existe, la foto menos usada hasta ahora.
        piece.photo = chosen.get(piece.id) or min(valid, key=lambda f: (uses[f], f))
        uses[piece.photo] += 1 if piece.id not in chosen else 0

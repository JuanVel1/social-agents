"""Moodboard de Pinterest: el paso "Investigación visual" del flujo (curaduría y tendencias).

Una persona (la agencia o el cliente) guarda en un tablero de Pinterest los pines que sirven de
referencia: composición, colores, tipo de foto, recursos gráficos. El agente lee ese tablero con la
conexión de Pinterest que ya tiene Postiz (solo lectura, funciona con el acceso Trial), Gemini mira
las imágenes y escribe una dirección visual adaptada a la identidad de la marca, y esa guía llega a la
parrilla mensual (brief de marca → campo `visual` de cada pieza y elección de fotos o poses).

Resultado: clients/<slug>/moodboard.json. Si el tablero no cambió, no se vuelve a analizar.
"""
from __future__ import annotations

import io
import json
import logging
from datetime import date
from pathlib import Path

import httpx
from PIL import Image

from . import progress
from .config import Settings
from .llm.base import LLMProvider
from .models import Brand, VisualGuide
from .photos import postiz_token

log = logging.getLogger(__name__)

API = "https://api.pinterest.com/v5"
MAX_PINS = 30  # los más recientes: el tablero puede crecer sin que el análisis se vuelva enorme
IMAGE_SIZES = ("600x", "1200x", "originals", "400x300")

GUIDE_SYSTEM = (
    "Eres director de arte de una agencia de redes sociales. Lees un moodboard de referencias y lo "
    "conviertes en principios de diseño accionables para las piezas de una marca concreta."
)


def guide_path(clients_dir: Path, slug: str) -> Path:
    return clients_dir / slug / "moodboard.json"


def board_pins(token: str, board: str, http: httpx.Client | None = None, limit: int = MAX_PINS) -> list[dict]:
    http = http or httpx.Client(timeout=30)
    pins: list[dict] = []
    params: dict = {"page_size": 50}
    while len(pins) < limit:
        response = http.get(f"{API}/boards/{board}/pins", params=params, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()
        pins += data.get("items", [])
        if not data.get("bookmark"):
            break
        params["bookmark"] = data["bookmark"]
    return pins[:limit]


def pin_image(pin: dict, sizes: tuple[str, ...] = IMAGE_SIZES) -> str | None:
    """URL de la imagen del pin: la imagen, la primera de un pin con varias o la portada de un video."""
    media = pin.get("media") or {}
    images = media.get("images") or ((media.get("items") or [{}])[0].get("images")) or {}
    for size in sizes:
        if images.get(size, {}).get("url"):
            return images[size]["url"]
    return media.get("cover_image_url")


def _thumbnail(data: bytes) -> bytes:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((512, 512))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def analyze(brand: Brand, pins: list[tuple[dict, bytes]], llm: LLMProvider) -> VisualGuide:
    notes = [{"title": p.get("title") or "", "description": (p.get("description") or "")[:200]} for p, _ in pins]
    return llm.generate(
        f"Marca: {brand.name} ({brand.sector}). Estilo de sus plantillas: {brand.style.name or 'por defecto'}. "
        f"Colores de la marca: {json.dumps(brand.colors, ensure_ascii=False)}. "
        f"Tipografías: {json.dumps(brand.fonts, ensure_ascii=False)}.\n"
        f"Estas {len(pins)} imágenes son el moodboard de referencias que curó la marca en Pinterest "
        "(títulos y descripciones abajo, en el mismo orden). Extrae los principios visuales que se repiten "
        "y tradúcelos a esta marca: los colores y tipografías de la marca mandan; las referencias aportan "
        "composición, tratamiento de la imagen, recursos gráficos y tendencias. No describas imágenes sueltas "
        "ni propongas copiarlas, y no menciones marcas o logos que aparezcan.\n"
        "summary: 2-3 frases con la dirección visual. Cada lista: 3-5 puntos breves y concretos que un "
        "diseñador o un generador de imágenes pueda aplicar. avoid: lo que no encaja con la marca.\n"
        f"<pins>{json.dumps(notes, ensure_ascii=False)}</pins>",
        VisualGuide, tier="quality", system=GUIDE_SYSTEM,
        attachments=[(image, "image/jpeg") for _, image in pins],
    )


@progress.tracked("direccion")
def refresh_moodboard(settings: Settings, brand: Brand, llm: LLMProvider, *, token: str | None = None,
                      http: httpx.Client | None = None, force: bool = False) -> dict:
    """Lee el tablero y, si cambió, rehace la dirección visual. Devuelve {"pines", "analizado", "guide"}."""
    board = brand.moodboard
    if not board.board:
        raise RuntimeError(f"{brand.slug} no tiene moodboard.board en brand.yaml (social-agent integrations "
                           "lista los tableros de Pinterest)")
    if not token:
        if settings.pinterest_access_token:
            token = settings.pinterest_access_token
        elif board.pinterest:
            token = postiz_token(settings, board.pinterest, "Pinterest")
        else:
            raise RuntimeError(f"{brand.slug} no tiene moodboard.pinterest (canal de Postiz) ni hay "
                               "PINTEREST_ACCESS_TOKEN en .env")
    http = http or httpx.Client(timeout=60, follow_redirects=True)
    progress.activity("Leyendo el tablero de Pinterest")
    pins = [p for p in board_pins(token, board.board, http) if pin_image(p)]
    progress.metric("Pines con imagen", len(pins))
    path = guide_path(settings.clients_dir, brand.slug)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ids = sorted(p["id"] for p in pins)
    if not pins:
        log.warning("El moodboard de %s (tablero %s) no tiene pines con imagen", brand.slug, board.board)
        return {"pines": 0, "analizado": False, "guide": previous.get("guide")}
    if not force and previous.get("board") == board.board and previous.get("pins") == ids:
        progress.detail("El tablero no cambió: se mantiene la guía")
        return {"pines": len(pins), "analizado": False, "guide": previous.get("guide")}

    progress.set_total(len(pins) + 1)
    images = []
    for pin in pins:
        progress.advance(item=str(pin.get("id")))
        try:
            images.append((pin, _thumbnail(http.get(pin_image(pin)).raise_for_status().content)))
        except Exception as exc:  # una imagen caída no detiene el análisis
            log.warning("No se pudo descargar el pin %s: %s", pin.get("id"), exc)
    progress.detail(f"Analizando {len(images)} pines")
    guide = analyze(brand, images, llm)
    progress.advance(item="Guía visual")
    path.write_text(json.dumps({"board": board.board, "pins": ids, "updated": date.today().isoformat(),
                                "guide": guide.model_dump()}, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Dirección visual de %s actualizada con %s pines", brand.slug, len(images))
    return {"pines": len(pins), "analizado": True, "guide": guide.model_dump()}


def moodboard_token(settings: Settings, brand: Brand) -> str:
    if settings.pinterest_access_token:
        return settings.pinterest_access_token
    if brand.moodboard.pinterest:
        return postiz_token(settings, brand.moodboard.pinterest, "Pinterest")
    raise RuntimeError(f"{brand.slug} no tiene moodboard.pinterest (canal de Postiz) ni hay PINTEREST_ACCESS_TOKEN en .env")


def _design_subject(settings: Settings, brand: Brand) -> dict:
    """Con qué imagen de la marca se prueban las plantillas y qué poses hay para elegir."""
    from .photos import PHOTO, usable_photos

    photos = usable_photos(settings.clients_dir, brand.slug, PHOTO)
    wants_real = brand.style.real_photo_references or brand.style.ai_images
    real = photos if wants_real else []
    return {"poses": photos, "subject": "la persona de la marca", "real_photos": real}


def refresh_pin_references(settings: Settings, brand: Brand, llm: LLMProvider, *, token: str | None = None,
                           http: httpx.Client | None = None, local: tuple[Path, ...] = (), force: bool = False,
                           retry: bool = False, only: set[str] | None = None, renderer=None) -> dict:
    """Convierte en plantillas de la marca los pines del moodboard (y las imágenes locales) que aún no lo son."""
    from .references import local_id, needs_design, refresh_references, references_dir

    folder = references_dir(settings.clients_dir, brand.slug)
    items: list[dict] = []
    if brand.moodboard.board:
        with progress.step("pines"):
            http = http or httpx.Client(timeout=60, follow_redirects=True)
            token = token or moodboard_token(settings, brand)
            progress.activity("Leyendo el tablero de Pinterest")
            for pin in board_pins(token, brand.moodboard.board, http):
                url = pin_image(pin, ("1200x", "originals", "600x"))
                if not url or not needs_design(folder / pin["id"], force, retry):
                    continue
                try:
                    image = http.get(url).raise_for_status().content
                except Exception as exc:
                    log.warning("No se pudo descargar el pin %s: %s", pin["id"], exc)
                    progress.advance(item=pin["id"], status="error", note=str(exc)[:200])
                    continue
                items.append({"id": pin["id"], "image": image,
                              "title": (pin.get("title") or pin.get("description") or "")[:80],
                              "url": f"https://www.pinterest.com/pin/{pin['id']}/"})
                progress.advance(item=pin["id"], note=items[-1]["title"])
            progress.metric("Pines por diseñar", len(items))
    else:
        progress.skip("pines", "La marca no tiene tablero")
    for path in local:
        data = path.read_bytes()
        items.append({"id": local_id(data), "image": data, "title": path.stem, "url": ""})
    # Las imágenes locales descartadas se reintentan con la copia que quedó guardada.
    given = {item["id"] for item in items}
    for saved in sorted(folder.glob("local-*/reference.jpg")) if retry or force else []:
        if saved.parent.name not in given and needs_design(saved.parent, force, retry):
            items.append({"id": saved.parent.name, "image": saved.read_bytes(), "title": "", "url": ""})
    if only:
        items = [item for item in items if item["id"] in only]
    if not items:
        progress.skip("personas", "No hay referencias nuevas")
        progress.skip("plantillas", "No hay referencias nuevas")
        return {"nuevas": 0, "descartadas": 0, "existentes": len(list(folder.glob("*/design.json")))}

    build = _design_subject(settings, brand)
    if renderer is not None:
        return refresh_references(settings.clients_dir, brand, llm, items, renderer, force=force, retry=retry, **build)
    from .render import ReferenceRenderer

    with ReferenceRenderer(brand, settings.templates_dir, settings.clients_dir) as render:
        return refresh_references(settings.clients_dir, brand, llm, items, render, force=force, retry=retry, **build)

"""Convierte cada pieza en PNG a partir de plantillas HTML/CSS de la marca."""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape
from playwright.sync_api import sync_playwright

from .photos import bank_dir

from .models import Brand, MonthPlan, Piece, Slide

log = logging.getLogger(__name__)

SIZES = {"post": (1080, 1350), "carousel": (1080, 1350), "story": (1080, 1920)}


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def highlight(text: str, words: str) -> Markup:
    """Envuelve en <em> la primera aparición de `words` en el texto (sin distinguir mayúsculas)."""
    if not words.strip():
        return escape(text)
    match = re.search(re.escape(words.strip()), text, flags=re.IGNORECASE)
    if not match:
        return escape(text)
    a, b = match.span()
    return escape(text[:a]) + Markup("<em>") + escape(text[a:b]) + Markup("</em>") + escape(text[b:])


def environment(templates_dir: Path, style: str = "") -> Environment:
    """Las plantillas del estilo (templates/estilos/<style>/) reemplazan a las de por defecto."""
    folders = ([templates_dir / "estilos" / style] if style else []) + [templates_dir]
    env = Environment(loader=FileSystemLoader(folders), undefined=StrictUndefined, autoescape=True)
    env.filters["highlight"] = highlight
    return env


def _pages(piece: Piece, base: dict) -> list[tuple[str, dict]]:
    """(plantilla, contexto) de cada imagen de la pieza."""
    c = piece.content
    slides = c.slides or [Slide(heading=c.headline, body="")]
    ctx = {**base, "pillar": piece.pillar, "headline": c.headline, "highlight_words": c.highlight,
           "cta": c.cta, "sticker": c.sticker}
    if piece.type == "post":
        return [("post.html.j2", {**ctx, "slide": slides[0]})]
    if piece.type == "story":
        return [("story.html.j2", {**ctx, "slide": slides[0]})]
    total = len(slides)
    return [
        ("slide.html.j2", {**ctx, "slide": s, "index": i + 1, "total": total,
                           "first": i == 0, "last": i == total - 1})
        for i, s in enumerate(slides)
    ]


def piece_context(piece: Piece, brand: Brand, clients_dir: Path) -> dict:
    """Lo que cambia por pieza en los estilos: la foto del banco y el fondo según el pilar."""
    photo = bank_dir(clients_dir, brand.slug) / piece.photo if piece.photo else None
    return {
        "photo": _data_uri(photo) if photo and photo.exists() else None,
        "light": piece.pillar in brand.style.light_pillars,
    }


def render_plan(plan: MonthPlan, brand: Brand, templates_dir: Path, clients_dir: Path, out_dir: Path) -> None:
    env = environment(templates_dir, brand.style.name)
    logo_path = clients_dir / brand.slug / brand.logo if brand.logo else None
    base = {
        "colors": brand.colors,
        "fonts": brand.fonts,
        "handle": brand.handle,
        "brand_name": brand.name,
        "logo": _data_uri(logo_path) if logo_path and logo_path.exists() else None,
    }
    target = out_dir / brand.slug / plan.month
    target.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for piece in plan.pieces:
            if piece.type not in SIZES or piece.content is None:
                continue
            width, height = SIZES[piece.type]
            page = browser.new_page(viewport={"width": width, "height": height})
            piece.images = []
            for n, (template, ctx) in enumerate(_pages(piece, {**base, **piece_context(piece, brand, clients_dir)}), start=1):
                html = env.get_template(template).render(width=width, height=height, **ctx)
                page.set_content(html, wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                path = target / (f"{piece.id}.png" if piece.type != "carousel" else f"{piece.id}-{n:02d}.png")
                page.screenshot(path=str(path))
                piece.images.append(str(path))
            page.close()
            log.info("Renderizada %s (%s imágenes)", piece.id, len(piece.images))
        browser.close()

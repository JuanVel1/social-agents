"""Convierte cada pieza en PNG a partir de plantillas HTML/CSS de la marca."""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.sync_api import sync_playwright

from .models import Brand, MonthPlan, Piece, Slide

log = logging.getLogger(__name__)

SIZES = {"post": (1080, 1350), "carousel": (1080, 1350), "story": (1080, 1920)}


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _pages(piece: Piece, base: dict) -> list[tuple[str, dict]]:
    """(plantilla, contexto) de cada imagen de la pieza."""
    c = piece.content
    slides = c.slides or [Slide(heading=c.headline, body="")]
    ctx = {**base, "pillar": piece.pillar, "headline": c.headline, "cta": c.cta, "sticker": c.sticker}
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


def render_plan(plan: MonthPlan, brand: Brand, templates_dir: Path, clients_dir: Path, out_dir: Path) -> None:
    env = Environment(loader=FileSystemLoader(templates_dir), undefined=StrictUndefined, autoescape=True)
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
            for n, (template, ctx) in enumerate(_pages(piece, base), start=1):
                html = env.get_template(template).render(width=width, height=height, **ctx)
                page.set_content(html, wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                path = target / (f"{piece.id}.png" if piece.type != "carousel" else f"{piece.id}-{n:02d}.png")
                page.screenshot(path=str(path))
                piece.images.append(str(path))
            page.close()
            log.info("Renderizada %s (%s imágenes)", piece.id, len(piece.images))
        browser.close()

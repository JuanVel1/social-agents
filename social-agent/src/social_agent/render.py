"""Convierte cada pieza en PNG a partir de plantillas HTML/CSS de la marca."""
from __future__ import annotations

import base64
import logging
import mimetypes
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment
from markupsafe import Markup, escape
from playwright.sync_api import sync_playwright

from . import progress
from .models import Brand, MonthPlan, Piece, Slide
from .photos import bank_dir

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


def environment(templates_dir: Path, style: str = "", extra: tuple[Path, ...] = ()) -> Environment:
    """Las plantillas del estilo (templates/estilos/<style>/) reemplazan a las de por defecto.

    extra: carpetas que se buscan antes, como las plantillas de referencias del cliente. Es un entorno
    sandbox porque esas plantillas las escribe un modelo.
    """
    folders = list(extra) + ([templates_dir / "estilos" / style] if style else []) + [templates_dir]
    env = ImmutableSandboxedEnvironment(loader=FileSystemLoader(folders), undefined=StrictUndefined, autoescape=True)
    env.filters["highlight"] = highlight
    return env


ALLOWED_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def guard(page) -> None:
    """La página solo puede cargar las fuentes de Google (y data: URIs): nada más sale a la red."""
    def route(r):
        url = r.request.url
        if url.startswith("data:") or url.split("/")[2] in ALLOWED_HOSTS:
            r.continue_()
        else:
            r.abort()
    page.route("**/*", route)


def brand_context(brand: Brand, clients_dir: Path) -> dict:
    logo_path = clients_dir / brand.slug / brand.logo if brand.logo else None
    return {
        "colors": brand.colors,
        "fonts": brand.fonts,
        "handle": brand.handle,
        "brand_name": brand.name,
        "logo": _data_uri(logo_path) if logo_path and logo_path.exists() else None,
    }


class ReferenceRenderer:
    """Renderiza plantillas de referencias para probarlas: (PNG, problemas de desborde)."""

    def __init__(self, brand: Brand, templates_dir: Path, clients_dir: Path):
        from .references import references_dir

        self.env = environment(templates_dir, brand.style.name, (references_dir(clients_dir, brand.slug),))
        self.base = brand_context(brand, clients_dir)
        self.bank = bank_dir(clients_dir, brand.slug)

    def __enter__(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch()
        return self

    def __exit__(self, *exc):
        self.browser.close()
        self._pw.stop()

    def __call__(self, template: str, design, ctx: dict, photo: str | None) -> tuple[bytes, list[str]]:
        """ctx: kind (post, story o slide), f, slide, index, total… (references.sample_context)."""
        from .references import OVERFLOW_JS, SIZES

        width, height = SIZES[ctx["kind"]]
        image = self.bank / photo if photo else None
        show = design.photo and ctx["kind"] != "slide" and image and image.exists()
        html = self.env.get_template(template).render(
            width=width, height=height, extra_font=design.extra_font, photo=_data_uri(image) if show else None,
            **self.base, **ctx)
        page = self.browser.new_page(viewport={"width": width, "height": height})
        guard(page)
        try:
            page.set_content(html, wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            return page.screenshot(), page.evaluate(OVERFLOW_JS)
        finally:
            page.close()


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


def _reference_pages(piece: Piece, base: dict, references: dict, clients_dir: Path, brand: Brand,
                     standard: list | None = None) -> list | None:
    """Páginas de una pieza que adapta una referencia del moodboard, o None si no aplica.

    Post e historia: la composición principal con los textos de sus huecos. Carrusel: esa composición de
    portada y, después, una lámina por cada slide con el mismo estilo; si la plantilla no trae formato de
    lámina, las láminas internas son las estándar (`standard`).
    """
    from .references import field_values, supports_slide

    reference = references.get(piece.reference) if piece.reference else None
    if not reference or piece.type not in ("post", "story", "carousel") or not reference.get("template_ok", True):
        return None
    design = reference["design"]
    template = f"{piece.reference}/template.html.j2"
    image = bank_dir(clients_dir, brand.slug) / piece.photo if piece.photo else None
    c = piece.content
    ctx = {**base, "f": field_values(design, c.fields), "extra_font": design.extra_font,
           "photo": _data_uri(image) if design.photo and image and image.exists() else None,
           "headline": c.headline, "highlight_words": c.highlight, "cta": c.cta,
           "slide": c.slides[0] if c.slides else Slide(heading=c.headline, body=""), "index": 1,
           "total": max(len(c.slides), 1), "first": True, "last": len(c.slides) <= 1}
    if piece.type != "carousel":
        return [(template, {**ctx, "kind": piece.type})]
    pages = [(template, {**ctx, "kind": "post"})]
    if not supports_slide(design):
        return pages + list((standard or [])[1:])
    for i, slide in enumerate(c.slides[1:], start=2):
        pages.append((template, {**ctx, "kind": "slide", "photo": None, "slide": slide, "index": i,
                                 "first": False, "last": i == len(c.slides)}))
    return pages


def render_plan(plan: MonthPlan, brand: Brand, templates_dir: Path, clients_dir: Path, out_dir: Path) -> None:
    from .references import load_references, references_dir

    env = environment(templates_dir, brand.style.name, (references_dir(clients_dir, brand.slug),))
    references = {r["id"]: r for r in load_references(clients_dir, brand.slug)}
    base = brand_context(brand, clients_dir)
    target = out_dir / brand.slug / plan.month
    target.mkdir(parents=True, exist_ok=True)

    renderable = [p for p in plan.pieces if p.type in SIZES and p.content is not None]
    with progress.step("render", total=len(renderable)), sync_playwright() as pw:
        progress.activity("Abriendo Chromium")
        browser = pw.chromium.launch()
        for piece in renderable:
            progress.activity(f"Diseñando {piece.id}")
            width, height = SIZES[piece.type]
            page = browser.new_page(viewport={"width": width, "height": height})
            guard(page)
            standard = _pages(piece, {**base, **piece_context(piece, brand, clients_dir)})
            pages = _reference_pages(piece, base, references, clients_dir, brand, standard) or standard
            try:
                piece.images = _shoot(page, env, pages, piece, target, width, height)
            except Exception as exc:
                if pages is standard:
                    raise
                # Una plantilla de referencia que falla no frena la parrilla: el post sale con la estándar.
                log.warning("La referencia %s falló en %s (%s): se usa la plantilla estándar",
                            piece.reference, piece.id, exc)
                piece.reference = piece.reference_url = ""
                piece.images = _shoot(page, env, standard, piece, target, width, height)
            page.close()
            if piece.generated and piece.images and Path(piece.generated).is_file():
                # La imagen completa generada con IA ocupa el lugar de la primera (la única, o la portada).
                Path(piece.images[0]).write_bytes(Path(piece.generated).read_bytes())
            log.info("Renderizada %s (%s imágenes)", piece.id, len(piece.images))
            for image in piece.images:
                progress.output(image)
            progress.count("Imágenes", len(piece.images))
            progress.advance(item=piece.id, note=f"{len(piece.images)} img"
                             + (f" · ref {piece.reference}" if piece.reference else "")
                             + (" · imagen con IA" if piece.generated else ""))
        browser.close()


def _shoot(page, env, pages, piece: Piece, target: Path, width: int, height: int) -> list[str]:
    images = []
    for n, (template, ctx) in enumerate(pages, start=1):
        html = env.get_template(template).render(width=width, height=height, **ctx)
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        path = target / (f"{piece.id}.png" if piece.type != "carousel" else f"{piece.id}-{n:02d}.png")
        page.screenshot(path=str(path))
        images.append(str(path))
    return images

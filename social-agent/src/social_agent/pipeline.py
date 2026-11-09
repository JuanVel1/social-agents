from __future__ import annotations

import logging
from pathlib import Path

from .approval import TYPE_LABELS as PIECE_LABELS
from .approval import NotionBoard
from .config import Settings
from .llm import get_provider
from .models import Brand, MonthPlan
from .llm.base import NoQuotaError
from .photos import assign_photos, describe_visuals, generate_photos, usable_photos
from .planner import plan_month
from .publish import PostizClient, describe, soon
from .render import render_plan

log = logging.getLogger(__name__)
TYPE_LABELS = {"post": "Posts", "carousel": "Carruseles", "story": "Historias", "reel_script": "Guiones de Reel"}
DAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def plan_path(settings: Settings, client: str, month: str) -> Path:
    return settings.out_dir / client / month / "plan.json"


def save_plan(settings: Settings, plan: MonthPlan) -> Path:
    path = plan_path(settings, plan.client, plan.month)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path


def write_summary(settings: Settings, plan: MonthPlan) -> Path:
    lines = [f"# Parrilla {plan.client} · {plan.month}", ""]
    lines += [f"- {label}: {plan.count(t)}" for t, label in TYPE_LABELS.items()]
    for p in plan.pieces:
        c = p.content
        day = f"{DAYS[p.date.weekday()]} {p.date.day}"
        lines += ["", f"## {day} · {PIECE_LABELS[p.type]} · {p.pillar}", f"**{c.headline if c else p.topic}**", ""]
        if c and p.type == "carousel":
            lines += [f"{i}. **{s.heading}** — {s.body}" for i, s in enumerate(c.slides, 1)] + [""]
        if c and p.type == "reel_script":
            lines += [c.script, ""]
        if c and p.type == "story":
            lines += [c.slides[0].body if c.slides else "", f"Sticker: {c.sticker}", ""]
        if p.caption_full:
            lines += [p.caption_full]
    path = settings.out_dir / plan.client / plan.month / "resumen.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_month(settings: Settings, client: str, month: str, dry_run: bool) -> MonthPlan:
    brand = Brand.load(settings.clients_dir, client)
    llm = get_provider(settings)
    plan = plan_month(brand, month, llm)
    if brand.style.photos:
        photos = usable_photos(settings.clients_dir, brand.slug)
        if photos:
            assign_photos(plan, photos, llm)
            if brand.style.generate_photos:
                try:
                    generate_photos(plan, brand, settings.clients_dir, llm)
                except NoQuotaError as exc:
                    # La parrilla sale igual, con las fotos originales del banco.
                    log.warning("Sin fotos generadas para %s: %s", client, exc)
        else:
            log.warning("%s usa fotos pero su banco está vacío: ejecuta social-agent fotos --client %s", client, client)
    render_plan(plan, brand, settings.templates_dir, settings.clients_dir, settings.out_dir)
    save_plan(settings, plan)
    summary = write_summary(settings, plan)
    log.info("Parrilla lista: %s", summary)
    if not dry_run:
        send_for_approval(settings, plan, plan.pieces)
    return plan


def send_for_approval(settings: Settings, plan: MonthPlan, pieces: list) -> None:
    """Sube las imágenes a Postiz y crea una fila en Notion (Pendiente) por pieza."""
    postiz = PostizClient(settings.postiz_url, settings.postiz_api_key)
    board = NotionBoard(settings.notion_token, settings.notion_database_id)
    for piece in pieces:
        piece.postiz_media = [postiz.upload(Path(p)) for p in piece.images]
        piece.image_urls = [postiz.public_url(m) for m in piece.postiz_media]
        board.add_piece(plan.client, plan.month, piece)
        save_plan(settings, plan)
    log.info("%s piezas enviadas a Notion para aprobación", len(pieces))


def redesign_month(settings: Settings, client: str, month: str, dry_run: bool) -> dict:
    """Rehace las imágenes de una parrilla ya escrita (mismos textos) con fotos generadas con IA.

    Sin dry_run, reemplaza en Notion solo las filas que siguen en Pendiente: las archiva y las crea de
    nuevo con las imágenes nuevas. Las que ya se aprobaron, programaron o tienen cambios no se tocan.
    """
    brand = Brand.load(settings.clients_dir, client)
    plan = MonthPlan.model_validate_json(plan_path(settings, client, month).read_text(encoding="utf-8"))
    llm = get_provider(settings)
    described = describe_visuals(plan, brand, llm)
    photos = generate_photos(plan, brand, settings.clients_dir, llm)
    render_plan(plan, brand, settings.templates_dir, settings.clients_dir, settings.out_dir)
    save_plan(settings, plan)
    write_summary(settings, plan)
    result = {"visuales": described, **photos, "reemplazadas": 0, "sin_tocar": []}
    if dry_run:
        return result

    board = NotionBoard(settings.notion_token, settings.notion_database_id)
    rows = board.rows(f"{client}/{month}/")
    replace = []
    for piece in plan.pieces:
        page_id, state = rows.get(f"{client}/{month}/{piece.id}", ("", "Pendiente"))
        if state != "Pendiente":
            result["sin_tocar"].append(f"{piece.id} ({state})")
            continue
        if page_id:
            board.archive(page_id)
        replace.append(piece)
    send_for_approval(settings, plan, replace)
    result["reemplazadas"] = len(replace)
    return result


def publish_approved(settings: Settings, now: bool = False) -> int:
    """Programa en Postiz lo aprobado. Con now=True usa unos minutos desde ahora (pruebas)."""
    board = NotionBoard(settings.notion_token, settings.notion_database_id)
    postiz = PostizClient(settings.postiz_url, settings.postiz_api_key)
    plans: dict[tuple[str, str], MonthPlan] = {}
    done = 0
    for item in board.approved():
        # Un error en una pieza (cliente sin canales, plan borrado, Postiz caído) no frena las demás;
        # la fila sigue en Aprobado y se reintenta en la siguiente pasada.
        try:
            client, month, piece_id = item["key"].split("/")
            if (client, month) not in plans:
                plans[(client, month)] = MonthPlan.model_validate_json(
                    plan_path(settings, client, month).read_text(encoding="utf-8"))
            piece = next(p for p in plans[(client, month)].pieces if p.id == piece_id)
            when = soon(settings.timezone) if now else None
            result = postiz.schedule(Brand.load(settings.clients_dir, client), piece, settings.timezone, when)
            board.mark_scheduled(item["page_id"], describe(result))
        except Exception:
            log.exception("No se pudo programar %s", item["key"])
            continue
        log.info("Programada %s", item["key"])
        done += 1
    return done

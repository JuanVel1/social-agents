from __future__ import annotations

import logging
from pathlib import Path

from . import progress
from .approval import TYPE_LABELS as PIECE_LABELS
from .approval import NotionBoard
from .config import Settings
from .llm import get_provider
from .models import Brand, MonthPlan
from .llm.base import NoQuotaError
from .photos import PHOTO, assign_photos, bank_dir, describe_visuals, generate_photos, usable_photos
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


def run_month(settings: Settings, client: str, month: str, dry_run: bool, replace: bool = False) -> MonthPlan:
    """Escribe, diseña y envía a aprobación la parrilla del mes.

    replace: antes de enviarla, archiva en Notion las filas de ese mes que siguen en Pendiente (la parrilla
    anterior). Las que ya se aprobaron, programaron o tienen cambios se quedan y su pieza nueva no se envía.
    Se hace al final, cuando todo está generado: si algo falla antes, Notion queda como estaba.
    """
    with progress.step("marca"):
        brand = Brand.load(settings.clients_dir, client)
        llm = get_provider(settings)
        progress.detail(f"{brand.name} · {month}")
        if brand.style.name:
            progress.metric("Estilo", brand.style.name)
    from .references import apply_reference_photos, load_references

    with progress.step("referencias"):
        references = load_references(settings.clients_dir, brand.slug)
        progress.metric("Plantillas", len(references))
        progress.detail(f"{len(references)} plantillas aprobadas" if references else "Sin plantillas: diseño estándar")
    plan = plan_month(brand, month, llm, references)
    if not brand.style.photos:
        for key in ("asignar_fotos", "fotos_ia", "poses_referencia"):
            progress.skip(key, "La marca no usa fotos")
    elif not brand.style.generate_photos:
        progress.skip("fotos_ia", "La marca no genera fotos con IA")
    if brand.style.photos:
        photos = usable_photos(settings.clients_dir, brand.slug, PHOTO)
        if photos:
            assign_photos(plan, photos, llm)
            if brand.style.generate_photos:
                try:
                    generate_photos(plan, brand, settings.clients_dir, llm)
                except NoQuotaError as exc:
                    # La parrilla sale igual, con las fotos originales del banco.
                    log.warning("Sin fotos generadas para %s: %s", client, exc)
        else:
            progress.skip("asignar_fotos", "Banco de fotos vacío")
            progress.skip("fotos_ia", "Banco de fotos vacío")
            log.warning("%s usa fotos pero su banco está vacío: ejecuta social-agent fotos --client %s", client, client)
        with progress.step("poses_referencia"):
            changed = apply_reference_photos(plan.pieces, references, bank_dir(settings.clients_dir, brand.slug),
                                             photos if brand.style.photos and photos else [])
            progress.metric("Piezas ajustadas", changed)
    if brand.style.ai_images:
        from .covers import generate_covers

        generate_covers(plan, brand, settings.clients_dir, references, llm)
    else:
        progress.skip("imagenes_ia", "Opción desactivada (style.ai_images)")
    render_plan(plan, brand, settings.templates_dir, settings.clients_dir, settings.out_dir)
    with progress.step("guardar"):
        progress.output(save_plan(settings, plan))
        summary = write_summary(settings, plan)
        progress.output(summary)
        for t, label in TYPE_LABELS.items():
            progress.metric(label, plan.count(t))
        log.info("Parrilla lista: %s", summary)
    if dry_run:
        progress.skip("archivar", "Simulación (--dry-run)")
        progress.skip("aprobacion", "Simulación (--dry-run)")
    else:
        if not replace:
            progress.skip("archivar", "Sin --reemplazar")
        pieces = plan.pieces
        if replace:
            kept = archive_pending(settings, client, month)
            pieces = [p for p in plan.pieces if f"{client}/{month}/{p.id}" not in kept]
        send_for_approval(settings, plan, pieces)
    return plan


def archive_pending(settings: Settings, client: str, month: str) -> dict[str, str]:
    """Archiva las filas del mes en Pendiente. Devuelve las que no tocó: {ID: Estado}."""
    with progress.step("archivar"):
        board = NotionBoard(settings.notion_token, settings.notion_database_id)
        kept = {}
        rows = board.rows(f"{client}/{month}/")
        progress.set_total(len(rows))
        for key, (page_id, state) in rows.items():
            if state == "Pendiente":
                board.archive(page_id)
                progress.advance(item=key, note="archivada")
            else:
                kept[key] = state
                progress.advance(item=key, status="skipped", note=state)
    log.info("Parrilla anterior de %s %s archivada en Notion%s", client, month,
             f"; se quedan {len(kept)} filas ya revisadas: {', '.join(kept)}" if kept else "")
    return kept


def send_for_approval(settings: Settings, plan: MonthPlan, pieces: list) -> None:
    """Sube las imágenes a Postiz y crea una fila en Notion (Pendiente) por pieza."""
    with progress.step("aprobacion", total=len(pieces)):
        postiz = PostizClient(settings.postiz_url, settings.postiz_api_key)
        board = NotionBoard(settings.notion_token, settings.notion_database_id)
        for piece in pieces:
            progress.activity(f"Subiendo {piece.id} a Postiz ({len(piece.images)} imágenes)")
            piece.postiz_media = [postiz.upload(Path(p)) for p in piece.images]
            piece.image_urls = [postiz.public_url(m) for m in piece.postiz_media]
            progress.activity(f"Creando la fila de {piece.id} en Notion")
            board.add_piece(plan.client, plan.month, piece)
            save_plan(settings, plan)
            progress.advance(item=piece.id)
    log.info("%s piezas enviadas a Notion para aprobación", len(pieces))


def redesign_month(settings: Settings, client: str, month: str, dry_run: bool) -> dict:
    """Rehace las imágenes de una parrilla ya escrita (mismos textos) con una foto nueva generada con IA
    por pieza.

    Sin dry_run, reemplaza en Notion solo las filas que siguen en Pendiente: las archiva y las crea de
    nuevo con las imágenes nuevas. Las que ya se aprobaron, programaron o tienen cambios no se tocan.
    """
    with progress.step("cargar"):
        brand = Brand.load(settings.clients_dir, client)
        plan = MonthPlan.model_validate_json(plan_path(settings, client, month).read_text(encoding="utf-8"))
        llm = get_provider(settings)
        progress.detail(f"{brand.name} · {month} · {len(plan.pieces)} piezas")
    progress.skip("asignar_fotos", "Se generan fotos nuevas")
    described = describe_visuals(plan, brand, llm)
    photos = generate_photos(plan, brand, settings.clients_dir, llm)
    render_plan(plan, brand, settings.templates_dir, settings.clients_dir, settings.out_dir)
    with progress.step("guardar"):
        progress.output(save_plan(settings, plan))
        progress.output(write_summary(settings, plan))
    result = {"visuales": described, **photos, "reemplazadas": 0, "sin_tocar": []}
    if dry_run:
        return result

    with progress.step("notion", total=len(plan.pieces)):
        board = NotionBoard(settings.notion_token, settings.notion_database_id)
        rows = board.rows(f"{client}/{month}/")
        replace = []
        for piece in plan.pieces:
            page_id, state = rows.get(f"{client}/{month}/{piece.id}", ("", "Pendiente"))
            if state != "Pendiente":
                result["sin_tocar"].append(f"{piece.id} ({state})")
                progress.advance(item=piece.id, status="skipped", note=state)
                continue
            if page_id:
                board.archive(page_id)
            replace.append(piece)
            progress.advance(item=piece.id, note="se reemplaza")
    send_for_approval(settings, plan, replace)
    result["reemplazadas"] = len(replace)
    return result


def publish_approved(settings: Settings, now: bool = False) -> int:
    """Programa en Postiz lo aprobado. Con now=True usa unos minutos desde ahora (pruebas)."""
    board = NotionBoard(settings.notion_token, settings.notion_database_id)
    postiz = PostizClient(settings.postiz_url, settings.postiz_api_key)
    plans: dict[tuple[str, str], MonthPlan] = {}
    with progress.step("aprobados"):
        approved = list(board.approved())
        progress.metric("Aprobadas", len(approved))
    if not approved:
        progress.skip("programar", "Nada aprobado")
        return 0
    with progress.step("programar", total=len(approved)):
        return _schedule(settings, board, postiz, approved, plans, now)


def _schedule(settings: Settings, board, postiz, approved: list[dict], plans: dict, now: bool) -> int:
    done = 0
    for item in approved:
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
        except Exception as exc:
            log.exception("No se pudo programar %s", item["key"])
            progress.advance(item=item["key"], status="error", note=str(exc)[:200])
            continue
        log.info("Programada %s", item["key"])
        progress.advance(item=item["key"])
        done += 1
    return done

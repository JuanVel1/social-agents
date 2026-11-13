from __future__ import annotations

import logging
from datetime import date, datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from . import progress
from .config import Settings
from .llm.base import NoQuotaError
from .pipeline import publish_approved, run_month

log = logging.getLogger(__name__)


def next_month(today: date) -> str:
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return f"{year}-{month:02d}"


def clients(settings: Settings) -> list[str]:
    return sorted(p.name for p in settings.clients_dir.iterdir() if (p / "brand.yaml").exists() and p.name != "demo")


def runs_dir(settings: Settings):
    return settings.out_dir / "_runs"


def refresh_photos(settings: Settings, client: str, group: str = "") -> None:
    """Añade al banco las fotos de lo publicado este mes. Si falla, la parrilla sale con el banco que había."""
    from .llm import get_provider
    from .models import Brand
    from .photos import refresh_bank

    brand = Brand.load(settings.clients_dir, client)
    if not brand.style.photos:
        return
    try:
        with progress.run("fotos", runs_dir(settings), client=client, group=group):
            refresh_bank(settings, brand, get_provider(settings))
    except Exception:
        log.exception("No se pudo actualizar el banco de fotos de %s", client)


def refresh_moodboard(settings: Settings, client: str, group: str = "") -> None:
    """Relee el moodboard de Pinterest. Si falla, la parrilla sale con la dirección visual que había."""
    from .llm import get_provider
    from .models import Brand
    from .moodboard import refresh_moodboard as refresh

    brand = Brand.load(settings.clients_dir, client)
    if not brand.moodboard.board:
        return
    from .moodboard import refresh_pin_references

    llm = get_provider(settings)
    try:
        with progress.run("moodboard", runs_dir(settings), client=client, group=group):
            progress.skip("revalidar", "Solo con --revalidar")
            refresh(settings, brand, llm)
            # Los pines nuevos se convierten en plantillas de la marca antes de la parrilla.
            refresh_pin_references(settings, Brand.load(settings.clients_dir, client), llm)
    except Exception:
        log.exception("No se pudo leer el moodboard de %s", client)


def monthly_plans(settings: Settings) -> None:
    month = next_month(date.today())
    group = f"parrilla-{month}-{datetime.now():%Y%m%d-%H%M}"
    for client in clients(settings):
        refresh_photos(settings, client, group)
        refresh_moodboard(settings, client, group)
        try:
            with progress.run("plan", runs_dir(settings), args={"month": month}, client=client, group=group):
                run_month(settings, client, month, dry_run=False)
        except Exception:
            log.exception("Falló la parrilla de %s para %s", client, month)


def onboarding_pass(settings: Settings) -> None:
    """Rellena las filas nuevas del formulario con su manual y activa las aprobadas."""
    from .llm import get_provider
    from .onboarding import OnboardingBoard, run

    try:
        board = OnboardingBoard(settings.notion_token, settings.notion_onboarding_database_id)
        with progress.run("onboarding run", runs_dir(settings), discard_if_empty=True):
            result = run(board, settings.clients_dir, lambda: get_provider(settings))
        if any(result.values()):
            log.info("Onboarding: %s por revisar, %s clientes activados", result["por_revisar"], result["activos"])
    except Exception:
        log.exception("Falló la pasada de onboarding")


def publish_pass(settings: Settings) -> int:
    """Pasada de publicación: en el panel solo queda si programó (o falló) algo."""
    with progress.run("publish", runs_dir(settings), discard_if_empty=True):
        return publish_approved(settings)


def register_jobs(scheduler, settings: Settings) -> list[str]:
    # La parrilla del mes siguiente sale el 20 para dejar tiempo de aprobación.
    scheduler.add_job(monthly_plans, "cron", day=20, hour=7, args=[settings], id="parrilla")
    # Lo aprobado en Notion se procesa en cuanto se ve: cada pocos segundos, sin solaparse
    # (si una pasada tarda más que el intervalo, la siguiente se salta) y la primera al arrancar.
    every = {"trigger": "interval", "seconds": settings.approval_poll_seconds, "args": [settings],
             "max_instances": 1, "coalesce": True, "next_run_time": datetime.now()}
    scheduler.add_job(publish_pass, id="publicar", **every)
    if settings.notion_onboarding_database_id:
        scheduler.add_job(onboarding_pass, id="onboarding", **every)
    return [job.id for job in scheduler.get_jobs()]


def serve(settings: Settings) -> None:
    # Con una pasada por minuto, las peticiones HTTP y el "Running job" del programador llenarían el log.
    for noisy in ("httpx", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    scheduler = BlockingScheduler(timezone=settings.timezone)
    jobs = register_jobs(scheduler, settings)
    log.info("Agente en marcha: parrilla el día 20 a las 07:00; aprobados%s cada %s s",
             " y onboarding" if "onboarding" in jobs else "", settings.approval_poll_seconds)
    scheduler.start()

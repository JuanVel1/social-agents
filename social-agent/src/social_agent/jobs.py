from __future__ import annotations

import logging
from datetime import date

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import Settings
from .pipeline import publish_approved, run_month

log = logging.getLogger(__name__)


def next_month(today: date) -> str:
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return f"{year}-{month:02d}"


def clients(settings: Settings) -> list[str]:
    return sorted(p.name for p in settings.clients_dir.iterdir() if (p / "brand.yaml").exists() and p.name != "demo")


def monthly_plans(settings: Settings) -> None:
    month = next_month(date.today())
    for client in clients(settings):
        try:
            run_month(settings, client, month, dry_run=False)
        except Exception:
            log.exception("Falló la parrilla de %s para %s", client, month)


def serve(settings: Settings) -> None:
    scheduler = BlockingScheduler(timezone=settings.timezone)
    # La parrilla del mes siguiente sale el 20 para dejar tiempo de aprobación.
    scheduler.add_job(monthly_plans, "cron", day=20, hour=7, args=[settings], id="parrilla")
    scheduler.add_job(publish_approved, "cron", minute=5, args=[settings], id="publicar")
    log.info("Agente en marcha: parrilla el día 20 a las 07:00, revisión de aprobados cada hora")
    scheduler.start()

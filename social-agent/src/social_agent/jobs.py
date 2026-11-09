from __future__ import annotations

import logging
from datetime import date, datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import Settings
from .llm.base import NoQuotaError
from .pipeline import publish_approved, run_month

log = logging.getLogger(__name__)


def next_month(today: date) -> str:
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return f"{year}-{month:02d}"


def clients(settings: Settings) -> list[str]:
    return sorted(p.name for p in settings.clients_dir.iterdir() if (p / "brand.yaml").exists() and p.name != "demo")


def refresh_photos(settings: Settings, client: str) -> None:
    """Añade al banco las fotos de lo publicado este mes. Si falla, la parrilla sale con el banco que había."""
    from .llm import get_provider
    from .models import Brand
    from .photos import refresh_bank

    brand = Brand.load(settings.clients_dir, client)
    if not brand.style.photos:
        return
    try:
        refresh_bank(settings, brand, get_provider(settings))
    except Exception:
        log.exception("No se pudo actualizar el banco de fotos de %s", client)


def monthly_plans(settings: Settings) -> None:
    month = next_month(date.today())
    for client in clients(settings):
        refresh_photos(settings, client)
        try:
            run_month(settings, client, month, dry_run=False)
        except Exception:
            log.exception("Falló la parrilla de %s para %s", client, month)


def onboarding_pass(settings: Settings) -> None:
    """Rellena las filas nuevas del formulario con su manual y activa las aprobadas."""
    from .llm import get_provider
    from .onboarding import OnboardingBoard, run

    try:
        board = OnboardingBoard(settings.notion_token, settings.notion_onboarding_database_id)
        result = run(board, settings.clients_dir, lambda: get_provider(settings))
        if any(result.values()):
            log.info("Onboarding: %s por revisar, %s clientes activados", result["por_revisar"], result["activos"])
    except Exception:
        log.exception("Falló la pasada de onboarding")


def character_pass(settings: Settings) -> None:
    """Avanza el personaje con la cuota de GPU que haya (la gratuita se recarga sola).

    Sin maestro elegido: genera las variantes a partir de style.character_photo y espera a que alguien
    elija una (`social-agent personaje elegir`). Con maestro: genera las poses que falten.
    """
    from .character import QwenEditor, character_dir, fill_poses, make_masters, master_path, pending_poses
    from .llm import get_provider
    from .models import Brand
    from .photos import bank_dir, load_bank

    for client in clients(settings):
        brand = Brand.load(settings.clients_dir, client)
        if not brand.style.character:
            continue
        editor = QwenEditor(settings.huggingface_token, settings.character_space)
        try:
            if not master_path(settings.clients_dir, client).exists():
                photo = bank_dir(settings.clients_dir, client) / brand.style.character_photo
                if brand.style.character_photo and photo.is_file():
                    make_masters(settings.clients_dir, brand, photo, editor)
                    log.info("Variantes del personaje de %s en %s: elige una con social-agent personaje elegir "
                             "--client %s --variante <n>", client, character_dir(settings.clients_dir, client), client)
            elif pending_poses(load_bank(settings.clients_dir, client)):
                fill_poses(settings.clients_dir, brand, get_provider(settings), editor)
        except NoQuotaError as exc:
            log.info("Personaje de %s en pausa: %s", client, exc)
        except Exception:
            log.exception("Falló la tanda del personaje de %s", client)


def register_jobs(scheduler, settings: Settings) -> list[str]:
    # La parrilla del mes siguiente sale el 20 para dejar tiempo de aprobación.
    scheduler.add_job(monthly_plans, "cron", day=20, hour=7, args=[settings], id="parrilla")
    # Lo aprobado en Notion se procesa en cuanto se ve: cada pocos segundos, sin solaparse
    # (si una pasada tarda más que el intervalo, la siguiente se salta) y la primera al arrancar.
    every = {"trigger": "interval", "seconds": settings.approval_poll_seconds, "args": [settings],
             "max_instances": 1, "coalesce": True, "next_run_time": datetime.now()}
    scheduler.add_job(publish_approved, id="publicar", **every)
    if settings.notion_onboarding_database_id:
        scheduler.add_job(onboarding_pass, id="onboarding", **every)
    if settings.huggingface_token:
        scheduler.add_job(character_pass, "interval", hours=settings.character_poll_hours, args=[settings],
                          id="personaje", max_instances=1, coalesce=True, next_run_time=datetime.now())
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

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    gemini_api_key: str
    gemini_model_quality: str
    gemini_model_bulk: str
    gemini_fallback_models: tuple[str, ...]
    gemini_image_model: str
    gemini_max_rpm: int
    # Moodboard de Pinterest: opcional; sin él, el token se lee del canal de Pinterest en Postiz.
    pinterest_access_token: str
    notion_token: str
    notion_database_id: str
    notion_onboarding_database_id: str
    postiz_url: str
    postiz_api_key: str
    timezone: str
    approval_poll_seconds: int
    # Contenedor de Postgres de Postiz: de ahí sale el token de Instagram para leer las publicaciones.
    postiz_db_container: str
    postiz_db_user: str
    postiz_db_name: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    clients_dir: Path
    templates_dir: Path
    out_dir: Path
    # Segunda clave de Gemini (otro proyecto): entra cuando la principal responde sin cuota o saturada.
    gemini_api_key_backup: str = ""


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    env = os.environ.get
    return Settings(
        llm_provider=env("LLM_PROVIDER", "gemini"),
        gemini_api_key=env("GEMINI_API_KEY", ""),
        gemini_model_quality=env("GEMINI_MODEL_QUALITY", "gemini-3.5-flash"),
        gemini_model_bulk=env("GEMINI_MODEL_BULK", "gemini-3.5-flash-lite"),
        gemini_fallback_models=tuple(m.strip() for m in env(
            "GEMINI_FALLBACK_MODELS", "gemini-3-flash-preview,gemini-3.1-flash-lite").split(",") if m.strip()),
        gemini_image_model=env("GEMINI_IMAGE_MODEL", "gemini-3-pro-image"),
        gemini_max_rpm=int(env("GEMINI_MAX_RPM", "8")),
        pinterest_access_token=env("PINTEREST_ACCESS_TOKEN", ""),
        notion_token=env("NOTION_TOKEN", ""),
        notion_database_id=env("NOTION_DATABASE_ID", ""),
        notion_onboarding_database_id=env("NOTION_ONBOARDING_DATABASE_ID", ""),
        postiz_url=env("POSTIZ_URL", "http://localhost:5000").rstrip("/"),
        postiz_api_key=env("POSTIZ_API_KEY", ""),
        timezone=env("TIMEZONE", "America/Bogota"),
        approval_poll_seconds=int(env("APPROVAL_POLL_SECONDS", "60")),
        postiz_db_container=env("POSTIZ_DB_CONTAINER", "postiz-postgres"),
        postiz_db_user=env("POSTIZ_DB_USER", "postiz-user"),
        postiz_db_name=env("POSTIZ_DB_NAME", "postiz-db-local"),
        whisper_model=env("WHISPER_MODEL", "small"),
        whisper_device=env("WHISPER_DEVICE", "cpu"),
        whisper_compute_type=env("WHISPER_COMPUTE_TYPE", "int8"),
        clients_dir=ROOT / "clients",
        templates_dir=ROOT / "templates",
        out_dir=ROOT / "out",
        gemini_api_key_backup=env("GEMINI_API_KEY_RESPALD") or env("GEMINI_API_KEY_RESPALDO", ""),
    )

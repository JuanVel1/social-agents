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
    gemini_max_rpm: int
    notion_token: str
    notion_database_id: str
    postiz_url: str
    postiz_api_key: str
    timezone: str
    clients_dir: Path
    templates_dir: Path
    out_dir: Path


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
        gemini_max_rpm=int(env("GEMINI_MAX_RPM", "8")),
        notion_token=env("NOTION_TOKEN", ""),
        notion_database_id=env("NOTION_DATABASE_ID", ""),
        postiz_url=env("POSTIZ_URL", "http://localhost:5000").rstrip("/"),
        postiz_api_key=env("POSTIZ_API_KEY", ""),
        timezone=env("TIMEZONE", "America/Bogota"),
        clients_dir=ROOT / "clients",
        templates_dir=ROOT / "templates",
        out_dir=ROOT / "out",
    )

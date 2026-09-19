"""Konfiguracja aplikacji (ENV + pliki YAML w katalogu config/)."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OGL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- baza ---
    database_url: str = f"sqlite:///{DATA_DIR / 'ogloszenia.db'}"

    # --- region ---
    default_voivodeship: str = "opolskie"

    # --- sieć ---
    http_timeout: float = 25.0
    global_concurrency: int = 8
    per_host_concurrency: int = 2
    respect_robots: bool = True
    user_agent: str = "ogloszenia-bot/1.0 (+https://github.com/wisniabobo/ogloszenia)"
    proxy_url: str | None = None
    min_delay_s: float = 0.8
    max_retries: int = 3

    # --- prywatność / RODO ---
    mask_phones: bool = True
    store_phone_hash_only: bool = False
    phone_hash_salt: str = "zmien-mnie"
    retention_days: int = 540  # po tylu dniach oferty archiwalne są czyszczone

    # --- powiadomienia ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None

    # --- web ---
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # --- harmonogram ---
    scan_interval_minutes: int = 10
    slow_source_interval_minutes: int = 180

    @property
    def data_dir(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@functools.lru_cache(maxsize=8)
def load_yaml(name: str) -> dict[str, Any]:
    """Wczytuje plik YAML z katalogu config/."""
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources_config() -> dict[str, Any]:
    return load_yaml("sources.yaml")


def regions_config() -> dict[str, Any]:
    return load_yaml("regions_opolskie.yaml")


def agencies_config() -> dict[str, Any]:
    return load_yaml("agencies_opolskie.yaml")

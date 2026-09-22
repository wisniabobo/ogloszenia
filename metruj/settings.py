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
    database_url: str = f"sqlite:///{DATA_DIR / 'metruj.db'}"

    # --- zasięg ---
    #: Domyślnie serwis zbiera oferty z **całej Polski**. Pojedyncze
    #: województwo (albo ich lista po przecinku) zawęża skan — przydaje się
    #: przy własnej, małej instancji, ale nie jest już założeniem projektu.
    voivodeships: str = "wszystkie"
    #: Zostawione dla zgodności ze starymi plikami .env i skryptami wdrożenia.
    default_voivodeship: str = ""

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

    # --- opcjonalne integracje ---
    apify_token: str | None = None
    nominatim_url: str | None = None

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
    #: Hasło do zapisu: bez niego schowek i alerty są tylko do czytania
    #: z internetu (z sieci lokalnej działają bez hasła, żeby nie utrudniać
    #: pracy na własnym komputerze). Strona jest publiczna, więc bez tego
    #: każdy mógł skasować cudze poszukiwania albo założyć własne — a alerty
    #: z nich i tak lecą na jeden, Twój kanał Telegrama.
    admin_token: str = ""
    #: Ile razy na godzinę jeden adres IP może odsłonić numer telefonu.
    #: Numery są w ogłoszeniach publiczne, ale cała ich baza już nie.
    phone_reveal_limit: int = 60
    #: Adres publiczny — do linków kanonicznych, sitemapy i danych OpenGraph.
    site_url: str = "https://bot.wisnia.dev"
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # --- harmonogram ---
    scan_interval_minutes: int = 10
    slow_source_interval_minutes: int = 180

    @property
    def data_dir(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR

    @property
    def scope(self) -> list[str]:
        """Województwa objęte skanem — pusta lista znaczy „cała Polska"."""
        raw = (self.voivodeships or "").strip().lower()
        if self.default_voivodeship and raw in ("", "wszystkie"):
            raw = self.default_voivodeship          # stary OGL_DEFAULT_VOIVODESHIP
        if raw in ("", "wszystkie", "polska", "all", "*"):
            return []
        return [part.strip() for part in raw.split(",") if part.strip()]


#: Historycznie wszystkie zmienne miały przedrostek `OGL_`. Po zmianie nazwy
#: naturalny jest `METRUJ_`, ale działające instalacje mają w `.env` stary
#: zapis i nie ma powodu ich psuć — przyjmujemy oba, przy czym wpis jawny
#: w starym formacie ma pierwszeństwo, bo ktoś go tam świadomie postawił.
LEGACY_PREFIX, PREFIX = "OGL_", "METRUJ_"


def _merge_env_prefixes() -> None:
    import os

    for name, value in list(os.environ.items()):
        if name.startswith(PREFIX):
            os.environ.setdefault(LEGACY_PREFIX + name[len(PREFIX):], value)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    _merge_env_prefixes()
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
    """Parametry portali dla każdego z 16 województw (config/regions.yaml)."""
    return load_yaml("regions.yaml")


def agencies_config() -> dict[str, Any]:
    """Ziarno rejestru biur — rejestr i tak rośnie sam z katalogu Otodom."""
    return load_yaml("agencies.yaml")


@functools.lru_cache(maxsize=1)
def regions() -> list[dict[str, Any]]:
    return list(regions_config().get("wojewodztwa", []))


@functools.lru_cache(maxsize=32)
def region(name: str | None) -> dict[str, Any]:
    """Wpis regionu po nazwie albo kluczu — „śląskie" i „slaskie" to to samo."""
    if not name:
        return {}
    from .utils.text import deaccent

    key = deaccent(name).strip().lower()
    for entry in regions():
        if deaccent(str(entry.get("nazwa", ""))).lower() == key or entry.get("klucz") == key:
            return entry
    return {}


def region_names(scope: list[str] | None = None) -> list[str]:
    """Nazwy województw do objęcia skanem (domyślnie wszystkie 16)."""
    if not scope:
        return [str(entry["nazwa"]) for entry in regions()]
    out = []
    for name in scope:
        entry = region(name)
        if entry:
            out.append(str(entry["nazwa"]))
    return out

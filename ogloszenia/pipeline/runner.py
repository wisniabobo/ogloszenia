"""Orkiestracja przebiegu: źródła -> scrapery -> normalizacja -> baza -> alerty."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import (
    Listing,
    ListingStatus,
    OfferKind,
    Phone,
    PriceHistory,
    ScanRun,
    Source,
    utcnow,
)
from ..scrapers import SCRAPERS, ScrapeContext
from ..scrapers.base import RawListing
from ..scrapers.generic_html import GenericHtmlScraper
from ..settings import get_settings, sources_config
from ..utils.http import HttpClient
from ..utils.phones import PhoneNumber
from .dedup import compute_fingerprints, link_duplicates
from .enrich import enrich_listing, recount_agencies
from .normalize import normalize

log = logging.getLogger("ogloszenia.runner")

#: po ilu przebiegach bez zobaczenia oferty uznajemy ją za zdjętą
MISSING_RUNS_BEFORE_REMOVAL = 2


@dataclass
class SourceResult:
    source_key: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    price_changes: int = 0
    removed: int = 0
    errors: int = 0
    ok: bool = True
    message: str = ""


@dataclass
class ScanResult:
    sources: list[SourceResult] = field(default_factory=list)
    new_listing_ids: list[int] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "fetched": sum(s.fetched for s in self.sources),
            "new": sum(s.new for s in self.sources),
            "updated": sum(s.updated for s in self.sources),
            "duplicates": sum(s.duplicates for s in self.sources),
            "price_changes": sum(s.price_changes for s in self.sources),
            "removed": sum(s.removed for s in self.sources),
            "errors": sum(s.errors for s in self.sources),
        }


# --------------------------------------------------------------------------- #
# Synchronizacja rejestru źródeł
# --------------------------------------------------------------------------- #
def sync_sources(session: Session) -> int:
    """Wczytuje config/sources.yaml do tabeli `sources` (idempotentnie)."""
    cfg = sources_config()
    defaults = cfg.get("defaults", {})
    count = 0
    for entry in cfg.get("sources", []):
        key = entry.get("key")
        if not key:
            continue
        source = session.scalar(select(Source).where(Source.key == key))
        if source is None:
            source = Source(key=key)
            session.add(source)
            count += 1
        source.name = entry.get("name", key)
        source.kind = OfferKind(entry.get("kind", "nieruchomosc"))
        source.category = entry.get("category", "portal")
        source.base_url = entry.get("base_url")
        source.scraper = entry.get("scraper", "generic_html")
        source.coverage = entry.get("coverage", "krajowy")
        source.interval_minutes = int(
            entry.get("interval_minutes", defaults.get("interval_minutes", 15))
        )
        source.notes = entry.get("notes")
        source.config = entry.get("config", {}) or {}
        if source.enabled is None:
            source.enabled = bool(entry.get("enabled", False))
        elif "enabled" in entry and source.last_run_at is None:
            # dopóki źródło nie było uruchomione, YAML jest źródłem prawdy
            source.enabled = bool(entry["enabled"])
    return count


def _build_scraper(source: Source, client: HttpClient):
    cls = SCRAPERS.get(source.scraper or "")
    if cls is None or cls is GenericHtmlScraper:
        return GenericHtmlScraper(
            client, source.config, source_key=source.key, kind=source.kind, name=source.name
        )
    return cls(client, source.config)


# --------------------------------------------------------------------------- #
# Zapis pojedynczej oferty
# --------------------------------------------------------------------------- #
def _sync_phones(session: Session, listing: Listing, phones: list[PhoneNumber]) -> None:
    settings = get_settings()
    existing = {p.hashed for p in listing.phones}
    for phone in phones:
        if phone.hashed in existing:
            continue
        listing.phones.append(
            Phone(
                e164="" if settings.store_phone_hash_only else phone.e164,
                national="" if settings.store_phone_hash_only else phone.national,
                masked=phone.masked,
                hashed=phone.hashed,
                origin=phone.origin,
            )
        )


def upsert_listing(
    session: Session, data: dict, phones: list[PhoneNumber], source: Source
) -> tuple[Listing, str, bool]:
    """Wstawia albo aktualizuje ofertę.

    Zwraca (oferta, 'new'|'updated'|'unchanged', czy_zmiana_ceny).
    """
    compute_fingerprints(data, phones)
    enrich_listing(session, data, phones)

    listing = session.scalar(
        select(Listing).where(
            Listing.source_key == data["source_key"],
            Listing.external_id == data["external_id"],
        )
    )
    price_changed = False

    if listing is None:
        listing = Listing(**data, source_id=source.id, initial_price=data.get("price"))
        session.add(listing)
        session.flush()
        _sync_phones(session, listing, phones)
        if data.get("price"):
            session.add(PriceHistory(listing_id=listing.id, price=data["price"]))
        return listing, "new", False

    changed = False
    old_price = listing.price
    for key, value in data.items():
        if key in {"raw", "extra", "first_seen_at"}:
            continue
        if value in (None, "", [], {}):
            continue
        if getattr(listing, key, None) != value:
            setattr(listing, key, value)
            changed = True

    if data.get("price") and old_price and abs(data["price"] - old_price) > 0.5:
        session.add(
            PriceHistory(listing_id=listing.id, price=data["price"], previous_price=old_price)
        )
        price_changed = True
    if data.get("raw"):
        listing.raw = data["raw"]
    if data.get("extra"):
        listing.extra = {**(listing.extra or {}), **data["extra"]}

    listing.last_seen_at = utcnow()
    listing.status = ListingStatus.AKTYWNA
    listing.removed_at = None
    _sync_phones(session, listing, phones)
    return listing, ("updated" if changed else "unchanged"), price_changed


# --------------------------------------------------------------------------- #
# Przebieg jednego źródła
# --------------------------------------------------------------------------- #
#: komunikat dla źródeł, których wyniki powstają dopiero w przeglądarce
JS_REQUIRED_MESSAGE = (
    "źródło renderuje wyniki po stronie klienta — potrzebny silnik JS "
    "(patrz README: Źródła wymagające przeglądarki)"
)


async def _collect(source: Source, client: HttpClient, ctx: ScrapeContext) -> tuple[list[RawListing], str]:
    if (source.config or {}).get("requires_js"):
        # lepiej powiedzieć wprost, że się nie da, niż zwrócić ciche zero
        return [], JS_REQUIRED_MESSAGE
    scraper = _build_scraper(source, client)
    items: list[RawListing] = []
    error = ""
    try:
        async for raw in scraper.run(ctx):
            raw.source_key = raw.source_key or source.key
            items.append(raw)
    except Exception as exc:  # scraper nie może wywrócić całego skanu
        error = f"{type(exc).__name__}: {exc}"
        log.warning("Źródło %s zakończyło się błędem: %s", source.key, error)
    return items, error


def _persist(source_key: str, items: list[RawListing], error: str,
             voivodeship: str, require_region: bool) -> tuple[SourceResult, list[int]]:
    result = SourceResult(source_key=source_key, fetched=len(items), ok=not error, message=error)
    new_ids: list[int] = []

    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.key == source_key))
        if source is None:
            result.ok = False
            result.message = "brak źródła w rejestrze"
            return result, []

        run = ScanRun(source_key=source_key)
        session.add(run)
        session.flush()

        for raw in items:
            try:
                normalized = normalize(raw, voivodeship=voivodeship, require_region=require_region)
            except Exception as exc:
                log.debug("Normalizacja odrzuciła ofertę %s: %s", raw.url, exc)
                result.errors += 1
                continue
            if normalized is None:
                continue
            try:
                listing, action, price_changed = upsert_listing(
                    session, normalized.data, normalized.phones, source
                )
            except Exception as exc:
                session.rollback()
                log.debug("Zapis oferty %s nie powiódł się: %s", raw.url, exc)
                result.errors += 1
                continue

            if action == "new":
                result.new += 1
                new_ids.append(listing.id)
                result.duplicates += link_duplicates(session, listing)
            elif action == "updated":
                result.updated += 1
            if price_changed:
                result.price_changes += 1

        # oznaczanie ofert zdjętych ze źródła
        if not error and items:
            result.removed = _mark_missing(session, source)

        source.last_run_at = utcnow()
        source.total_listings = int(
            session.scalar(select(func.count(Listing.id)).where(Listing.source_key == source_key)) or 0
        )
        if error:
            source.last_error = error[:1000]
        else:
            source.last_ok_at = utcnow()
            source.last_error = None

        run.finished_at = utcnow()
        run.fetched = result.fetched
        run.new = result.new
        run.updated = result.updated
        run.duplicates = result.duplicates
        run.errors = result.errors
        run.ok = result.ok
        run.message = result.message[:1000] or None

    return result, new_ids


def _mark_missing(session: Session, source: Source) -> int:
    """Wygasza oferty, których nie widzieliśmy w kilku kolejnych przebiegach.

    Dzięki temu licznik „ile oferta stoi" zatrzymuje się w momencie zdjęcia
    ogłoszenia, zamiast rosnąć w nieskończoność. Wygaszamy dopiero po
    MISSING_RUNS_BEFORE_REMOVAL przebiegach, żeby chwilowy błąd portalu nie
    skasował połowy bazy.
    """
    window = max(source.interval_minutes, 5) * MISSING_RUNS_BEFORE_REMOVAL
    stale_before = utcnow() - timedelta(minutes=window)
    stale = session.scalars(
        select(Listing)
        .where(
            Listing.source_key == source.key,
            Listing.status == ListingStatus.AKTYWNA,
            Listing.last_seen_at < stale_before,
        )
        .limit(1000)
    )
    count = 0
    for listing in stale:
        listing.status = ListingStatus.NIEAKTYWNA
        listing.removed_at = listing.removed_at or utcnow()
        count += 1
    return count


# --------------------------------------------------------------------------- #
# API publiczne
# --------------------------------------------------------------------------- #
async def run_scan(
    *,
    only: list[str] | None = None,
    categories: list[str] | None = None,
    max_pages: int | None = None,
    max_items: int | None = None,
    voivodeship: str | None = None,
    require_region: bool = True,
    fetch_details: bool = True,
    include_disabled: bool = False,
) -> ScanResult:
    """Uruchamia skan wybranych źródeł równolegle."""
    settings = get_settings()
    voivodeship = voivodeship or settings.default_voivodeship

    with session_scope() as session:
        sync_sources(session)

    with session_scope() as session:
        stmt = select(Source)
        if not include_disabled:
            stmt = stmt.where(Source.enabled.is_(True))
        if only:
            stmt = stmt.where(Source.key.in_(only))
        if categories:
            stmt = stmt.where(Source.category.in_(categories))
        sources = list(session.scalars(stmt))
        session.expunge_all()

    if not sources:
        return ScanResult()

    ctx = ScrapeContext(
        voivodeship=voivodeship,
        max_pages=max_pages or int(sources_config().get("defaults", {}).get("max_pages", 5)),
        max_items=max_items or int(sources_config().get("defaults", {}).get("max_items", 400)),
        fetch_details=fetch_details,
    )

    result = ScanResult()
    async with HttpClient() as client:
        collected = await asyncio.gather(
            *(_collect(source, client, ctx) for source in sources), return_exceptions=False
        )

    for source, (items, error) in zip(sources, collected, strict=True):
        source_result, new_ids = _persist(source.key, items, error, voivodeship, require_region)
        result.sources.append(source_result)
        result.new_listing_ids.extend(new_ids)

    with session_scope() as session:
        recount_agencies(session)

    return result

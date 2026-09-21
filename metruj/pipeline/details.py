"""Dociąganie kart ofert — głównie po to, żeby mieć numer telefonu.

Listy wyników portali numeru nie podają prawie nigdy. Podaje go za to **karta
pojedynczej oferty**: Otodom trzyma w `__NEXT_DATA__` osobno numer agenta
i centralę biura, GetHome podaje numer wprost. Wejście na kartę to jednak
jedno zapytanie na ofertę, więc nie robimy tego przy zbieraniu — byłoby to
pół miliona zapytań na przebieg.

Zamiast tego jest osobny, budżetowany przebieg: bierze oferty **bez kontaktu**,
najnowsze najpierw, i dociąga im kartę. Dzięki temu koszt jest znany z góry
(`--limit`), a oferta, którą ktoś właśnie wystawił, dostaje numer w ciągu
kilkunastu minut.

Kolejność ma znaczenie także dlatego, że karta oferty niesie przy okazji pełny
opis, rok budowy, piętro i współrzędne — czyli dokładnie te pola, których
brakuje na liście wyników.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import case, desc, or_, select

from ..db import session_scope
from ..models import Agency, Listing, ListingStatus, Phone, PropertyType, utcnow
from ..settings import get_settings
from ..utils.http import HttpClient
from ..utils.phones import parse_phone

log = logging.getLogger("metruj.details")

#: Źródła, których karta oferty wnosi dane niedostępne na liście wyników.
#: Klucz źródła -> klasa scrapera z metodą `fetch_detail(url) -> dict`.
DETAIL_SOURCES = ("otodom",)

#: Pola, które wolno nadpisać danymi z karty. Reszta zostaje — lista wyników
#: bywa świeższa (cena), a karta bogatsza (opis, parametry).
UPDATABLE = (
    "description", "year_built", "floor", "floors_total", "building_type",
    "market", "plot_area", "lat", "lon", "street", "district",
)

#: Ile kart dociągamy domyślnie w jednym przebiegu.
DEFAULT_BATCH = 300


@dataclass
class DetailStats:
    checked: int = 0
    fetched: int = 0
    with_phone: int = 0
    enriched: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"sprawdzone={self.checked} pobrane={self.fetched} "
            f"z_numerem={self.with_phone} uzupełnione_pola={self.enriched} "
            f"nieudane={self.failed}"
        )


def _scraper_for(source_key: str, client: HttpClient):
    from ..scrapers import SCRAPERS

    cls = SCRAPERS.get(source_key)
    if cls is None or not hasattr(cls, "fetch_detail"):
        return None
    return cls(client, {})


def _pending(limit: int, sources: list[str]) -> list[tuple[int, str, str]]:
    """Oferty, którym karta coś dołoży — od najnowszych.

    Nie chodzi już tylko o telefon. Lista wyników Otodomu nie podaje piętra
    (było przy 19% mieszkań), roku budowy ani pełnego opisu — wszystko to
    stoi na karcie oferty. Skoro i tak po nią sięgamy, bierzemy też oferty,
    którym brakuje tych pól, a nie wyłącznie te bez kontaktu.
    """
    with session_scope() as session:
        incomplete = or_(
            ~Listing.phones.any(),
            Listing.floor.is_(None),
            Listing.year_built.is_(None),
            Listing.description.is_(None),
        )
        stmt = (
            select(Listing.id, Listing.source_key, Listing.url)
            .where(
                Listing.source_key.in_(sources),
                Listing.status == ListingStatus.AKTYWNA,
                incomplete,
                # Karta jest jedna: gdy już ją pobraliśmy, nie wracamy po to,
                # czego w niej nie było.
                Listing.detail_fetched_at.is_(None),
            )
            # Najpierw mieszkania i domy: przy garażu czy hali „piętro" i „rok
            # budowy" nie istnieją, więc dociąganie ich karty niczego nie doda
            # poza numerem telefonu. Budżet przebiegu jest ograniczony i ma iść
            # tam, gdzie brakuje czegoś, co ktoś naprawdę czyta.
            .order_by(
                case(
                    (Listing.property_type.in_(
                        [PropertyType.MIESZKANIE, PropertyType.DOM]), 0),
                    else_=1,
                ),
                desc(Listing.first_seen_at),
            )
            .limit(limit)
        )
        return [(row.id, row.source_key, row.url) for row in session.execute(stmt)]


def _apply(listing_id: int, updates: dict) -> tuple[bool, int]:
    """Zapisuje dane z karty. Zwraca (czy doszedł numer, ile pól uzupełniono)."""
    settings = get_settings()
    got_phone = False
    filled = 0
    with session_scope() as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return False, 0
        listing.detail_fetched_at = utcnow()

        for field in UPDATABLE:
            value = updates.get(field)
            if value in (None, "", []):
                continue
            if getattr(listing, field, None) in (None, "", []):
                setattr(listing, field, value)
                filled += 1
        if updates.get("lat") and updates.get("lon") and listing.geo_precision != "address":
            listing.geo_precision = "portal"
            listing.geo_source = "portal"

        existing = {p.hashed for p in listing.phones}
        for raw in updates.get("phones_raw") or []:
            phone = parse_phone(raw, origin="portal")
            if phone is None or phone.hashed in existing:
                continue
            existing.add(phone.hashed)
            listing.phones.append(
                Phone(
                    e164="" if settings.store_phone_hash_only else phone.e164,
                    national="" if settings.store_phone_hash_only else phone.national,
                    masked=phone.masked,
                    hashed=phone.hashed,
                    origin=phone.origin,
                )
            )
            got_phone = True
            # numer z karty jest też numerem biura, które ofertę wystawiło
            if listing.agency_id:
                agency = session.get(Agency, listing.agency_id)
                if agency is not None:
                    value = phone.masked if settings.mask_phones else phone.e164
                    agency.phones = sorted(set((agency.phones or []) + [value]))[:10]
    return got_phone, filled


async def enrich_details(
    *, limit: int = DEFAULT_BATCH, sources: list[str] | None = None, concurrency: int = 3
) -> DetailStats:
    """Dociąga karty ofert bez kontaktu i zapisuje, co z nich wynika."""
    sources = sources or list(DETAIL_SOURCES)
    stats = DetailStats()
    pending = _pending(limit, sources)
    stats.checked = len(pending)
    if not pending:
        return stats

    gate = asyncio.Semaphore(max(1, concurrency))
    async with HttpClient(concurrency=max(2, concurrency)) as client:
        scrapers = {key: _scraper_for(key, client) for key in sources}

        async def one(entry: tuple[int, str, str]) -> None:
            listing_id, source_key, url = entry
            scraper = scrapers.get(source_key)
            if scraper is None:
                return
            async with gate:
                try:
                    updates = await scraper.fetch_detail(url)
                except Exception as exc:
                    log.debug("Karta %s nie odpowiedziała: %s", url, exc)
                    stats.failed += 1
                    # i tak oznaczamy próbę, żeby nie wracać do niej w kółko
                    _apply(listing_id, {})
                    return
            if not updates:
                stats.failed += 1
                _apply(listing_id, {})
                return
            stats.fetched += 1
            got_phone, filled = _apply(listing_id, updates)
            stats.with_phone += int(got_phone)
            stats.enriched += filled

        await asyncio.gather(*(one(entry) for entry in pending))

    log.info("Karty ofert: %s", stats)
    return stats


def enrich_details_sync(limit: int = DEFAULT_BATCH) -> DetailStats:
    return asyncio.run(enrich_details(limit=limit))

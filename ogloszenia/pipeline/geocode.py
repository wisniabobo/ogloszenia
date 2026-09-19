"""Nadawanie ofertom współrzędnych — żeby dało się je pokazać na mapie.

Kolejność źródeł, od najlepszego:
  1. **współrzędne z portalu** — jeśli oferta już je ma, nie ruszamy,
  2. **GUGiK UUG** — punkty adresowe z ewidencji, najdokładniejsze dla Polski,
  3. **Nominatim (OSM)** — zapas, gdy ewidencja nie zna adresu.

Wszystko przechodzi przez `geocode_cache`, więc ten sam adres pytamy raz.
Trzy portale z tą samą kamienicą to jedno zapytanie, nie trzy — inaczej
darmowe usługi zablokowałyby nas po kilkuset ofertach.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..apis.gugik import GeocodeResult, GugikClient
from ..apis.nominatim import NominatimClient
from ..db import session_scope
from ..models import GeocodeCache, Listing, ListingStatus
from ..utils.http import HttpClient
from ..utils.text import clean, sha1

log = logging.getLogger("ogloszenia.geocode")

#: ile ofert geokodujemy w jednym przebiegu (ochrona cudzych serwerów)
DEFAULT_BATCH = 200

#: prefiks TERYT województwa — chroni przed trafieniem w miejscowość
#: o tej samej nazwie w innym regionie (Opole vs Opole Lubelskie)
TERYT_PREFIX: dict[str, str] = {
    "dolnoslaskie": "02", "kujawsko-pomorskie": "04", "lubelskie": "06", "lubuskie": "08",
    "lodzkie": "10", "malopolskie": "12", "mazowieckie": "14", "opolskie": "16",
    "podkarpackie": "18", "podlaskie": "20", "pomorskie": "22", "slaskie": "24",
    "swietokrzyskie": "26", "warminsko-mazurskie": "28", "wielkopolskie": "30",
    "zachodniopomorskie": "32",
}

#: zgrubna ramka województwa — druga linia obrony, gdy TERYT nie wrócił
BBOX: dict[str, tuple[float, float, float, float]] = {
    # (lat_min, lat_max, lon_min, lon_max)
    "opolskie": (49.95, 51.20, 16.85, 18.75),
}


def _within_region(lat: float, lon: float, voivodeship: str) -> bool:
    box = BBOX.get(voivodeship)
    if not box:
        return True
    lat_min, lat_max, lon_min, lon_max = box
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


@dataclass
class GeocodeStats:
    checked: int = 0
    from_cache: int = 0
    geocoded: int = 0
    failed: int = 0
    #: ile realnych zapytań poszło w świat — miara tego, jak bardzo
    #: grupowanie po adresie i cache oszczędzają cudze serwery
    queries: int = 0

    def __str__(self) -> str:
        return (
            f"sprawdzone={self.checked} z_cache={self.from_cache} "
            f"nowe={self.geocoded} nieudane={self.failed} zapytań={self.queries}"
        )


def _cache_key(
    city: str | None, street: str | None, number: str | None, voivodeship: str
) -> tuple[str, str]:
    query = ", ".join(x for x in (clean(city), clean(street), clean(number)) if x)
    return sha1(query.lower(), voivodeship), query


def _lookup_cache(session: Session, key: str) -> GeocodeCache | None:
    entry = session.scalar(select(GeocodeCache).where(GeocodeCache.query_hash == key))
    if entry is not None:
        entry.hits += 1
    return entry


def _store_cache(
    session: Session, key: str, query: str, result: GeocodeResult | None
) -> GeocodeCache:
    """Zapisuje wynik do cache'u, znosząc wyścig między procesami.

    Skan co kwadrans i dobowe pełne przejście potrafią geokodować w tym samym
    czasie. Gdy oba trafią na ten sam adres, drugi INSERT łamie unikalny klucz
    — wtedy po prostu bierzemy to, co zapisał pierwszy, zamiast się wywracać.
    """
    from sqlalchemy.exc import IntegrityError

    existing = session.scalar(select(GeocodeCache).where(GeocodeCache.query_hash == key))
    if existing is not None:
        existing.hits += 1
        return existing

    entry = GeocodeCache(
        query_hash=key,
        query=query[:400],
        lat=result.lat if result else None,
        lon=result.lon if result else None,
        precision=result.precision if result else None,
        source=result.source if result else None,
        teryt=result.teryt if result else None,
        simc=result.simc if result else None,
        postal_code=result.postal_code if result else None,
        city=result.city if result else None,
        street=result.street if result else None,
    )
    session.add(entry)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = session.scalar(select(GeocodeCache).where(GeocodeCache.query_hash == key))
        if winner is not None:
            return winner
        raise
    return entry


def _apply(listing: Listing, entry: GeocodeCache) -> None:
    listing.lat = entry.lat
    listing.lon = entry.lon
    listing.geo_precision = entry.precision
    listing.geo_source = entry.source
    listing.teryt = listing.teryt or entry.teryt
    listing.simc = listing.simc or entry.simc
    listing.postal_code = listing.postal_code or entry.postal_code


async def geocode_pending(
    *,
    limit: int = DEFAULT_BATCH,
    only_active: bool = True,
    voivodeship: str | None = None,
    concurrency: int = 4,
) -> GeocodeStats:
    """Uzupełnia współrzędne ofertom, które ich jeszcze nie mają.

    Pytamy o **unikalne adresy, nie o oferty**. Przy pełnym zbiorze z jednego
    województwa 5 654 oferty to tylko 2 112 różnych adresów — bo kilkanaście
    ogłoszeń potrafi wisieć przy tej samej ulicy, a wiele ma tylko miejscowość.
    Grupowanie skraca robotę prawie trzykrotnie i o tyle samo odciąża cudze
    serwery; do tego kilka adresów leci równolegle.
    """
    from collections import defaultdict

    from ..settings import get_settings

    voivodeship = voivodeship or get_settings().default_voivodeship
    teryt_prefix = TERYT_PREFIX.get(voivodeship)
    stats = GeocodeStats()

    # --- 1. zbierz oferty i pogrupuj je po adresie --------------------- #
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    with session_scope() as session:
        stmt = select(
            Listing.id, Listing.city, Listing.street, Listing.district
        ).where(Listing.lat.is_(None), Listing.city.is_not(None))
        if only_active:
            stmt = stmt.where(Listing.status == ListingStatus.AKTYWNA)
        for row in session.execute(stmt.order_by(Listing.first_seen_at.desc()).limit(limit)):
            groups[(row.city or "", row.street or "", row.district or "")].append(row.id)

    if not groups:
        return stats
    stats.checked = sum(len(ids) for ids in groups.values())

    # --- 2. co już mamy w cache'u ------------------------------------- #
    pending: list[tuple[tuple[str, str, str], str, str]] = []
    with session_scope() as session:
        for key, listing_ids in groups.items():
            city, street, district = key
            cache_key, query = _cache_key(city, street or district, None, voivodeship)
            cached = _lookup_cache(session, cache_key)
            if cached is None:
                pending.append((key, cache_key, query))
                continue
            if cached.found:
                _apply_to_many(session, listing_ids, cached)
                stats.from_cache += len(listing_ids)
            else:
                stats.failed += len(listing_ids)

    if not pending:
        log.info("Geokodowanie (wszystko z cache): %s", stats)
        return stats

    # --- 3. odpytaj tylko nieznane adresy, równolegle ------------------ #
    gate = asyncio.Semaphore(max(1, concurrency))

    async with HttpClient(concurrency=max(4, concurrency * 2)) as http:
        gugik = GugikClient(http)
        nominatim = NominatimClient(http)

        async def resolve(entry):
            key, cache_key, query = entry
            city, street, district = key
            async with gate:
                result = await gugik.geocode(
                    city=city, street=street or None, district=district or None,
                    teryt_prefix=teryt_prefix,
                )
                if result is None:
                    place = await nominatim.search(f"{query}, {voivodeship}, Polska")
                    if place is not None and _within_region(place.lat, place.lon, voivodeship):
                        result = GeocodeResult(
                            lat=place.lat, lon=place.lon, city=city, street=street or None,
                            precision="street" if street else "city", source="nominatim",
                        )
                if result is not None and not _within_region(result.lat, result.lon, voivodeship):
                    result = None
            return key, cache_key, query, result

        resolved = await asyncio.gather(*(resolve(e) for e in pending))

    # --- 4. zapisz do cache'u i rozdaj ofertom ------------------------ #
    for key, cache_key, query, result in resolved:
        listing_ids = groups[key]
        try:
            with session_scope() as session:
                entry = _store_cache(session, cache_key, query, result)
                session.flush()
                if result is not None:
                    _apply_to_many(session, listing_ids, entry)
        except Exception as exc:
            # jeden problematyczny adres nie może zatrzymać całej paczki
            log.debug("Nie zapisano adresu %r: %s", query, exc)
            stats.failed += len(listing_ids)
            continue
        if result is not None:
            stats.geocoded += len(listing_ids)
        else:
            stats.failed += len(listing_ids)

    log.info("Geokodowanie: %s (zapytań: %s na %s ofert)", stats, len(pending), stats.checked)
    stats.queries = len(pending)
    return stats


def _apply_to_many(session: Session, listing_ids: list[int], entry: GeocodeCache) -> None:
    """Rozdaje jeden wynik geokodowania wszystkim ofertom spod tego adresu."""
    from sqlalchemy import update

    session.execute(
        update(Listing)
        .where(Listing.id.in_(listing_ids))
        .values(
            lat=entry.lat,
            lon=entry.lon,
            geo_precision=entry.precision,
            geo_source=entry.source,
        )
    )
    if entry.teryt or entry.simc or entry.postal_code:
        for listing_id in listing_ids:
            listing = session.get(Listing, listing_id)
            if listing is None:
                continue
            listing.teryt = listing.teryt or entry.teryt
            listing.simc = listing.simc or entry.simc
            listing.postal_code = listing.postal_code or entry.postal_code


async def enrich_surroundings(listing_id: int, radius_m: int = 1000) -> dict[str, int]:
    """Dopisuje do oferty odległości do szkół, sklepów i przystanków (OSM).

    Wywoływane na żądanie — przy otwarciu karty oferty — bo publiczny Overpass
    ma ostry limit i nie ma sensu odpytywać go dla ofert, których nikt nie ogląda.
    """
    from ..apis.overpass import OverpassClient

    with session_scope() as session:
        listing = session.get(Listing, listing_id)
        if not listing or listing.lat is None or listing.lon is None:
            return {}
        if listing.poi:
            return listing.poi
        lat, lon = listing.lat, listing.lon

    async with HttpClient(concurrency=1) as http:
        surroundings = await OverpassClient(http).around(lat, lon, radius_m)

    summary = surroundings.summary()
    if summary:
        with session_scope() as session:
            listing = session.get(Listing, listing_id)
            if listing:
                listing.poi = summary
    return summary


def geocode_sync(limit: int = DEFAULT_BATCH) -> GeocodeStats:
    """Wersja do wywołania spoza pętli asynchronicznej (CLI)."""
    return asyncio.run(geocode_pending(limit=limit))

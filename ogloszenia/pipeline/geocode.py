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

    def __str__(self) -> str:
        return (
            f"sprawdzone={self.checked} z_cache={self.from_cache} "
            f"nowe={self.geocoded} nieudane={self.failed}"
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
) -> GeocodeStats:
    """Uzupełnia współrzędne ofertom, które ich jeszcze nie mają."""
    from ..settings import get_settings

    voivodeship = voivodeship or get_settings().default_voivodeship
    teryt_prefix = TERYT_PREFIX.get(voivodeship)
    stats = GeocodeStats()

    with session_scope() as session:
        stmt = select(Listing).where(Listing.lat.is_(None), Listing.city.is_not(None))
        if only_active:
            stmt = stmt.where(Listing.status == ListingStatus.AKTYWNA)
        pending = list(session.scalars(stmt.order_by(Listing.first_seen_at.desc()).limit(limit)))
        targets = [
            (x.id, x.city, x.street, x.district, x.extra.get("house_number") if x.extra else None)
            for x in pending
        ]

    if not targets:
        return stats

    async with HttpClient(concurrency=4) as http:
        gugik = GugikClient(http)
        nominatim = NominatimClient(http)

        for listing_id, city, street, district, number in targets:
            stats.checked += 1
            key, query = _cache_key(city, street or district, number, voivodeship)

            with session_scope() as session:
                cached = _lookup_cache(session, key)
                if cached is not None:
                    listing = session.get(Listing, listing_id)
                    if listing and cached.found:
                        _apply(listing, cached)
                        stats.from_cache += 1
                    else:
                        stats.failed += 1
                    continue

            result = await gugik.geocode(
                city=city, street=street, number=number, district=district,
                voivodeship=voivodeship, teryt_prefix=teryt_prefix,
            )
            if result is None:
                place = await nominatim.search(
                    f"{query}, {voivodeship}, Polska" if query else f"{city}, {voivodeship}"
                )
                if place is not None and _within_region(place.lat, place.lon, voivodeship):
                    result = GeocodeResult(
                        lat=place.lat, lon=place.lon, city=city, street=street,
                        precision="street" if street else "city", source="nominatim",
                    )
            if result is not None and not _within_region(result.lat, result.lon, voivodeship):
                log.debug("Odrzucono punkt spoza regionu: %s -> %s", query, result)
                result = None

            with session_scope() as session:
                entry = _store_cache(session, key, query, result)
                session.flush()
                listing = session.get(Listing, listing_id)
                if listing and result is not None:
                    _apply(listing, entry)
                    stats.geocoded += 1
                else:
                    stats.failed += 1

    log.info("Geokodowanie: %s", stats)
    return stats


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

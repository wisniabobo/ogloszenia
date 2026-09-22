"""Nadawanie ofertom współrzędnych — i poprawianie przy okazji lokalizacji.

Kolejność źródeł, od najlepszego:
  1. **współrzędne z portalu** — jeśli oferta już je ma, nie ruszamy,
  2. **GUGiK UUG** — punkty adresowe z ewidencji, najdokładniejsze dla Polski,
  3. **Nominatim (OSM)** — zapas, gdy ewidencja nie zna adresu.

GUGiK przy każdym adresie oddaje pole `jednostka` w postaci
`{Polska,małopolskie,Kraków,Kraków}` oraz kod TERYT gminy. To jest **mocniejsze
niż cokolwiek, co da się wyczytać z treści ogłoszenia**, więc geokoder nie tylko
dopisuje punkt na mapie, ale też **poprawia województwo, powiat i gminę**.
Dzięki temu oferta, której miejscowość odczytano z tekstu, dostaje właściwy
region z rejestru adresowego zamiast zostawać przy zgadywance.

Wszystko przechodzi przez `geocode_cache`, więc ten sam adres pytamy raz.
Trzy portale z tą samą kamienicą to jedno zapytanie, nie trzy — inaczej
darmowe usługi zablokowałyby nas po kilkuset ofertach.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..apis.gugik import GeocodeResult, GugikClient
from ..apis.nominatim import NominatimClient
from ..db import session_scope
from ..geo import in_poland, in_voivodeship, parse_jednostka, voivodeship_of
from ..geo.streets import split_house_number
from ..models import GeocodeCache, Listing, ListingStatus
from ..settings import region
from ..utils.http import HttpClient
from ..utils.text import clean, sha1

log = logging.getLogger("metruj.geocode")

#: ile ofert geokodujemy w jednym przebiegu (ochrona cudzych serwerów)
DEFAULT_BATCH = 400

#: Dzielnica musi leżeć w tym promieniu od środka swojej miejscowości.
#: Bez tego sprawdzenia „Gosławice" z OpenStreetMap wylądowałyby na Dolnym
#: Śląsku, a „Śródmieście" w Lublinie — takich samych nazw jest w Polsce wiele.
MAX_DISTRICT_KM = 15.0


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


def teryt_prefix(voivodeship: str | None) -> str | None:
    """Dwucyfrowy prefiks TERYT województwa — zabezpieczenie przed dublem nazw.

    „Opole" istnieje w Polsce trzy razy, „Świerczów" pięć. Gdy wiemy, z jakiego
    województwa jest oferta, żądamy trafienia z tym prefiksem — inaczej
    mieszkanie z Opola potrafiło wylądować pod Parczewem.
    """
    entry = region(voivodeship)
    return str(entry["teryt"]) if entry.get("teryt") else None


@dataclass
class GeocodeStats:
    checked: int = 0
    from_cache: int = 0
    geocoded: int = 0
    failed: int = 0
    #: ile ofert dostało poprawiony region na podstawie kodu TERYT z GUGiK
    corrected: int = 0
    #: ile realnych zapytań poszło w świat — miara tego, jak bardzo
    #: grupowanie po adresie i cache oszczędzają cudze serwery
    queries: int = 0
    #: ile ofert bez miasta dostało miejscowość z rejestru po współrzędnych
    named: int = 0

    def __str__(self) -> str:
        return (
            f"sprawdzone={self.checked} z_cache={self.from_cache} "
            f"nowe={self.geocoded} poprawione_regiony={self.corrected} "
            f"nieudane={self.failed} zapytań={self.queries} "
            f"miejscowości_z_punktu={self.named}"
        )


def _cache_key(
    city: str | None, street: str | None, number: str | None, voivodeship: str | None
) -> tuple[str, str]:
    query = ", ".join(x for x in (clean(city), clean(street), clean(number)) if x)
    return sha1(query.lower(), voivodeship or "pl"), query


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
        jednostka=result.jednostka if result else None,
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


async def geocode_pending(
    *,
    limit: int = DEFAULT_BATCH,
    only_active: bool = True,
    scope: list[str] | None = None,
    concurrency: int = 4,
) -> GeocodeStats:
    """Uzupełnia współrzędne ofertom, które ich jeszcze nie mają.

    Pytamy o **unikalne adresy, nie o oferty**. Kilkanaście ogłoszeń potrafi
    wisieć przy tej samej ulicy, a wiele ma tylko miejscowość — grupowanie
    skraca robotę prawie trzykrotnie i o tyle samo odciąża cudze serwery.
    """
    from collections import defaultdict

    stats = GeocodeStats()

    # --- 1. zbierz oferty i pogrupuj je po adresie --------------------- #
    Key = tuple[str, str, str, str]
    groups: dict[Key, list[int]] = defaultdict(list)
    with session_scope() as session:
        stmt = select(
            Listing.id, Listing.city, Listing.street, Listing.district, Listing.voivodeship
        ).where(Listing.lat.is_(None), Listing.city.is_not(None))
        if only_active:
            stmt = stmt.where(Listing.status == ListingStatus.AKTYWNA)
        if scope:
            stmt = stmt.where(Listing.voivodeship.in_(scope))
        for row in session.execute(stmt.order_by(Listing.first_seen_at.desc()).limit(limit)):
            key = (row.city or "", row.street or "", row.district or "", row.voivodeship or "")
            groups[key].append(row.id)

    if not groups:
        stats.named = await name_places_from_points(limit=limit)
        return stats
    stats.checked = sum(len(ids) for ids in groups.values())

    # --- 2. co już mamy w cache'u ------------------------------------- #
    pending: list[tuple[Key, str, str]] = []
    with session_scope() as session:
        for key, listing_ids in groups.items():
            city, street, district, voivodeship = key
            name, number = split_house_number(street or district)
            cache_key, query = _cache_key(city, name, number, voivodeship)
            cached = _lookup_cache(session, cache_key)
            if cached is None:
                pending.append((key, cache_key, query))
                continue
            if cached.found:
                stats.corrected += _apply_to_many(session, listing_ids, cached)
                stats.from_cache += len(listing_ids)
            else:
                stats.failed += len(listing_ids)

    if not pending:
        stats.named = await name_places_from_points(limit=limit)
        log.info("Geokodowanie (wszystko z cache): %s", stats)
        return stats

    # --- 3. odpytaj tylko nieznane adresy, równolegle ------------------ #
    gate = asyncio.Semaphore(max(1, concurrency))

    async with HttpClient(concurrency=max(4, concurrency * 2)) as http:
        gugik = GugikClient(http)
        nominatim = NominatimClient(http)

        async def resolve(entry):
            key, cache_key, query = entry
            city, street, district, voivodeship = key
            prefix = teryt_prefix(voivodeship)
            name, number = split_house_number(street or district)
            async with gate:
                result = await gugik.geocode(
                    city=city, street=name, number=number,
                    district=district or None, teryt_prefix=prefix,
                )

                # Rejestr GUGiK nie zna dzielnic miast — na „Śródmieście"
                # odpowiada wsią pod Lublinem. OpenStreetMap zna je dobrze,
                # więc dla ofert bez ulicy, ale z dzielnicą, pytamy właśnie
                # jego. Wynik przyjmujemy tylko wtedy, gdy leży blisko środka
                # swojej miejscowości.
                needs_district = (
                    district
                    and not street
                    and (result is None or result.precision in ("city", "district"))
                )
                if needs_district:
                    place = await nominatim.search(f"{district}, {city}, Polska")
                    if place is not None and in_poland(place.lat, place.lon):
                        anchor = result
                        near_enough = anchor is None or _distance_km(
                            anchor.lat, anchor.lon, place.lat, place.lon
                        ) <= MAX_DISTRICT_KM
                        if near_enough:
                            result = GeocodeResult(
                                lat=place.lat, lon=place.lon, city=city, street=None,
                                teryt=anchor.teryt if anchor else None,
                                jednostka=anchor.jednostka if anchor else None,
                                precision="district", source="osm-dzielnica",
                            )

                if result is None:
                    where = f"{query}, {voivodeship}, Polska" if voivodeship else f"{query}, Polska"
                    place = await nominatim.search(where)
                    if place is not None and in_voivodeship(place.lat, place.lon, voivodeship):
                        result = GeocodeResult(
                            lat=place.lat, lon=place.lon, city=city, street=name,
                            precision="street" if street else "city", source="nominatim",
                        )
                # Punkt poza Polską to na pewno pomyłka; poza zadeklarowanym
                # województwem — prawdopodobna, więc też odrzucamy.
                if result is not None and not in_voivodeship(result.lat, result.lon, voivodeship):
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
                    stats.corrected += _apply_to_many(session, listing_ids, entry)
        except Exception as exc:
            # jeden problematyczny adres nie może zatrzymać całej paczki
            log.debug("Nie zapisano adresu %r: %s", query, exc)
            stats.failed += len(listing_ids)
            continue
        if result is not None:
            stats.geocoded += len(listing_ids)
        else:
            stats.failed += len(listing_ids)

    stats.queries = len(pending)
    stats.named = await name_places_from_points(limit=limit)
    log.info("Geokodowanie: %s", stats)
    return stats


#: Jak daleko od punktu szukać najbliższego adresu. Na wsi najbliższy
#: budynek z numerem bywa kilkaset metrów dalej.
REVERSE_RADIUS_M = 1500


async def name_places_from_points(*, limit: int = DEFAULT_BATCH, concurrency: int = 4) -> int:
    """Lokalizacja z rejestru adresowego tam, gdzie punkt wie więcej niż tekst.

    Dwa przypadki:

    - **oferta ma punkt, a nie ma miasta.** GetHome przy części ogłoszeń podaje
      tylko powiat i współrzędne; karta pokazywała „— · pow. …".
    - **punkt od portalu leży poza przypisanym województwem.** GetHome nie
      podaje regionu, więc brał się on z nazwy miejscowości: z kilku
      „Osieków" wygrywał świętokrzyski, choć pinezka stała pod Krakowem,
      a wieś spoza rejestru gmin dostawała region z sekcji wyszukiwania —
      i w filtrze „opolskie" wisiały oferty z Florynki i Wieliczki.

    Rejestr GUGiK zwraca najbliższy adres z miejscowością, gminą, powiatem
    i kodem TERYT. Ofercie bez miasta bierzemy z niego wszystko, ale gdy
    ogłoszenie samo podało powiat, rejestr musi się z nim zgadzać. Ofercie
    z miastem zostawiamy jej miejscowość, a przynależność administracyjną
    bierzemy z punktu.
    """
    with session_scope() as session:
        rows = session.execute(
            select(Listing.id, Listing.lat, Listing.lon, Listing.city, Listing.voivodeship,
                   Listing.county, Listing.geo_precision)
            .where(Listing.lat.is_not(None), Listing.lon.is_not(None),
                   Listing.status == ListingStatus.AKTYWNA,
                   or_(Listing.city.is_(None), Listing.geo_precision == "portal"))
        ).all()
    rows = [
        row for row in rows
        if row.city is None
        or not row.voivodeship
        or not in_voivodeship(row.lat, row.lon, row.voivodeship)
        # punkt od portalu, a powiatu nie znamy — wieś spoza rejestru gmin
        or (row.geo_precision == "portal" and not row.county)
    ][:limit]
    if not rows:
        return 0

    gate = asyncio.Semaphore(max(1, concurrency))
    async with HttpClient(concurrency=max(4, concurrency * 2)) as http:
        gugik = GugikClient(http)

        async def lookup(row):
            async with gate:
                return row, await gugik.reverse(row.lat, row.lon, radius=REVERSE_RADIUS_M)

        answers = await asyncio.gather(*(lookup(row) for row in rows))

    fixed = 0
    with session_scope() as session:
        for row, found in answers:
            if found is None or not found.city:
                continue
            unit = parse_jednostka(found.jednostka) if found.jednostka else {}
            if not unit.get("voivodeship"):
                continue
            from_portal = row.geo_precision == "portal"
            if row.city is None and not from_portal:
                # punkt z naszego geokodowania — musi się zgadzać z ogłoszeniem
                if row.voivodeship and unit["voivodeship"] != row.voivodeship:
                    continue
                if row.county and unit.get("county") and unit["county"] != row.county:
                    continue
            if from_portal and row.city is None and row.county and unit.get("county") \
                    and unit["county"] != row.county:
                continue
            listing = session.get(Listing, row.id)
            if listing is None:
                continue
            if listing.city is None or listing.city.lower() == found.city.lower():
                listing.city = found.city
            listing.voivodeship = unit["voivodeship"]
            listing.county = unit.get("county") or listing.county
            listing.commune = unit.get("commune") or listing.commune
            listing.teryt = found.teryt or listing.teryt
            listing.simc = listing.simc or found.simc
            listing.postal_code = listing.postal_code or found.postal_code
            fixed += 1
    return fixed


def _administrative_fix(listing: Listing, entry: GeocodeCache) -> bool:
    """Przepisuje województwo, powiat i gminę z odpowiedzi rejestru adresowego.

    Rejestr wie to na pewno, a treść ogłoszenia — nie. Jeżeli oferta miała
    wpisany inny region, to znaczy, że rozpoznanie z tekstu się pomyliło
    i właśnie teraz jest moment, żeby to naprawić.
    """
    unit = parse_jednostka(entry.jednostka) if entry.jednostka else {}
    if not unit and entry.teryt:
        voivodeship = voivodeship_of(entry.teryt)
        unit = {"voivodeship": voivodeship} if voivodeship else {}
    if not unit:
        return False

    changed = False
    for field, value in (
        ("voivodeship", unit.get("voivodeship")),
        ("county", unit.get("county")),
        ("commune", unit.get("commune")),
    ):
        if value and getattr(listing, field, None) != value:
            setattr(listing, field, value)
            changed = True
    if entry.teryt and listing.teryt != entry.teryt:
        listing.teryt = entry.teryt
    return changed


def _apply_to_many(session: Session, listing_ids: list[int], entry: GeocodeCache) -> int:
    """Rozdaje jeden wynik geokodowania wszystkim ofertom spod tego adresu.

    Zwraca liczbę ofert, którym przy okazji poprawiono przynależność
    administracyjną.
    """
    corrected = 0
    for listing_id in listing_ids:
        listing = session.get(Listing, listing_id)
        if listing is None:
            continue
        # Współrzędne z portalu są dokładniejsze niż geokodowanie po adresie.
        if listing.geo_precision != "portal":
            listing.lat = entry.lat
            listing.lon = entry.lon
            listing.geo_precision = entry.precision
            listing.geo_source = entry.source
        listing.simc = listing.simc or entry.simc
        listing.postal_code = listing.postal_code or entry.postal_code
        corrected += int(_administrative_fix(listing, entry))
    return corrected


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

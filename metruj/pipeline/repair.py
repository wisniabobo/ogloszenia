"""Naprawa danych, które trafiły do bazy przed poprawkami.

Baza na serwerze żyje od miesięcy i ma w sobie ślady każdego błędu, który po
drodze poprawiliśmy: oferty z ceną 0, ceny za metr zaokrąglone do zera,
lokalizacje odczytane starym, zgadującym mechanizmem i wpisy bez województwa,
bo serwis obejmował wtedy jedno.

Skan tego nie naprawi: aktualizuje tylko te pola, które akurat przyszły ze
źródła, a ogłoszenie sprzed pół roku może już ze źródła nie przychodzić.
Stąd osobny przebieg, uruchamiany raz po wdrożeniu.

Naprawa jest **zachowawcza**: nie wymyśla danych, tylko przelicza to, co da
się wyliczyć z pól już zapisanych, i czyści wartości, o których wiadomo,
że są nieprawdziwe. Wszystkiego, czego nie da się rozstrzygnąć na miejscu,
nie rusza — dokończy to geokoder, pytając rejestr adresowy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..geo import detect_location, known_voivodeship, lookup, resolve_place
from ..models import Listing, ListingStatus
from .normalize import LAND_TYPES, NAVIGATION_TITLE

#: Źródła, w których lokalizacja pochodzi z pola portalu, a nie z tekstu.
#: Ich ofert nie ruszamy: portal podał miasto wprost i jest ono prawdziwe.
PORTAL_SOURCES = {
    "olx", "otodom", "gratka", "morizon", "domiporta", "gethome", "adresowo",
    "nieruchomosci_online", "szybko", "rynekpierwotny", "krn", "domy_pl",
    "otoprzeprowadzki_oferty_net", "nportal", "tabelaofert",
}

log = logging.getLogger("metruj.repair")


@dataclass
class RepairStats:
    prices: int = 0
    price_per_m2: int = 0
    land_area: int = 0
    regions: int = 0
    relocated: int = 0
    navigation: int = 0
    regeocode: int = 0

    def __str__(self) -> str:
        return (
            f"ceny={self.prices} cena_za_m2={self.price_per_m2} "
            f"powierzchnia_gruntu={self.land_area} regiony={self.regions} "
            f"lokalizacje={self.relocated} śmieci={self.navigation} "
            f"do_przeliczenia={self.regeocode}"
        )


def _fix_prices(session: Session) -> tuple[int, int]:
    """Cena 0 znaczy „nie podano", a nie „za darmo" — i nie może sortować się
    przed ofertami z ceną. To samo z ceną za metr zaokrągloną do zera."""
    zeroed = session.execute(
        update(Listing).where(Listing.price <= 0).values(price=None, price_per_m2=None)
    ).rowcount

    recomputed = 0
    rows = session.execute(
        select(Listing.id, Listing.price, Listing.area).where(
            Listing.price.is_not(None), Listing.area.is_not(None), Listing.area > 1,
            or_(Listing.price_per_m2.is_(None), Listing.price_per_m2 <= 0),
        )
    ).all()
    for row in rows:
        per_meter = row.price / row.area
        value = round(per_meter, 2) if per_meter >= 1 else round(per_meter, 4)
        if not value:
            continue
        session.execute(
            update(Listing).where(Listing.id == row.id).values(price_per_m2=value)
        )
        recomputed += 1
    return int(zeroed or 0), recomputed


def _fix_land_area(session: Session) -> int:
    """Przy działce powierzchnia oferty to powierzchnia gruntu — portale
    podają ją raz w jednym, raz w drugim polu."""
    changed = session.execute(
        update(Listing)
        .where(Listing.property_type.in_(LAND_TYPES),
               Listing.area.is_(None), Listing.plot_area.is_not(None))
        .values(area=Listing.plot_area)
    ).rowcount or 0
    changed += session.execute(
        update(Listing)
        .where(Listing.property_type.in_(LAND_TYPES),
               Listing.plot_area.is_(None), Listing.area.is_not(None))
        .values(plot_area=Listing.area)
    ).rowcount or 0
    return int(changed)


def _fix_regions(session: Session) -> int:
    """Uzupełnia województwo i powiat na podstawie nazwy miejscowości.

    Dotyczy ofert zebranych, gdy serwis obejmował jedno województwo i pola
    po prostu nie wypełniał. Nazwę powtarzającą się w kraju rozstrzygamy
    powiatem, jeśli jest zapisany; bez niego zostawiamy pole puste i niech
    rozstrzygnie geokoder.
    """
    rows = session.execute(
        select(Listing.id, Listing.city, Listing.county, Listing.voivodeship)
        .where(Listing.city.is_not(None), Listing.voivodeship.is_(None))
    ).all()

    changed = 0
    for row in rows:
        hint = None
        if row.county:
            counties = [u for u in lookup(row.county) if u.kind == "powiat"]
            regions = {u.voivodeship for u in counties}
            if len(regions) == 1:
                hint = regions.pop()
        place = resolve_place(row.city, voivodeship_hint=hint)
        values = {
            key: value
            for key, value in (
                ("voivodeship", known_voivodeship(place.get("voivodeship"))),
                ("county", place.get("county") if not row.county else None),
                ("commune", place.get("commune")),
                ("teryt", place.get("teryt")),
            )
            if value
        }
        if not values:
            continue
        session.execute(update(Listing).where(Listing.id == row.id).values(**values))
        changed += 1
    return changed


def _mark_for_regeocode(session: Session, suspicious_only: bool = True) -> int:
    """Kasuje współrzędne tam, gdzie punkt nie zgadza się z województwem.

    Geokoder policzy je od nowa i przy okazji poprawi przynależność
    administracyjną, bo GUGiK oddaje ją razem z adresem. Współrzędnych
    z portalu nie ruszamy — są dokładniejsze od naszego geokodowania.
    """
    from ..geo import in_voivodeship

    rows = session.execute(
        select(Listing.id, Listing.lat, Listing.lon, Listing.voivodeship)
        .where(Listing.lat.is_not(None), Listing.geo_precision != "portal")
    ).all()

    stale = [
        row.id for row in rows
        if row.voivodeship and not in_voivodeship(row.lat, row.lon, row.voivodeship)
    ] if suspicious_only else [row.id for row in rows]

    for chunk_start in range(0, len(stale), 500):
        chunk = stale[chunk_start : chunk_start + 500]
        session.execute(
            update(Listing).where(Listing.id.in_(chunk)).values(
                lat=None, lon=None, geo_precision=None, geo_source=None
            )
        )
    return len(stale)


def _deactivate_navigation(session: Session) -> int:
    """Wygasza pozycje, które nigdy nie były ofertą, tylko elementem strony.

    Scraper AMW zapisał jedenaście razy blok „Polecane nieruchomości" jako
    ogłoszenie, każde z lokalizacją zgadniętą z przypadkowego słowa w menu.
    Normalizacja takich już nie wpuszcza; te, które zdążyły wejść, gasimy —
    a nie kasujemy, bo skasowania nie da się cofnąć, a wygaszenie owszem.
    """
    rows = session.execute(
        select(Listing.id, Listing.title).where(Listing.status == ListingStatus.AKTYWNA)
    ).all()
    junk = [row.id for row in rows if NAVIGATION_TITLE.match(row.title or "")]
    for start in range(0, len(junk), 500):
        session.execute(
            update(Listing)
            .where(Listing.id.in_(junk[start : start + 500]))
            .values(status=ListingStatus.ARCHIWALNA)
        )
    return len(junk)


def _relocate_text_sources(session: Session) -> int:
    """Czyta lokalizację od nowa tam, gdzie brała się z tekstu.

    Ogłoszenia urzędowe, komornicze i instytucjonalne nie mają pola „miasto" —
    lokalizację trzeba wyczytać z tytułu. Robił to stary mechanizm, który znał
    tylko jedno województwo i każdą nazwę spoza niego dopasowywał do czegoś
    w środku: działka w Lubaniu trafiła do Kędzierzyna-Koźla.

    Nową lokalizację przyjmujemy tylko wtedy, gdy rozpoznanie jest pewne
    (miasto albo wyraźna wskazówka) i różni się od zapisanej. Wtedy kasujemy
    też współrzędne, żeby geokoder policzył je od nowa i potwierdził region
    kodem TERYT z rejestru adresowego.
    """
    rows = session.execute(
        select(Listing.id, Listing.source_key, Listing.title, Listing.city,
               Listing.voivodeship)
        .where(Listing.source_key.notin_(PORTAL_SOURCES))
    ).all()

    changed = 0
    for row in rows:
        found = detect_location(row.title)
        if not found.city or found.basis not in ("miasto", "wskazówka"):
            continue
        if found.city == row.city and found.voivodeship == row.voivodeship:
            continue
        session.execute(
            update(Listing).where(Listing.id == row.id).values(
                city=found.city,
                county=found.county,
                commune=found.commune,
                voivodeship=found.voivodeship,
                teryt=found.teryt,
                lat=None, lon=None, geo_precision=None, geo_source=None,
            )
        )
        changed += 1
    return changed


def repair(session: Session, *, regeocode_all: bool = False) -> RepairStats:
    """Przelicza pola, które dało się policzyć źle, i czyści nieprawdziwe."""
    stats = RepairStats()
    stats.prices, stats.price_per_m2 = _fix_prices(session)
    stats.land_area = _fix_land_area(session)
    stats.navigation = _deactivate_navigation(session)
    stats.relocated = _relocate_text_sources(session)
    stats.regions = _fix_regions(session)
    stats.regeocode = _mark_for_regeocode(session, suspicious_only=not regeocode_all)
    log.info("Naprawa danych: %s", stats)
    return stats

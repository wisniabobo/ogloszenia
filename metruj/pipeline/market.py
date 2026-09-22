"""Odniesienie rynkowe: ile się tu zwykle płaci — i które oferty odstają.

Sama cena niczego nie mówi. 450 000 zł za 50 m² to okazja w Warszawie
i drogo w Sanoku. Żeby „wyszukiwanie okazji" miało sens, potrzebne jest
odniesienie: **mediana ceny za metr** dla tej samej miejscowości, tego samego
typu nieruchomości i tej samej transakcji.

Mediana, nie średnia — jedna kamienica za 40 milionów potrafi podnieść średnią
całego miasta, a mediany nie ruszy.

Liczymy to raz po skanie i zapisujemy przy ofercie jako `deal_ratio`
(1,00 = dokładnie mediana; 0,70 = trzydzieści procent poniżej). Dzięki temu
sortowanie i filtrowanie po okazyjności to zwykłe zapytanie po indeksowanej
kolumnie, a nie liczenie mediany przy każdym odświeżeniu strony.

Przy ilu ofertach mediana jest coś warta? Poniżej `MIN_SAMPLE` w ogóle jej nie
liczymy dla miejscowości i schodzimy na powiat, a potem na województwo. Lepiej
powiedzieć „brak odniesienia" niż porównywać ofertę z samą sobą.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Listing, ListingStatus, MarketStat, PropertyType, TransactionType, utcnow

log = logging.getLogger("metruj.market")

#: Poniżej tylu ofert mediana jest przypadkiem, a nie odniesieniem.
MIN_SAMPLE = 8

#: Poziomy odniesienia, od najdokładniejszego. Oferta dostaje pierwszy poziom,
#: który ma dość danych.
LEVELS = ("miasto", "powiat", "wojewodztwo")

#: Typy, dla których cena za metr jest porównywalna. Przy gruntach rolnych
#: i halach rozrzut jest tak duży, że mediana niczego nie tłumaczy.
COMPARABLE = {
    PropertyType.MIESZKANIE,
    PropertyType.DOM,
    PropertyType.KAMIENICA,
    PropertyType.LOKAL,
    PropertyType.BIURO,
    PropertyType.GARAZ,
    PropertyType.DZIALKA,
}

#: Przedmioty, które portal wrzuca do kategorii „dom" albo „mieszkanie",
#: a które domem ani mieszkaniem nie są: altana na działce ROD, domek
#: holenderski, kontener, pawilon handlowy, kwatera pracownicza rozliczana
#: za dobę. Porównane z medianą miasta wychodzą na okazję stulecia — bo są
#: po prostu czymś innym. Nie oceniamy ich wcale; lepiej nie powiedzieć nic
#: niż podsunąć kontener jako tani dom w Warszawie.
NOT_COMPARABLE = re.compile(
    r"\bROD\b|rodzinnych ogrod|ogrodzie dzia[łl]kow|\baltan|domek holendersk|"
    r"\bkontener|pawilon|modu[łl]ow|barakow[oó]z|\bblaszak|\bkiosk|"
    r"kwater\w*\s+(?:pracownicz|dla)|noclegi|nocleg\w*|za\s+dob[ęe]|na\s+doby|"
    r"agroturyst|\bhostel|apartament\w*\s+na\s+doby|\bmiejsce\s+noclegow",
    re.I,
)

#: Poniżej tego stosunku do mediany oferta niemal zawsze opisuje coś innego
#: niż reszta zbioru: udział w nieruchomości, ruinę, cenę „od" albo pomyłkę
#: w metrażu. Nie nazywamy tego okazją, bo w dziewięciu przypadkach na dziesięć
#: nią nie jest — a dziesiąty i tak wymaga obejrzenia ogłoszenia.
MIN_PLAUSIBLE_RATIO = 0.35


@dataclass
class MarketStats:
    scopes: int = 0
    scored: int = 0
    without_reference: int = 0
    #: oferty odrzucone jako niewiarygodnie tanie — patrz MIN_PLAUSIBLE_RATIO
    implausible: int = 0

    def __str__(self) -> str:
        return (
            f"odniesień={self.scopes} ocenione={self.scored} "
            f"bez_odniesienia={self.without_reference} odstające={self.implausible}"
        )


def _median(values: list[float]) -> float:
    values = sorted(values)
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def scope_value(level: str, city: str | None, county: str | None,
                voivodeship: str | None) -> str | None:
    """Klucz zakresu mediany.

    Miejscowość i powiat zawsze razem z województwem: „Nowa Wieś" jest
    w każdym z nich, a powiat „brzeski", „opolski" czy „średzki" — w dwóch.
    Sama nazwa mieszała w jednej medianie ceny z drugiego końca Polski.
    """
    if level == "wojewodztwo":
        return voivodeship or None
    value = city if level == "miasto" else county
    if not value or not voivodeship:
        return None
    return f"{value}|{voivodeship}"


def _key(level: str, listing_scope: tuple[str | None, str | None, str | None],
         ptype: str, transaction: str) -> tuple[str, str, str, str] | None:
    value = scope_value(level, *listing_scope)
    return (level, value, ptype, transaction) if value else None


def recompute(session: Session) -> MarketStats:
    """Przelicza mediany i ocenia oferty względem nich."""
    stats = MarketStats()

    rows = session.execute(
        select(
            Listing.id, Listing.city, Listing.county, Listing.voivodeship,
            Listing.property_type, Listing.transaction, Listing.price_per_m2,
            Listing.title,
        ).where(
            Listing.status == ListingStatus.AKTYWNA,
            Listing.price_per_m2.is_not(None),
            Listing.price_per_m2 > 0,
        )
    ).all()

    def comparable(row) -> bool:
        return row.property_type in COMPARABLE and not NOT_COMPARABLE.search(row.title or "")

    buckets: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        if not comparable(row):
            continue
        scope = (row.city, row.county, row.voivodeship)
        ptype = row.property_type.value
        transaction = row.transaction.value
        for level in LEVELS:
            key = _key(level, scope, ptype, transaction)
            if key:
                buckets[key].append(row.price_per_m2)

    medians: dict[tuple[str, str, str, str], tuple[float, int]] = {}
    for key, values in buckets.items():
        if len(values) < MIN_SAMPLE:
            continue
        medians[key] = (_median(values), len(values))
    stats.scopes = len(medians)

    # --- zapis tabeli odniesień (zasila raporty i stronę „Rynek") ---
    session.execute(MarketStat.__table__.delete())
    now = utcnow()
    session.bulk_save_objects([
        MarketStat(
            level=level, scope=scope, property_type=ptype, transaction=transaction,
            median_price_m2=round(median, 2), sample=sample, computed_at=now,
        )
        for (level, scope, ptype, transaction), (median, sample) in medians.items()
    ])

    # --- ocena ofert ---
    updates: list[dict] = []
    for row in rows:
        if not comparable(row):
            stats.without_reference += 1
            updates.append({"id": row.id, "deal_ratio": None, "deal_level": None})
            continue
        scope = (row.city, row.county, row.voivodeship)
        ptype = row.property_type.value
        transaction = row.transaction.value
        reference = None
        for level in LEVELS:
            key = _key(level, scope, ptype, transaction)
            if key and key in medians:
                reference = (level, *medians[key])
                break
        if reference is None:
            stats.without_reference += 1
            updates.append({"id": row.id, "deal_ratio": None, "deal_level": None})
            continue
        level, median, sample = reference
        ratio = round(row.price_per_m2 / median, 3) if median else None
        if ratio is not None and ratio < MIN_PLAUSIBLE_RATIO:
            # Nie „okazja stulecia", tylko oferta opisująca co innego niż reszta.
            stats.implausible += 1
            updates.append({"id": row.id, "deal_ratio": None, "deal_level": None})
            continue
        updates.append({"id": row.id, "deal_ratio": ratio, "deal_level": level})
        stats.scored += 1

    if updates:
        session.execute(update(Listing), updates)

    # oferty, które wypadły z zakresu (np. straciły cenę), nie mogą zostać
    # z nieaktualną oceną sprzed tygodnia
    # Różnicę liczymy w Pythonie i czyścimy paczkami: „NOT IN (…)" z kilkudziesięcioma
    # tysiącami identyfikatorów przekraczało limit parametrów SQLite.
    scored_ids = {u["id"] for u in updates}
    stale = [
        listing_id for listing_id in session.scalars(
            select(Listing.id).where(Listing.deal_ratio.is_not(None))
        )
        if listing_id not in scored_ids
    ]
    for start in range(0, len(stale), 500):
        session.execute(
            update(Listing).where(Listing.id.in_(stale[start:start + 500]))
            .values(deal_ratio=None, deal_level=None)
        )

    log.info("Odniesienie rynkowe: %s", stats)
    return stats


def market_reference(
    session: Session, *, city: str | None = None, county: str | None = None,
    voivodeship: str | None = None, property_type: str | None = None,
    transaction: str = TransactionType.SPRZEDAZ.value,
) -> MarketStat | None:
    """Najdokładniejsze dostępne odniesienie dla podanego zakresu."""
    for level in LEVELS:
        value = scope_value(level, city, county, voivodeship)
        if not value:
            continue
        hit = session.scalar(
            select(MarketStat).where(
                MarketStat.level == level,
                MarketStat.scope == value,
                MarketStat.property_type == property_type,
                MarketStat.transaction == transaction,
            )
        )
        if hit is not None:
            return hit
    return None


def recompute_sync() -> MarketStats:
    from ..db import session_scope

    with session_scope() as session:
        return recompute(session)


def market_levels(session: Session, listing: Listing) -> list[dict]:
    """Mediany ceny za metr dla oferty: w miejscowości, powiecie i województwie.

    Na stronie oferty to odpowiedź na pytanie „drogo czy tanio?" — z liczbą
    ofert, z których mediana wyszła, i z różnicą względem tej oferty.
    """
    if not listing.price_per_m2 or listing.property_type not in COMPARABLE:
        return []
    names = {"miasto": listing.city, "powiat": f"pow. {listing.county}" if listing.county else None,
             "wojewodztwo": f"woj. {listing.voivodeship}" if listing.voivodeship else None}
    out = []
    for level in LEVELS:
        if level == "powiat" and listing.county == listing.city:
            continue  # miasto na prawach powiatu — ta sama liczba dwa razy
        value = scope_value(level, listing.city, listing.county, listing.voivodeship)
        if not value:
            continue
        stat = session.scalar(
            select(MarketStat).where(
                MarketStat.level == level,
                MarketStat.scope == value,
                MarketStat.property_type == listing.property_type.value,
                MarketStat.transaction == listing.transaction.value,
            )
        )
        if stat is None or not stat.median_price_m2:
            continue
        out.append({
            "level": level,
            "place": names[level],
            "median": stat.median_price_m2,
            "sample": stat.sample,
            "diff_pct": round(100 * (listing.price_per_m2 - stat.median_price_m2) / stat.median_price_m2),
        })
    return out

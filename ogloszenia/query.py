"""Wspólny builder filtrów — używany przez API, interfejs web i alerty.

Dzięki jednemu miejscu zapisane poszukiwanie („Poszukiwania") filtruje dokładnie
tak samo jak widok listy, więc to, co widzisz na ekranie, dostaniesz też
powiadomieniem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, and_, desc, func, or_, select

from .models import (
    Agency,
    Listing,
    ListingStatus,
    OfferKind,
    Phone,
    PropertyType,
    SellerType,
    TransactionType,
    utcnow,
)

SORTS = {
    "najnowsze": (Listing.first_seen_at, True),
    "najstarsze": (Listing.first_seen_at, False),
    "cena_rosnaco": (Listing.price, False),
    "cena_malejaco": (Listing.price, True),
    "cena_m2_rosnaco": (Listing.price_per_m2, False),
    "cena_m2_malejaco": (Listing.price_per_m2, True),
    "powierzchnia": (Listing.area, True),
    "najdluzej_wisi": (Listing.first_seen_at, False),
    "najwiecej_kopii": (Listing.copies_count, True),
    "termin_licytacji": (Listing.event_date, False),
}

PERIODS = {"dzis": 1, "7dni": 7, "30dni": 30, "90dni": 90, "1rok": 365}


@dataclass
class Filters:
    """Zestaw filtrów odpowiadający panelowi wyszukiwania."""

    kind: str | None = None
    property_type: str | None = None
    transaction: str | None = None
    city: str | None = None
    district: str | None = None
    county: str | None = None
    street: str | None = None
    source: list[str] = field(default_factory=list)
    seller_type: str | None = None
    agency_id: int | None = None
    price_min: float | None = None
    price_max: float | None = None
    price_m2_min: float | None = None
    price_m2_max: float | None = None
    area_min: float | None = None
    area_max: float | None = None
    rooms_min: int | None = None
    rooms_max: int | None = None
    floor_min: int | None = None
    floor_max: int | None = None
    year_min: int | None = None
    market: str | None = None
    only_original: bool = True
    only_active: bool = True
    with_phone: bool = False
    period: str | None = None
    days_on_market_min: int | None = None
    days_on_market_max: int | None = None
    price_dropped: bool = False
    q: str | None = None
    sort: str = "najnowsze"
    page: int = 1
    per_page: int = 25

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", [], False)}


def _enum(value: str | None, enum_cls):
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def apply_filters(stmt: Select, filters: dict[str, Any] | Filters) -> Select:
    """Nakłada filtry na dowolne zapytanie o `Listing`."""
    f = filters if isinstance(filters, Filters) else Filters(**{
        k: v for k, v in (filters or {}).items() if k in Filters.__dataclass_fields__
    })

    clauses = []

    if kind := _enum(f.kind, OfferKind):
        clauses.append(Listing.kind == kind)
    if ptype := _enum(f.property_type, PropertyType):
        clauses.append(Listing.property_type == ptype)
    if transaction := _enum(f.transaction, TransactionType):
        clauses.append(Listing.transaction == transaction)
    if seller := _enum(f.seller_type, SellerType):
        clauses.append(Listing.seller_type == seller)

    if f.city:
        clauses.append(Listing.city.ilike(f"%{f.city}%"))
    if f.district:
        clauses.append(Listing.district.ilike(f"%{f.district}%"))
    if f.county:
        clauses.append(Listing.county.ilike(f"%{f.county}%"))
    if f.street:
        clauses.append(Listing.street.ilike(f"%{f.street}%"))
    if f.source:
        clauses.append(Listing.source_key.in_(f.source))
    if f.agency_id:
        clauses.append(Listing.agency_id == f.agency_id)
    if f.market:
        clauses.append(Listing.market == f.market)

    for column, low, high in (
        (Listing.price, f.price_min, f.price_max),
        (Listing.price_per_m2, f.price_m2_min, f.price_m2_max),
        (Listing.area, f.area_min, f.area_max),
        (Listing.rooms, f.rooms_min, f.rooms_max),
        (Listing.floor, f.floor_min, f.floor_max),
    ):
        if low is not None:
            clauses.append(column >= low)
        if high is not None:
            clauses.append(column <= high)
    if f.year_min is not None:
        clauses.append(Listing.year_built >= f.year_min)

    if f.only_original:
        clauses.append(Listing.is_original.is_(True))
    if f.only_active:
        clauses.append(Listing.status == ListingStatus.AKTYWNA)

    if f.with_phone:
        clauses.append(Listing.phones.any())

    if f.period and f.period in PERIODS:
        clauses.append(Listing.first_seen_at >= utcnow() - timedelta(days=PERIODS[f.period]))

    now = utcnow()
    if f.days_on_market_min is not None:
        clauses.append(Listing.first_seen_at <= now - timedelta(days=f.days_on_market_min))
    if f.days_on_market_max is not None:
        clauses.append(Listing.first_seen_at >= now - timedelta(days=f.days_on_market_max))

    if f.price_dropped:
        clauses.append(and_(Listing.initial_price.is_not(None), Listing.price < Listing.initial_price))

    if f.q:
        pattern = f"%{f.q}%"
        clauses.append(
            or_(
                Listing.title.ilike(pattern),
                Listing.description.ilike(pattern),
                Listing.street.ilike(pattern),
                Listing.case_number.ilike(pattern),
                Listing.authority.ilike(pattern),
                Listing.seller_name.ilike(pattern),
            )
        )

    return stmt.where(*clauses) if clauses else stmt


def apply_sort(stmt: Select, sort: str = "najnowsze") -> Select:
    column, descending = SORTS.get(sort, SORTS["najnowsze"])
    return stmt.order_by(desc(column) if descending else column.asc(), desc(Listing.id))


def search_listings(session, filters: Filters) -> tuple[list[Listing], int]:
    """Zwraca (strona wyników, łączna liczba trafień)."""
    base = select(Listing)
    base = apply_filters(base, filters)

    total = session.scalar(
        apply_filters(select(func.count(Listing.id)), filters)
    ) or 0

    stmt = apply_sort(base, filters.sort)
    stmt = stmt.offset((max(filters.page, 1) - 1) * filters.per_page).limit(filters.per_page)
    return list(session.scalars(stmt)), int(total)


def phone_lookup(session, phone_fragment: str) -> list[Listing]:
    """Wyszukiwanie po numerze telefonu — pokazuje wszystkie oferty oferenta.

    Przydaje się do sprawdzenia, czy „prywatna" oferta nie jest przypadkiem
    kolejnym ogłoszeniem tego samego biura.
    """
    digits = "".join(c for c in phone_fragment if c.isdigit())
    if len(digits) < 4:
        return []
    stmt = (
        select(Listing)
        .join(Phone)
        .where(or_(Phone.national.like(f"%{digits}%"), Phone.e164.like(f"%{digits}%")))
        .order_by(desc(Listing.first_seen_at))
        .limit(100)
    )
    return list(session.scalars(stmt))


def dashboard_stats(session) -> dict[str, Any]:
    """Liczby na pulpit."""
    now = utcnow()

    def count(*where) -> int:
        return int(session.scalar(select(func.count(Listing.id)).where(*where)) or 0)

    active = Listing.status == ListingStatus.AKTYWNA
    original = Listing.is_original.is_(True)

    by_kind = dict(
        session.execute(
            select(Listing.kind, func.count(Listing.id)).where(active).group_by(Listing.kind)
        ).all()
    )
    by_source = session.execute(
        select(Listing.source_key, func.count(Listing.id))
        .where(active)
        .group_by(Listing.source_key)
        .order_by(desc(func.count(Listing.id)))
    ).all()
    by_seller = dict(
        session.execute(
            select(Listing.seller_type, func.count(Listing.id))
            .where(active, original)
            .group_by(Listing.seller_type)
        ).all()
    )
    by_city = session.execute(
        select(Listing.city, func.count(Listing.id))
        .where(active, original, Listing.city.is_not(None))
        .group_by(Listing.city)
        .order_by(desc(func.count(Listing.id)))
        .limit(12)
    ).all()

    median_price_m2 = session.scalar(
        select(func.avg(Listing.price_per_m2)).where(
            active, original, Listing.price_per_m2.is_not(None),
            Listing.property_type == PropertyType.MIESZKANIE,
        )
    )

    return {
        "total": count(active),
        "original": count(active, original),
        "copies": count(active, Listing.is_original.is_(False)),
        "today": count(active, Listing.first_seen_at >= now - timedelta(days=1)),
        "week": count(active, Listing.first_seen_at >= now - timedelta(days=7)),
        "price_drops": count(active, original, Listing.initial_price.is_not(None),
                             Listing.price < Listing.initial_price),
        "auctions": count(active, Listing.kind == OfferKind.LICYTACJA),
        "tenders": count(active, Listing.kind == OfferKind.PRZETARG),
        "with_phone": count(active, original, Listing.phones.any()),
        "agencies": int(session.scalar(select(func.count(Agency.id))) or 0),
        "avg_price_m2": round(median_price_m2, 0) if median_price_m2 else None,
        "by_kind": {k.value if hasattr(k, "value") else str(k): v for k, v in by_kind.items()},
        "by_seller": {k.value if hasattr(k, "value") else str(k): v for k, v in by_seller.items()},
        "by_source": [{"source": k, "count": v} for k, v in by_source],
        "by_city": [{"city": k, "count": v} for k, v in by_city],
    }

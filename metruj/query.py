"""Wspólny builder filtrów — używany przez API, interfejs web i alerty.

Dzięki jednemu miejscu zapisane poszukiwanie („Poszukiwania") filtruje dokładnie
tak samo jak widok listy, więc to, co widzisz na ekranie, dostaniesz też
powiadomieniem.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, and_, desc, func, or_, select
from sqlalchemy.orm import defer

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
from .utils.text import polish_midnight_utc, to_polish_time

SORTS = {
    # „Najnowsze" to najświeżej **wystawione**, nie najświeżej przez nas
    # zauważone — patrz `Listing.listed_at`.
    "najnowsze": (Listing.listed_at, True),
    "najstarsze": (Listing.listed_at, False),
    "cena_rosnaco": (Listing.price, False),
    "cena_malejaco": (Listing.price, True),
    "cena_m2_rosnaco": (Listing.price_per_m2, False),
    "cena_m2_malejaco": (Listing.price_per_m2, True),
    "powierzchnia": (Listing.area, True),
    "powierzchnia_rosnaco": (Listing.area, False),
    "dzialka": (Listing.plot_area, True),
    # Im niższy stosunek do mediany, tym większa okazja — stąd rosnąco.
    "okazje": (Listing.deal_ratio, False),
    "najdluzej_wisi": (Listing.listed_at, False),
    "najwiecej_kopii": (Listing.copies_count, True),
    "termin_licytacji": (Listing.event_date, False),
}

#: Etykiety sortowań dla interfejsu — jedno miejsce zamiast listy w szablonie.
SORT_LABELS = {
    "najnowsze": "od najnowszych",
    "okazje": "największa okazja",
    "cena_rosnaco": "od najtańszych",
    "cena_malejaco": "od najdroższych",
    "cena_m2_rosnaco": "najtańsze za m²",
    "cena_m2_malejaco": "najdroższe za m²",
    "powierzchnia": "największe",
    "powierzchnia_rosnaco": "najmniejsze",
    "dzialka": "największa działka",
    "najdluzej_wisi": "najdłużej wiszące",
    "najwiecej_kopii": "najczęściej powtarzane",
    "termin_licytacji": "najbliższy termin licytacji",
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
    voivodeship: str | None = None
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
    plot_area_min: float | None = None
    plot_area_max: float | None = None
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
    #: Maksymalny stosunek ceny za metr do mediany rynkowej. 0,85 znaczy
    #: „pokaż tylko oferty co najmniej 15% tańsze niż reszta w okolicy".
    deal_max: float | None = None
    #: Do czego wolno porównywać: „miasto", „powiat", „wojewodztwo".
    #: Mieszkanie we wsi porównane z medianą całego województwa zawsze wyjdzie
    #: na okazję życia — i nigdy nią nie będzie. Lista okazji domyślnie żąda
    #: więc odniesienia z miasta albo powiatu.
    deal_level: list[str] = field(default_factory=list)
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


def place_clause(column, value: str):
    """Dokładne dopasowanie dla nazw z rejestru, przedrostkowe dla reszty.

    Rejestr szuka bez ogonków i wielkości liter, więc „wroclaw" go znajduje —
    ale w bazie stoi „Wrocław". Porównujemy więc z nazwą z rejestru, a nie
    z tym, co ktoś wpisał; inaczej trafienie w rejestrze dawało zero ofert.
    """
    from .geo import lookup

    name = value.strip()
    if hits := lookup(name):
        names = sorted({unit.name for unit in hits})
        return column == names[0] if len(names) == 1 else column.in_(names)
    return _fold_like(column, f"{_escape_like(_fold_text(name))}%")


def _escape_like(text: str) -> str:
    """`%` i `_` wpisane przez użytkownika to zwykłe znaki, nie wzorzec."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fold_text(text: str) -> str:
    from .utils.text import deaccent

    return deaccent(text).lower().strip()


def _is_sqlite() -> bool:
    from .settings import get_settings

    return get_settings().database_url.startswith("sqlite")


def _fold_like(column, pattern: str):
    """LIKE bez względu na wielkość liter i polskie znaki.

    Na SQLite przez funkcję `pl_fold` rejestrowaną przy połączeniu; na innych
    bazach zostaje `ILIKE`, które wielkość liter obsługuje samo.
    """
    if _is_sqlite():
        return func.pl_fold(column).like(pattern, escape="\\")
    return column.ilike(pattern, escape="\\")


#: Typ ulicy, którego ludzie używają zamiennie albo wcale: „ul. Browarna",
#: „Browarna", „al. Pokoju", „aleja Pokoju".
_STREET_PREFIX = re.compile(
    r"^\s*(?:ul(?:ica|icy)?|al(?:eja|eje|ei)?|os(?:iedle|iedlu)?|pl(?:ac)?|"
    r"skwer|rondo|bulwar|droga)\b\.?\s*",
    re.I,
)


def strip_street_prefix(value: str) -> str:
    """Zdejmuje typ ulicy, także podwojony („Al. Aleja Jana Pawła II")."""
    text = value.strip()
    for _ in range(3):
        shorter = _STREET_PREFIX.sub("", text)
        if shorter == text:
            break
        text = shorter
    return text


def street_terms(value: str | None) -> list[str]:
    """Słowa nazwy ulicy do dopasowania, bez typu ulicy i numeru domu.

    Każde słowo musi wystąpić, w dowolnej kolejności — „Pawła II" znajduje
    „Jana Pawła II", a „Miłosza Czesława" to samo co „Czesława Miłosza".
    """
    from .geo.streets import split_house_number

    if not value:
        return []
    text = strip_street_prefix(value)
    text = split_house_number(text)[0] or text
    return [_stem(_fold_text(word)) for word in re.split(r"[\s,.]+", text) if word][:6]


#: Ogłoszenia piszą „przy ulicy Ozimskiej", a szukający „Ozimska". Ucinamy
#: końcówkę przymiotnika, żeby jedno trafiało w drugie.
_ADJ_ENDING = re.compile(r"(?<=[a-z]{4})(?:iej|ej|a)$")


def _stem(term: str) -> str:
    return _ADJ_ENDING.sub("", term)


def _street_clause(value: str):
    terms = street_terms(value)
    if not terms:
        return None
    return and_(
        *(_fold_like(Listing.street, f"%{_escape_like(term)}%") for term in terms)
    )


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

    # Nazwę, którą zna rejestr TERYT, dopasowujemy dokładnie — wtedy zapytanie
    # korzysta z indeksu. Nieznaną traktujemy jako początek nazwy („Kędzierzyn"),
    # co też jest indeksowalne. Wzorzec „%nazwa%" wymuszał przejście po całej
    # tabeli: przy 59 tysiącach ofert strona wyników wstawała 2,7 sekundy,
    # a docelowo ofert ma być kilkaset tysięcy.
    if f.city:
        clauses.append(place_clause(Listing.city, f.city))
    if f.district:
        clauses.append(
            _fold_like(Listing.district, f"{_escape_like(_fold_text(f.district))}%")
        )
    if f.county:
        clauses.append(place_clause(Listing.county, f.county))
    if f.voivodeship:
        clauses.append(Listing.voivodeship == f.voivodeship)
    if f.street and (street := _street_clause(f.street)) is not None:
        clauses.append(street)
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
        (Listing.plot_area, f.plot_area_min, f.plot_area_max),
        (Listing.rooms, f.rooms_min, f.rooms_max),
        (Listing.floor, f.floor_min, f.floor_max),
    ):
        if low is not None:
            clauses.append(column >= low)
        if high is not None:
            clauses.append(column <= high)
    if f.year_min is not None:
        clauses.append(Listing.year_built >= f.year_min)

    # Filtrując po serwisie, użytkownik pyta „co jest na tym portalu" — a nie
    # „co ten portal miał jako pierwszy". Bez tego wyjątku wybór Gratki dawał
    # dwie oferty zamiast sześćdziesięciu, bo resztę przypisano portalowi,
    # który wystawił je wcześniej.
    if f.only_original and not f.source:
        clauses.append(Listing.is_original.is_(True))
    if f.only_active:
        clauses.append(Listing.status == ListingStatus.AKTYWNA)

    if f.with_phone:
        # „z telefonem" znaczy: da się zadzwonić. Numer z ogłoszenia albo
        # centrala biura, które tę ofertę wystawiło — jedno i drugie się liczy.
        clauses.append(
            or_(
                Listing.phones.any(),
                Listing.agency.has(Agency.phones != []),
            )
        )

    if f.period and f.period in PERIODS:
        # „Dodane w tym tygodniu" znaczy: wystawione na portalu, a nie
        # zauważone przez nas. Wcześniej liczyła się data pierwszego
        # spotkania, więc tydzień po starcie serwisu każda oferta wyglądała
        # na świeżą. Datę z portalu mamy przy 87% ogłoszeń; przy pozostałych
        # zostaje moment, w którym je zobaczyliśmy.
        if f.period == "dzis":
            since = polish_midnight_utc()
        else:
            since = utcnow() - timedelta(days=PERIODS[f.period])
        clauses.append(Listing.listed_at >= since)

    now = utcnow()
    # „Jak długo wisi" liczone od wystawienia — tak samo jak licznik „na rynku"
    # na karcie oferty. Wcześniej filtr liczył od naszego pierwszego spotkania,
    # a karta od daty z portalu, więc oferta „na rynku 8 miesięcy" nie łapała
    # się do filtra „wisi ponad 30 dni".
    if f.days_on_market_min is not None:
        clauses.append(Listing.listed_at <= now - timedelta(days=f.days_on_market_min))
    if f.days_on_market_max is not None:
        clauses.append(Listing.listed_at >= now - timedelta(days=f.days_on_market_max))

    if f.price_dropped:
        clauses.append(and_(Listing.initial_price.is_not(None), Listing.price < Listing.initial_price))

    if f.deal_max is not None:
        clauses.append(and_(Listing.deal_ratio.is_not(None), Listing.deal_ratio <= f.deal_max))
    if f.deal_level:
        clauses.append(Listing.deal_level.in_(f.deal_level))

    if f.q:
        pattern = f"%{_escape_like(_fold_text(f.q))}%"
        clauses.append(
            or_(*(
                _fold_like(column, pattern)
                for column in (
                    Listing.title, Listing.description, Listing.street,
                    Listing.case_number, Listing.authority, Listing.seller_name,
                )
            ))
        )

    return stmt.where(*clauses) if clauses else stmt


def apply_sort(stmt: Select, sort: str = "najnowsze") -> Select:
    """Sortowanie z pustymi wartościami zawsze na końcu.

    SQLite domyślnie stawia NULL-e na początku przy sortowaniu rosnącym, więc
    „najtańsze najpierw" pokazywało stronę ofert *bez podanej ceny*. Oferta bez
    ceny nie jest najtańsza — jest nieznana, a więc jej miejsce jest na końcu.
    """
    from sqlalchemy import case, nullslast

    if sort == "termin_licytacji":
        # „Najbliższy termin" to najbliższy *przed nami*. Samo rosnąco po dacie
        # stawiało na początku licytacje, które już się odbyły. Terminy są
        # w czasie polskim, więc i „teraz" liczymy po polsku.
        now = to_polish_time(utcnow())
        passed = case((Listing.event_date < now, 1), else_=0)
        return stmt.order_by(
            passed, nullslast(Listing.event_date.asc()), desc(Listing.id)
        )

    column, descending = SORTS.get(sort, SORTS["najnowsze"])
    order = desc(column) if descending else column.asc()
    return stmt.order_by(nullslast(order), desc(Listing.id))


def search_listings(session, filters: Filters) -> tuple[list[Listing], int]:
    """Zwraca (strona wyników, łączna liczba trafień).

    Telefony dociągamy jednym dodatkowym zapytaniem (`selectinload`), a nie
    osobnym dla każdej oferty — przy 25 pozycjach na stronie to różnica
    między 2 a 26 zapytaniami do bazy.
    """
    from sqlalchemy.orm import selectinload

    base = select(Listing).options(
        selectinload(Listing.phones), selectinload(Listing.agency), defer(Listing.raw)
    )
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
        .order_by(desc(Listing.listed_at))
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

    # Tylko SPRZEDAŻ: przy wynajmie "cena za m²" znaczy zupełnie co innego
    # (złotych za metr miesięcznie), więc wrzucona do jednej średniej
    # zaniżała ją kilkukrotnie.
    median_price_m2 = session.scalar(
        select(func.avg(Listing.price_per_m2)).where(
            active, original, Listing.price_per_m2.is_not(None),
            Listing.property_type == PropertyType.MIESZKANIE,
            Listing.transaction == TransactionType.SPRZEDAZ,
        )
    )

    return {
        "total": count(active),
        "original": count(active, original),
        "copies": count(active, Listing.is_original.is_(False)),
        # „Dodane dziś" = wystawione dziś, a nie zebrane dziś przez nas.
        "today": count(active, Listing.listed_at >= polish_midnight_utc()),
        "week": count(active, Listing.listed_at >= now - timedelta(days=7)),
        "price_drops": count(active, original, Listing.initial_price.is_not(None),
                             Listing.price < Listing.initial_price),
        "auctions": count(active, Listing.kind == OfferKind.LICYTACJA),
        "tenders": count(active, Listing.kind == OfferKind.PRZETARG),
        # „z kontaktem" liczymy tak samo jak filtr: numer z ogłoszenia albo
        # centrala biura, które ofertę wystawiło
        "with_phone": count(
            active,
            original,
            or_(Listing.phones.any(), Listing.agency.has(Agency.phones != [])),
        ),
        "agencies": int(session.scalar(select(func.count(Agency.id))) or 0),
        # „Okazja" to oferta co najmniej 15% tańsza za metr od mediany swojej
        # miejscowości albo powiatu. Odniesienia do całego województwa tu nie
        # liczymy — mieszkanie we wsi zestawione z medianą województwa zawsze
        # wygląda na okazję i nigdy nią nie jest.
        "bargains": count(
            active, original,
            Listing.deal_ratio.is_not(None),
            Listing.deal_ratio <= 0.85,
            Listing.deal_level.in_(["miasto", "powiat"]),
        ),
        "avg_price_m2": round(median_price_m2, 0) if median_price_m2 else None,
        "by_kind": {k.value if hasattr(k, "value") else str(k): v for k, v in by_kind.items()},
        "by_seller": {k.value if hasattr(k, "value") else str(k): v for k, v in by_seller.items()},
        "by_source": [{"source": k, "count": v} for k, v in by_source],
        "by_city": [{"city": k, "count": v} for k, v in by_city],
    }


# --------------------------------------------------------------------------- #
# Mapa
# --------------------------------------------------------------------------- #
#: oferty bez ulicy siadają na środku miejscowości — bez rozsunięcia setka
#: pinezek nakłada się na jeden punkt i mapa wygląda na pustą
JITTER_DEG = 0.013   # ~1,4 km — tyle mniej więcej znaczy "gdzieś w tym mieście"


def _jitter(listing_id: int, lat: float, lon: float, precision: str | None) -> tuple[float, float]:
    """Deterministyczne rozsunięcie punktów o dokładności do miejscowości.

    Deterministyczne, bo pinezka nie może skakać przy każdym odświeżeniu.
    Danych w bazie nie ruszamy — to wyłącznie sposób rysowania.
    """
    if precision == "address":
        return lat, lon
    spread = JITTER_DEG if precision == "city" else JITTER_DEG / 4
    angle = (listing_id * 137.508) % 360  # kąt złoty — równomierny rozrzut
    radius = spread * (((listing_id * 31) % 100) / 100) ** 0.5
    return (
        lat + radius * math.cos(math.radians(angle)),
        lon + radius * math.sin(math.radians(angle)) * 1.55,  # korekta na szerokość PL
    )


def map_points(session, filters: Filters) -> list[dict]:
    """Lekki GeoJSON: tylko pola, które mapa rysuje w dymku."""
    stmt = select(
        Listing.id, Listing.lat, Listing.lon, Listing.geo_precision, Listing.title,
        Listing.price, Listing.price_per_m2, Listing.area, Listing.rooms,
        Listing.city, Listing.street, Listing.kind, Listing.property_type,
        Listing.seller_type, Listing.source_key, Listing.copies_count,
        Listing.first_seen_at, Listing.listed_at, Listing.url,
    ).where(Listing.lat.is_not(None), Listing.lon.is_not(None))
    stmt = apply_filters(stmt, filters)
    stmt = stmt.order_by(desc(Listing.listed_at)).limit(filters.per_page)

    now = utcnow()
    features = []
    for row in session.execute(stmt).all():
        lat, lon = _jitter(row.id, row.lat, row.lon, row.geo_precision)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                "properties": {
                    "id": row.id,
                    "t": row.title[:110],
                    "p": row.price,
                    "m2": row.price_per_m2,
                    "a": row.area,
                    "r": row.rooms,
                    "c": row.city,
                    "st": row.street,
                    "k": row.kind.value if row.kind else None,
                    "pt": row.property_type.value if row.property_type else None,
                    "s": row.seller_type.value if row.seller_type else None,
                    "src": row.source_key,
                    "cop": row.copies_count,
                    "d": max(0, (now - (row.listed_at or row.first_seen_at)).days),
                    "prec": row.geo_precision,
                    "u": row.url,
                },
            }
        )
    return features


# --------------------------------------------------------------------------- #
# Podobne oferty
# --------------------------------------------------------------------------- #
#: Ile ofert pokazujemy pod ogłoszeniem.
SIMILAR_LIMIT = 8
#: Pasmo ceny i metrażu wokół oglądanej oferty.
SIMILAR_PRICE = (0.75, 1.3)
SIMILAR_AREA = (0.75, 1.3)
#: Ilu kandydatów z jednego poziomu (miejscowość, powiat, województwo)
#: bierzemy do porównania, zanim ułożymy ich według podobieństwa.
SIMILAR_POOL = 300


@dataclass
class SimilarOffers:
    listings: list[Listing]
    #: nazwa miejsca, od którego zaczęliśmy („Wrocław")
    place: str | None
    #: dokąd trzeba było rozszerzyć poszukiwania („pow. żniński"), jeśli w samej
    #: miejscowości było za mało ofert
    widened_to: str | None
    #: lista ofert z tymi samymi warunkami — do „zobacz więcej"
    more_query: dict[str, Any]


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Odległość po kuli ziemskiej w kilometrach."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def similar_listings(session, listing: Listing, limit: int = SIMILAR_LIMIT) -> SimilarOffers:
    """Oferty podobne do oglądanej — tak, jak szuka ich kupujący.

    Ten sam rodzaj nieruchomości i ta sama transakcja, cena i metraż w paśmie
    wokół tej oferty, przy mieszkaniach i domach liczba pokoi ±1. Szukamy
    najpierw w tej samej miejscowości, a gdy tam jest za mało — w powiecie
    i w województwie. Pomijamy samą ofertę, jej powtórki z innych portali
    i oferty nieaktualne.

    Kandydatów układamy według podobieństwa: różnica ceny i metrażu (w skali
    logarytmicznej — 10% różnicy waży tyle samo przy kawalerce i przy
    domu), pokoi i odległości na mapie. Oferty ze zdjęciem idą przed
    ofertami bez zdjęcia, bo z samej ceny trudno ocenić podobieństwo.
    """
    conditions = [
        Listing.id != listing.id,
        Listing.status == ListingStatus.AKTYWNA,
        Listing.is_original.is_(True),
        Listing.kind == listing.kind,
        Listing.property_type == listing.property_type,
        Listing.transaction == listing.transaction,
    ]
    if listing.duplicate_of_id:
        conditions.append(Listing.id != listing.duplicate_of_id)
    more: dict[str, Any] = {
        "property_type": listing.property_type.value,
        "transaction": listing.transaction.value,
    }
    if listing.kind != OfferKind.NIERUCHOMOSC:
        more["kind"] = listing.kind.value
    if listing.price:
        low, high = SIMILAR_PRICE
        conditions.append(Listing.price.between(listing.price * low, listing.price * high))
        more["price_min"] = int(round(listing.price * low, -3))
        more["price_max"] = int(round(listing.price * high, -3))
    if listing.area:
        low, high = SIMILAR_AREA
        conditions.append(Listing.area.between(listing.area * low, listing.area * high))
        more["area_min"] = int(listing.area * low)
        more["area_max"] = int(math.ceil(listing.area * high))
    rooms_matter = listing.property_type in (PropertyType.MIESZKANIE, PropertyType.DOM)
    if listing.rooms and rooms_matter:
        conditions.append(or_(
            Listing.rooms.is_(None),
            Listing.rooms.between(listing.rooms - 1, listing.rooms + 1),
        ))
        more["rooms_min"] = max(1, listing.rooms - 1)
        more["rooms_max"] = listing.rooms + 1

    # Od najbliższej okolicy do coraz szerszej. Miejscowość zawsze razem
    # z województwem — „Nowa Wieś" jest w każdym z nich.
    levels: list[tuple[str, str, list, dict[str, Any]]] = []
    if listing.city:
        where = [Listing.city == listing.city]
        if listing.voivodeship:
            where.append(Listing.voivodeship == listing.voivodeship)
        levels.append(("miejscowość", listing.city, where, {"city": listing.city}))
    if listing.county:
        levels.append(("powiat", f"pow. {listing.county}", [Listing.county == listing.county],
                       {"county": listing.county}))
    if listing.voivodeship:
        levels.append(("województwo", f"woj. {listing.voivodeship}",
                       [Listing.voivodeship == listing.voivodeship],
                       {"voivodeship": listing.voivodeship}))

    def closeness(other: Listing) -> float:
        score = 0.0
        if listing.price and other.price:
            score += abs(math.log(other.price / listing.price))
        if listing.area:
            score += abs(math.log(other.area / listing.area)) if other.area else 0.5
        if listing.rooms and rooms_matter:
            score += 0.15 * abs((other.rooms or listing.rooms) - listing.rooms)
            score += 0.1 if other.rooms is None else 0.0
        if listing.lat and listing.lon and other.lat and other.lon:
            score += min(_km(listing.lat, listing.lon, other.lat, other.lon), 40) / 40
        if listing.district and other.district == listing.district:
            score -= 0.1
        if not other.images:
            score += 0.25
        return score

    # Wstępna kolejność w bazie: względna różnica ceny i metrażu. Wyrażenie
    # zamiast sortowania po dacie — przy „ORDER BY listed_at" SQLite szedł
    # po indeksie dat przez całą tabelę zamiast po indeksie miejscowości
    # i jedno otwarcie oferty trwało ponad sekundę.
    nearness = []
    if listing.price:
        nearness.append(func.abs(func.coalesce(Listing.price, 0) - listing.price) / listing.price)
    if listing.area:
        nearness.append(func.abs(func.coalesce(Listing.area, 0) - listing.area) / listing.area)
    order = sum(nearness[1:], nearness[0]) if nearness else func.abs(Listing.id - listing.id)

    chosen: list[Listing] = []
    place = widened_to = None
    for _label, name, where, query in levels:
        # Surowa odpowiedź portalu i pełny opis ważą więcej niż cała reszta
        # wiersza, a do porównania i do karty nie są potrzebne.
        candidates = session.scalars(
            select(Listing)
            .options(defer(Listing.raw), defer(Listing.description), defer(Listing.extra))
            .where(*conditions, *where).order_by(order).limit(SIMILAR_POOL)
        ).all()
        added = 0
        for other in sorted(candidates, key=closeness):
            if len(chosen) >= limit:
                break
            if _same_property(listing, other) or any(_same_property(c, other) for c in chosen):
                continue
            if any(c.id == other.id for c in chosen):
                continue
            chosen.append(other)
            added += 1
        if place is None:
            place = name
            more.update(query)
        elif added:
            widened_to = name
            for key in ("city", "county", "voivodeship"):
                more.pop(key, None)
            more.update(query)
        if len(chosen) >= limit:
            break
    return SimilarOffers(listings=chosen, place=place, widened_to=widened_to, more_query=more)


def _same_property(listing: Listing, other: Listing) -> bool:
    """Ta sama nieruchomość, której nie skleiliśmy w jeden wpis.

    Podobna oferta to inna nieruchomość w podobnej cenie — nie to samo
    ogłoszenie z drugiego portalu („Dom gmina Janowiec Wlkp." za 489 tys.
    i 99 m² z GetHome i z OLX) ani odświeżone pod nowym numerem.
    """
    if not (listing.price and other.price and listing.area and other.area):
        return False
    return (
        abs(other.price - listing.price) <= 0.005 * listing.price
        and abs(other.area - listing.area) < 1.0
        and (other.city or "").lower() == (listing.city or "").lower()
    )

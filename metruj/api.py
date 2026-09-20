"""FastAPI: interfejs webowy + REST.

Ten sam builder filtrów obsługuje widok HTML i endpointy JSON, więc lista
w przeglądarce i wynik `/api/listings` zawsze się zgadzają.
"""

from __future__ import annotations

import math
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
from starlette.middleware.cors import CORSMiddleware

from . import __version__
from .contacts import contacts_for
from .db import get_session_factory, init_db, session_scope
from .geo import counties, town_names
from .models import (
    Agency,
    DuplicateLink,
    Favorite,
    Listing,
    MarketStat,
    OfferKind,
    SavedSearch,
    ScanRun,
    Source,
    TransactionType,
    utcnow,
)
from .query import (
    SORT_LABELS,
    Filters,
    apply_filters,
    apply_sort,
    dashboard_stats,
    map_points,
    phone_lookup,
    search_listings,
)
from .settings import get_settings

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


def _asset_version(name: str) -> str:
    """Znacznik zmiany pliku statycznego, doklejany do adresu.

    Statyki serwujemy z długim cache, żeby strona wstawała natychmiast. Bez
    takiego znacznika każda poprawka stylu byłaby niewidoczna dla wracających
    użytkowników przez dobę — a przy pracy nad wyglądem to oznacza wrażenie,
    że "nic się nie zmieniło".
    """
    path = BASE_DIR / "web" / "static" / name
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return __version__


def asset(name: str) -> str:
    return f"/static/{name}?v={_asset_version(name)}"


def _replace_param(query_string: str, name: str, value: str = "") -> str:
    """Adres tego samego wyszukiwania z jednym warunkiem zdjętym albo zmienionym.

    Używane w komunikacie o pustym wyniku: zamiast samego „nic nie znaleziono"
    dajemy gotowe odnośniki, które odpuszczają po jednym filtrze.
    """
    params = [(k, v) for k, v in parse_qsl(query_string, keep_blank_values=True) if k != name]
    if value:
        params.append((name, value))
    params = [(k, v) for k, v in params if k != "page"]
    return urlencode(params)


@lru_cache(maxsize=1)
def _source_names() -> dict[str, str]:
    """Klucz źródła -> nazwa czytelna dla człowieka.

    Na kartach ofert widać, skąd pochodzi ogłoszenie. „bip_strzelce_opolskie"
    nikomu nic nie mówi — ma tam stać nazwa urzędu albo portalu.
    """
    with session_scope() as session:
        return {s.key: s.name for s in session.scalars(select(Source))}


def source_name(key: str) -> str:
    name = _source_names().get(key)
    if name:
        # Nazwy w konfiguracji bywają opisowe („BIP Gminy Nysa — nieruchomości");
        # na kartę wystarczy część przed myślnikiem.
        return name.split(" — ")[0]
    return key.replace("_", " ")


templates.env.globals["source_name"] = source_name
templates.env.filters["replace_param"] = _replace_param
templates.env.globals["asset"] = asset
# Szablon karty oferty sam pyta o numery kontaktowe — inaczej każdy widok
# musiałby je przekazywać osobno i łatwo byłoby o tym zapomnieć.
templates.env.globals["contacts_for"] = contacts_for
templates.env.globals["SORT_LABELS"] = SORT_LABELS


#: Ludzkie nazwy filtrów do paska nad wynikami. Bez nich pasek pokazywałby
#: „price_max = 400000", a ma pokazywać „do 400 000 zł".
FILTER_LABELS: dict[str, str] = {
    "property_type": "rodzaj",
    "transaction": "transakcja",
    "city": "miejscowość",
    "district": "dzielnica",
    "county": "powiat",
    "voivodeship": "województwo",
    "street": "ulica",
    "seller_type": "wystawia",
    "source": "serwis",
    "market": "rynek",
    "period": "dodane",
    "q": "szukane słowo",
}

#: Zakresy pokazujemy jako jeden znacznik: „cena 200 000 – 400 000 zł".
RANGE_LABELS: dict[str, tuple[str, str, str]] = {
    "price": ("price_min", "price_max", "cena"),
    "price_m2": ("price_m2_min", "price_m2_max", "cena za m²"),
    "area": ("area_min", "area_max", "powierzchnia"),
    "plot_area": ("plot_area_min", "plot_area_max", "działka"),
    "rooms": ("rooms_min", "rooms_max", "pokoje"),
    "floor": ("floor_min", "floor_max", "piętro"),
    "days_on_market": ("days_on_market_min", "days_on_market_max", "wisi dni"),
}

FLAG_LABELS: dict[str, str] = {
    "with_phone": "da się zadzwonić",
    "price_dropped": "po obniżce",
}

PERIOD_LABELS = {
    "dzis": "dzisiaj", "7dni": "w tym tygodniu", "30dni": "w tym miesiącu",
    "90dni": "w 3 miesiące", "1rok": "w ciągu roku",
}


def _number(value: str) -> str:
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return value


def active_filters(filters: Filters, query_string: str) -> list[dict[str, str]]:
    """Znaczniki aktywnych filtrów z adresem, który każdy z nich zdejmuje.

    Przy pół milionie ofert w całej Polsce najczęstsze pytanie brzmi „dlaczego
    wyników jest sto". Odpowiedź musi być widoczna nad listą, a nie schowana
    w zwiniętej sekcji formularza.
    """
    params = dict(parse_qsl(query_string, keep_blank_values=False))
    chips: list[dict[str, str]] = []

    used: set[str] = set()
    for _, (low_key, high_key, label) in RANGE_LABELS.items():
        low, high = params.get(low_key), params.get(high_key)
        if not low and not high:
            continue
        used |= {low_key, high_key}
        if low and high:
            text = f"{label} {_number(low)}–{_number(high)}"
        elif low:
            text = f"{label} od {_number(low)}"
        else:
            text = f"{label} do {_number(high)}"
        url = _replace_param(_replace_param(query_string, low_key), high_key)
        chips.append({"label": text, "url": url})

    for key, label in FILTER_LABELS.items():
        value = params.get(key)
        if not value or key in used:
            continue
        if key == "period":
            value = PERIOD_LABELS.get(value, value)
        elif key == "source":
            value = source_name(value)
        chips.append({"label": f"{label}: {value}", "url": _replace_param(query_string, key)})

    for key, label in FLAG_LABELS.items():
        if params.get(key) not in (None, "", "0", "false"):
            chips.append({"label": label, "url": _replace_param(query_string, key)})

    if params.get("deal_max"):
        try:
            percent = round((1 - float(params["deal_max"])) * 100)
            chips.append({"label": f"min. {percent}% poniżej mediany",
                          "url": _replace_param(query_string, "deal_max")})
        except ValueError:
            pass

    if params.get("only_original") in ("0", "false"):
        chips.append({"label": "z powtórkami z innych portali",
                      "url": _replace_param(query_string, "only_original")})
    if params.get("only_active") in ("0", "false"):
        chips.append({"label": "także nieaktualne",
                      "url": _replace_param(query_string, "only_active")})
    return chips


def hidden_fields(query_string: str, *skip: str) -> Markup:
    """Ukryte pola odtwarzające bieżące zapytanie — dla formularzy pomocniczych.

    Pasek sortowania jest osobnym formularzem; bez przepisania reszty parametrów
    zmiana kolejności kasowałaby wszystkie ustawione filtry.
    """
    out = []
    for key, value in parse_qsl(query_string, keep_blank_values=False):
        if key in skip or key == "page":
            continue
        out.append(f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">')
    return Markup("".join(out))


templates.env.globals["active_filters"] = active_filters
templates.env.globals["hidden_fields"] = hidden_fields

app = FastAPI(
    title="ogloszenia — otwarty monitor rynku nieruchomości",
    version=__version__,
    description=(
        "Publiczne, darmowe API z ofertami nieruchomości, licytacjami "
        "komorniczymi i skarbowymi oraz przetargami. Bez kluczy i bez limitów."
    ),
)

# Strony i GeoJSON kompresują się ~8-krotnie — to jest różnica między mapą,
# która wstaje od razu, a taką, na którą się czeka.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

# API jest publiczne i ma być używane także z cudzych stron i skryptów.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    max_age=86400,
)


class CachedStatics(StaticFiles):
    """Statyki z długim cache — przeglądarka pobiera je raz."""

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response


app.mount(
    "/static",
    CachedStatics(directory=str(BASE_DIR / "web" / "static")),
    name="static",
)


def suggest_cities(db: Session, limit: int = 400) -> list[str]:
    """Miejscowości do podpowiedzi w wyszukiwarce.

    Najpierw te, które faktycznie mają oferty — posortowane od najliczniejszych,
    bo o nie ludzie pytają najczęściej. Przy pustej bazie (świeża instalacja)
    zostaje krajowa lista miast z rejestru TERYT, żeby pole podpowiedzi nie
    świeciło pustką.
    """
    rows = db.execute(
        select(Listing.city, func.count(Listing.id).label("n"))
        .where(Listing.city.is_not(None))
        .group_by(Listing.city)
        .order_by(desc("n"))
        .limit(limit)
    ).all()
    found = [row[0] for row in rows if row[0]]
    return found or town_names()[:limit]


#: Filtry schowane w rozwijanej sekcji. Sekcja otwiera się, gdy użytkownik
#: ustawił którykolwiek z nich — ale tylko wtedy, gdy zrobił to sam. Wartości
#: domyślne strony (np. próg okazji na „/okazje") nie mogą rozwijać całego
#: formularza i spychać wyników pod zgięcie.
ADVANCED_FILTERS = (
    "voivodeship", "county", "district", "street", "price_m2_min", "price_m2_max",
    "rooms_min", "rooms_max", "floor_min", "floor_max", "plot_area_min",
    "plot_area_max", "seller_type", "source", "period", "market", "year_min",
    "days_on_market_min", "days_on_market_max", "q", "deal_max",
)


def filter_context(db: Session, request: Request) -> dict[str, Any]:
    """Dane, których potrzebuje panel filtrów — na każdej stronie te same."""
    from .geo import voivodeships

    return {
        "more_open": any(request.query_params.get(key) for key in ADVANCED_FILTERS),
        "cities": suggest_cities(db),
        "counties": counties(),
        "voivodeships": voivodeships(),
        "sources": list(
            db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.name))
        ),
        "query_string": str(request.query_params),
    }


def get_db() -> Session:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


DB = Annotated[Session, Depends(get_db)]


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --------------------------------------------------------------------------- #
# Pomocnicze
# --------------------------------------------------------------------------- #
def _filters_from_query(request: Request) -> Filters:
    params = request.query_params

    def flag(name: str, default: bool) -> bool:
        """Odczyt przełącznika z formularza.

        Niezaznaczony checkbox nie wysyła niczego, więc każdy przełącznik ma
        w formularzu ukryte pole o tej samej nazwie i wartości „0", postawione
        przed nim. Gdy przełącznik jest włączony, przeglądarka wysyła obie
        wartości — liczy się ostatnia. Bez tego filtrów nie dało się wyłączyć:
        wracały do stanu domyślnego przy każdym wyszukiwaniu.
        """
        values = params.getlist(name)
        if not values:
            return default
        return values[-1] not in ("0", "false", "")
    def num(name: str, cast=float):
        raw = params.get(name)
        if raw in (None, ""):
            return None
        try:
            return cast(str(raw).replace(" ", "").replace(",", "."))
        except ValueError:
            return None

    return Filters(
        kind=params.get("kind") or None,
        property_type=params.get("property_type") or None,
        transaction=(params.get("transaction") or None)
        if params.get("transaction") != "wszystkie"
        else None,
        city=params.get("city") or None,
        district=params.get("district") or None,
        county=params.get("county") or None,
        voivodeship=params.get("voivodeship") or None,
        street=params.get("street") or None,
        source=params.getlist("source") or [],
        seller_type=params.get("seller_type") or None,
        agency_id=num("agency_id", int),
        price_min=num("price_min"),
        price_max=num("price_max"),
        price_m2_min=num("price_m2_min"),
        price_m2_max=num("price_m2_max"),
        area_min=num("area_min"),
        area_max=num("area_max"),
        plot_area_min=num("plot_area_min"),
        plot_area_max=num("plot_area_max"),
        rooms_min=num("rooms_min", int),
        rooms_max=num("rooms_max", int),
        floor_min=num("floor_min", int),
        floor_max=num("floor_max", int),
        year_min=num("year_min", int),
        market=params.get("market") or None,
        only_original=flag("only_original", True),
        only_active=flag("only_active", True),
        with_phone=flag("with_phone", False),
        price_dropped=flag("price_dropped", False),
        deal_max=num("deal_max"),
        deal_level=params.getlist("deal_level") or [],
        period=params.get("period") or None,
        days_on_market_min=num("days_on_market_min", int),
        days_on_market_max=num("days_on_market_max", int),
        q=params.get("q") or None,
        sort=params.get("sort") or "najnowsze",
        page=int(params.get("page") or 1),
        per_page=min(int(params.get("per_page") or 25), 100),
    )


def _default_to_sale(request: Request, filters: Filters) -> None:
    """Na widokach przeglądania domyślnie pokazujemy sprzedaż.

    Wynajem i sprzedaż na jednej liście dają bezsens: sortowanie po cenie za m²
    stawia na górze pokój za 299 zł obok mieszkań za pół miliona. Wybór jest
    widoczny w formularzu i jednym kliknięciem zmieniany na wynajem albo na
    wszystko — API zostaje neutralne i niczego nie narzuca.
    """
    if "transaction" not in request.query_params:
        filters.transaction = TransactionType.SPRZEDAZ.value


def listing_to_dict(listing: Listing, *, reveal_phone: bool = False) -> dict[str, Any]:
    return {
        "id": listing.id,
        "external_id": listing.external_id,
        "source": listing.source_key,
        "url": listing.url,
        "kind": listing.kind.value,
        "transaction": listing.transaction.value,
        "property_type": listing.property_type.value,
        "status": listing.status.value,
        "title": listing.title,
        "description": (listing.description or "")[:800],
        "price": listing.price,
        "initial_price": listing.initial_price,
        "price_per_m2": listing.price_per_m2,
        "currency": listing.currency,
        "area": listing.area,
        "plot_area": listing.plot_area,
        "rooms": listing.rooms,
        "floor": listing.floor,
        "floors_total": listing.floors_total,
        "year_built": listing.year_built,
        "market": listing.market,
        # Cena tej oferty wobec mediany okolicy: 1,00 to dokładnie mediana,
        # 0,70 znaczy „trzydzieści procent poniżej". `poziom` mówi, z czym
        # porównujemy — bez tego liczba jest myląca.
        "okazja": {
            "wskaznik": listing.deal_ratio,
            "poziom": listing.deal_level,
            "roznica_proc": round((1 - listing.deal_ratio) * 100, 1)
            if listing.deal_ratio else None,
        }
        if listing.deal_ratio
        else None,
        "location": {
            "voivodeship": listing.voivodeship,
            "county": listing.county,
            "commune": listing.commune,
            "city": listing.city,
            "district": listing.district,
            "street": listing.street,
            "teryt": listing.teryt,
            "lat": listing.lat,
            "lon": listing.lon,
            "precyzja": listing.geo_precision,
        },
        "seller": {
            "type": listing.seller_type.value,
            "name": listing.seller_name,
            "agency_id": listing.agency_id,
        },
        "phones": [p.masked if not reveal_phone else (p.national or p.masked) for p in listing.phones],
        "kontakty": [c.as_dict() for c in contacts_for(listing)],
        "images": listing.images or [],
        "is_original": listing.is_original,
        "copies_count": listing.copies_count,
        "days_on_market": listing.days_on_market,
        "first_seen_at": listing.first_seen_at.isoformat(),
        "last_seen_at": listing.last_seen_at.isoformat(),
        "published_at": listing.published_at.isoformat() if listing.published_at else None,
        "auction": {
            "event_date": listing.event_date.isoformat() if listing.event_date else None,
            "deadline": listing.deadline.isoformat() if listing.deadline else None,
            "opening_price": listing.opening_price,
            "estimate_value": listing.estimate_value,
            "deposit": listing.deposit,
            "case_number": listing.case_number,
            "authority": listing.authority,
        }
        if listing.kind in (OfferKind.LICYTACJA, OfferKind.PRZETARG, OfferKind.WYKAZ)
        else None,
    }


# --------------------------------------------------------------------------- #
# Widoki HTML
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def view_home(request: Request, db: DB):
    """Strona główna: wyszukiwarka i najnowsze oferty.

    Dziennik pracy scrapera trafia na /zrodla, a zestawienia rynkowe na /rynek —
    kto wchodzi tu po mieszkanie, ma zacząć od szukania, a nie od tabeli
    przebiegów zbierania.
    """
    stats = dashboard_stats(db)
    newest = list(
        db.scalars(
            apply_sort(apply_filters(select(Listing), {"only_original": True}), "najnowsze").limit(9)
        )
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {"stats": stats, "newest": newest, "cities": suggest_cities(db), "active": "start"},
    )


@app.get("/rynek", response_class=HTMLResponse)
def view_dashboard(request: Request, db: DB):
    stats = dashboard_stats(db)
    newest = list(
        db.scalars(
            apply_sort(apply_filters(select(Listing), {"only_original": True}), "najnowsze").limit(6)
        )
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"stats": stats, "newest": newest, "active": "rynek"},
    )


@app.get("/nieruchomosci", response_class=HTMLResponse)
def view_listings(request: Request, db: DB):
    filters = _filters_from_query(request)
    _default_to_sale(request, filters)
    listings, total = search_listings(db, filters)
    return templates.TemplateResponse(
        request,
        "listings.html",
        {
            "listings": listings,
            "total": total,
            "filters": filters,
            "pages": max(1, math.ceil(total / filters.per_page)),
            "active": "nieruchomosci",
            **filter_context(db, request),
        },
    )


@app.get("/okazje", response_class=HTMLResponse)
def view_deals(request: Request, db: DB):
    """Oferty tańsze od mediany swojej okolicy.

    Domyślnie: co najmniej 15% poniżej mediany i **tylko** tam, gdzie
    odniesieniem jest miejscowość albo powiat. Porównanie do całego
    województwa zostawiamy zwykłej liście: mieszkanie we wsi zestawione
    z medianą województwa zawsze wygląda na okazję i nigdy nią nie jest.
    """
    filters = _filters_from_query(request)
    _default_to_sale(request, filters)
    if filters.deal_max is None:
        filters.deal_max = 0.85
    if not filters.deal_level:
        filters.deal_level = ["miasto", "powiat"]
    if "sort" not in request.query_params:
        filters.sort = "okazje"
    listings, total = search_listings(db, filters)

    scored = int(db.scalar(
        select(func.count(Listing.id)).where(Listing.deal_ratio.is_not(None))
    ) or 0)
    bargains = int(db.scalar(
        select(func.count(Listing.id)).where(
            Listing.deal_ratio <= 0.8, Listing.deal_level.in_(["miasto", "powiat"])
        )
    ) or 0)
    scopes = int(db.scalar(select(func.count(MarketStat.id))) or 0)
    computed_at = db.scalar(select(func.max(MarketStat.computed_at)))

    return templates.TemplateResponse(
        request,
        "okazje.html",
        {
            "listings": listings,
            "total": total,
            "filters": filters,
            "pages": max(1, math.ceil(total / filters.per_page)),
            "scored": scored,
            "bargains": bargains,
            "scopes": scopes,
            "computed_at": computed_at,
            "active": "okazje",
            "reset_url": "/okazje",
            "form_action": "/okazje",
            **filter_context(db, request),
        },
    )


@app.get("/licytacje", response_class=HTMLResponse)
def view_auctions(request: Request, db: DB):
    filters = _filters_from_query(request)
    filters.kind = filters.kind or OfferKind.LICYTACJA.value
    filters.sort = request.query_params.get("sort") or "termin_licytacji"
    listings, total = search_listings(db, filters)
    return templates.TemplateResponse(
        request,
        "auctions.html",
        {"listings": listings, "total": total, "filters": filters,
         "pages": max(1, math.ceil(total / filters.per_page)), "active": "licytacje",
         "query_string": str(request.query_params)},
    )


@app.get("/przetargi", response_class=HTMLResponse)
def view_tenders(request: Request, db: DB):
    filters = _filters_from_query(request)
    filters.kind = filters.kind or OfferKind.PRZETARG.value
    listings, total = search_listings(db, filters)
    return templates.TemplateResponse(
        request,
        "auctions.html",
        {"listings": listings, "total": total, "filters": filters,
         "pages": max(1, math.ceil(total / filters.per_page)), "active": "przetargi",
         "query_string": str(request.query_params)},
    )


@app.get("/oferta/{listing_id}", response_class=HTMLResponse)
def view_listing(listing_id: int, request: Request, db: DB):
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    copies = list(
        db.scalars(
            select(Listing)
            .join(DuplicateLink, DuplicateLink.copy_id == Listing.id)
            .where(DuplicateLink.original_id == listing.id)
        )
    )
    original = db.get(Listing, listing.duplicate_of_id) if listing.duplicate_of_id else None
    agency = db.get(Agency, listing.agency_id) if listing.agency_id else None
    return templates.TemplateResponse(
        request,
        "listing.html",
        {"listing": listing, "copies": copies, "original": original, "agency": agency,
         "active": "nieruchomosci"},
    )


@app.get("/mapa", response_class=HTMLResponse)
def view_map(request: Request, db: DB):
    filters = _filters_from_query(request)
    _default_to_sale(request, filters)
    return templates.TemplateResponse(
        request,
        "map.html",
        {"filters": filters, "active": "mapa", **filter_context(db, request)},
    )


@app.get("/biura", response_class=HTMLResponse)
def view_agencies(request: Request, db: DB):
    """Rejestr biur nieruchomości i deweloperów z całego kraju.

    Domyślnie pokazujemy **wszystkie** zarejestrowane firmy, a nie tylko te,
    których ogłoszenia zdążyliśmy zebrać: lista pośredników jest sama w sobie
    czymś, czego nigdzie indziej za darmo nie ma, a telefon do biura przydaje
    się także wtedy, gdy jego ofert jeszcze u nas nie ma.
    """
    from .geo import voivodeships

    params = request.query_params
    query = params.get("q") or ""
    city = params.get("city") or ""
    voivodeship = params.get("voivodeship") or ""
    only_with_offers = params.get("only_with_offers") in ("1", "true")
    page = max(1, int(params.get("page") or 1))
    per_page = 100

    stmt = select(Agency)
    if query:
        stmt = stmt.where(Agency.name.ilike(f"%{query}%"))
    if city:
        stmt = stmt.where(Agency.city.ilike(f"%{city}%"))
    if voivodeship:
        stmt = stmt.where(Agency.voivodeship == voivodeship)
    if only_with_offers:
        stmt = stmt.where(Agency.listings_count > 0)

    total = int(db.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0)
    agencies = list(
        db.scalars(
            stmt.order_by(
                desc(Agency.listings_count), desc(Agency.listings_expected), Agency.name
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    )
    registry_total = db.scalar(select(func.count(Agency.id))) or 0
    with_offers = db.scalar(
        select(func.count(Agency.id)).where(Agency.listings_count > 0)
    ) or 0

    return templates.TemplateResponse(
        request,
        "agencies.html",
        {
            "agencies": agencies,
            "total": total,
            "q": query,
            "city": city,
            "voivodeship": voivodeship,
            "voivodeships": voivodeships(),
            "only_with_offers": only_with_offers,
            "registry_total": registry_total,
            "with_offers": with_offers,
            "page": page,
            "pages": max(1, math.ceil(total / per_page)),
            "query_string": str(request.query_params),
            "active": "biura",
        },
    )


@app.get("/zrodla", response_class=HTMLResponse)
def view_sources(request: Request, db: DB):
    sources = list(db.scalars(select(Source).order_by(Source.category, Source.name)))
    runs = {
        row[0]: row[1]
        for row in db.execute(
            select(ScanRun.source_key, func.max(ScanRun.started_at)).group_by(ScanRun.source_key)
        ).all()
    }
    recent = list(db.scalars(select(ScanRun).order_by(desc(ScanRun.started_at)).limit(20)))
    return templates.TemplateResponse(
        request,
        "sources.html",
        {"sources": sources, "runs": runs, "recent": recent, "active": "zrodla"},
    )


@app.get("/poszukiwania", response_class=HTMLResponse)
def view_searches(request: Request, db: DB):
    searches = list(db.scalars(select(SavedSearch).order_by(desc(SavedSearch.created_at))))
    return templates.TemplateResponse(
        request, "searches.html", {"searches": searches, "active": "poszukiwania"}
    )


@app.get("/schowek", response_class=HTMLResponse)
def view_favorites(request: Request, db: DB):
    rows = db.execute(
        select(Favorite, Listing).join(Listing, Listing.id == Favorite.listing_id)
        .order_by(desc(Favorite.created_at))
    ).all()
    return templates.TemplateResponse(
        request, "favorites.html", {"rows": rows, "active": "schowek"}
    )


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def api_health(db: DB) -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "listings": int(db.scalar(select(func.count(Listing.id))) or 0),
        "sources_enabled": int(
            db.scalar(select(func.count(Source.id)).where(Source.enabled.is_(True))) or 0
        ),
    }


@app.get("/api/listings")
def api_listings(request: Request, db: DB) -> dict:
    filters = _filters_from_query(request)
    listings, total = search_listings(db, filters)
    return {
        "total": total,
        "page": filters.page,
        "per_page": filters.per_page,
        "items": [listing_to_dict(x) for x in listings],
    }


@app.get("/api/listings/{listing_id}")
def api_listing(listing_id: int, db: DB) -> dict:
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    data = listing_to_dict(listing)
    data["price_history"] = [
        {"price": h.price, "previous": h.previous_price, "delta_pct": h.delta_pct,
         "at": h.changed_at.isoformat()}
        for h in listing.price_history
    ]
    data["copies"] = [
        {"id": c.id, "source": c.source_key, "url": c.url, "price": c.price}
        for c in db.scalars(
            select(Listing)
            .join(DuplicateLink, DuplicateLink.copy_id == Listing.id)
            .where(DuplicateLink.original_id == listing.id)
        )
    ]
    return data


@app.get("/api/listings/{listing_id}/kontakt")
def api_listing_contacts(listing_id: int, db: DB) -> dict:
    """Numery kontaktowe oferty wraz z pochodzeniem (ogłoszenie / centrala biura)."""
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    return {
        "listing_id": listing.id,
        "kontakty": [c.as_dict() for c in contacts_for(listing)],
    }


@app.get("/api/listings/{listing_id}/phone")
def api_listing_phone(listing_id: int, db: DB) -> dict:
    """Ujawnienie pełnego numeru — świadoma, pojedyncza akcja użytkownika.

    Na liście numery są maskowane (`537 *** ***`). Pełny numer wymaga wejścia
    tutaj, dzięki czemu nie da się jednym zapytaniem pobrać całej bazy numerów.
    """
    if get_settings().store_phone_hash_only:
        raise HTTPException(403, "Tryb prywatności: numery nie są przechowywane")
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    return {
        "listing_id": listing.id,
        "phones": [
            {"e164": p.e164, "national": p.national, "origin": p.origin} for p in listing.phones
        ],
    }


@app.get("/api/phone-lookup")
def api_phone_lookup(db: DB, number: str = Query(min_length=4)) -> dict:
    listings = phone_lookup(db, number)
    return {
        "count": len(listings),
        "items": [
            {"id": x.id, "title": x.title, "source": x.source_key, "price": x.price,
             "city": x.city, "url": x.url, "seller_type": x.seller_type.value,
             "first_seen_at": x.first_seen_at.isoformat()}
            for x in listings
        ],
    }


@app.get("/api/geojson")
def api_geojson(request: Request, db: DB, limit: int = 5000) -> JSONResponse:
    """Punkty na mapę w formacie GeoJSON.

    Zwraca tylko to, czego mapa naprawdę potrzebuje (bez opisów i zdjęć), więc
    nawet kilka tysięcy ofert to kilkaset kilobajtów. Odpowiedź jest oznaczona
    do cache'owania na minutę — przesuwanie mapy nie odpytuje bazy od nowa.
    """
    filters = _filters_from_query(request)
    filters.per_page = min(limit, 20000)
    features = map_points(db, filters)
    payload = {
        "type": "FeatureCollection",
        "count": len(features),
        "features": features,
    }
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})


@app.get("/api/listings/{listing_id}/okolica")
async def api_surroundings(listing_id: int, db: DB, radius: int = 1000) -> dict:
    """Co jest w okolicy oferty — z OpenStreetMap, liczone na żądanie."""
    from .pipeline.geocode import enrich_surroundings

    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    if listing.lat is None:
        return {"listing_id": listing_id, "poi": {}, "info": "oferta nie ma współrzędnych"}
    summary = await enrich_surroundings(listing_id, radius_m=radius)
    return {"listing_id": listing_id, "radius_m": radius, "poi": summary}


@app.get("/api/listings/{listing_id}/dzialka")
async def api_parcel(listing_id: int, db: DB) -> dict:
    """Działka ewidencyjna pod ofertą — z rejestru GUGiK.

    Przydatne zwłaszcza przy gruntach i licytacjach: dostajemy identyfikator
    działki, po którym da się sprawdzić księgę wieczystą, oraz jej rzeczywisty
    kształt. Tego nie podaje żaden portal ogłoszeniowy.
    """
    from .apis.gugik import GugikClient
    from .utils.http import HttpClient

    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    if listing.lat is None or listing.lon is None:
        return {"listing_id": listing_id, "dzialka": None,
                "info": "oferta nie ma współrzędnych"}
    if listing.geo_precision not in ("address", "street"):
        return {"listing_id": listing_id, "dzialka": None,
                "info": "położenie jest zbyt przybliżone, żeby wskazać działkę"}

    async with HttpClient(concurrency=2) as http:
        parcel = await GugikClient(http).parcel_by_point(listing.lat, listing.lon)
    if parcel is None:
        return {"listing_id": listing_id, "dzialka": None, "info": "nie znaleziono działki"}
    return {
        "listing_id": listing_id,
        "dzialka": {
            "identyfikator": parcel.identifier,
            "teryt": parcel.teryt,
            "obreb": parcel.region,
            "geometria_wkt": parcel.geometry_wkt,
        },
    }


@app.get("/api/stats")
def api_stats(db: DB) -> dict:
    return dashboard_stats(db)


@app.get("/api/sources")
def api_sources(db: DB) -> dict:
    sources = list(db.scalars(select(Source).order_by(Source.category, Source.name)))
    return {
        "count": len(sources),
        "items": [
            {
                "key": s.key, "name": s.name, "kind": s.kind.value, "category": s.category,
                "base_url": s.base_url, "enabled": s.enabled, "coverage": s.coverage,
                "interval_minutes": s.interval_minutes, "total_listings": s.total_listings,
                "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                "last_ok_at": s.last_ok_at.isoformat() if s.last_ok_at else None,
                "last_error": s.last_error, "notes": s.notes,
            }
            for s in sources
        ],
    }


@app.get("/api/agencies")
def api_agencies(db: DB, min_offers: int = 0, q: str | None = None) -> dict:
    stmt = select(Agency).where(Agency.listings_count >= min_offers)
    if q:
        stmt = stmt.where(Agency.name.ilike(f"%{q}%"))
    agencies = list(db.scalars(stmt.order_by(desc(Agency.listings_count), Agency.name).limit(1000)))
    return {
        "count": len(agencies),
        "items": [
            {
                "id": a.id, "slug": a.slug, "name": a.name, "city": a.city,
                "website": a.website, "phones": a.phones, "listings": a.listings_count,
                "verified": a.verified, "discovered": a.discovered,
                "first_seen_at": a.first_seen_at.isoformat(),
            }
            for a in agencies
        ],
    }


@app.post("/api/favorites/{listing_id}")
def api_add_favorite(listing_id: int, db: DB, folder: str = "domyslny") -> JSONResponse:
    if not db.get(Listing, listing_id):
        raise HTTPException(404, "Nie ma takiej oferty")
    exists = db.scalar(
        select(Favorite).where(Favorite.listing_id == listing_id, Favorite.folder == folder)
    )
    if not exists:
        db.add(Favorite(listing_id=listing_id, folder=folder))
        db.commit()
    return JSONResponse({"ok": True, "listing_id": listing_id, "folder": folder})


@app.delete("/api/favorites/{listing_id}")
def api_remove_favorite(listing_id: int, db: DB, folder: str = "domyslny") -> JSONResponse:
    favorite = db.scalar(
        select(Favorite).where(Favorite.listing_id == listing_id, Favorite.folder == folder)
    )
    if favorite:
        db.delete(favorite)
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/searches")
def api_create_search(payload: dict, db: DB) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Poszukiwanie musi mieć nazwę")
    search = SavedSearch(
        name=name[:200],
        query=payload.get("query") or {},
        channels=payload.get("channels") or ["telegram"],
        only_original=bool(payload.get("only_original", True)),
    )
    db.add(search)
    db.commit()
    return {"id": search.id, "name": search.name}


@app.delete("/api/searches/{search_id}")
def api_delete_search(search_id: int, db: DB) -> dict:
    search = db.get(SavedSearch, search_id)
    if search:
        db.delete(search)
        db.commit()
    return {"ok": True}


@app.get("/api/runs")
def api_runs(db: DB, limit: int = 50) -> dict:
    runs = list(db.scalars(select(ScanRun).order_by(desc(ScanRun.started_at)).limit(limit)))
    return {
        "items": [
            {
                "source": r.source_key,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "fetched": r.fetched, "new": r.new, "updated": r.updated,
                "duplicates": r.duplicates, "errors": r.errors, "ok": r.ok, "message": r.message,
            }
            for r in runs
        ]
    }


@app.get("/api/market-report")
def api_market_report(db: DB, city: str | None = None, days: int = 90) -> dict:
    """Prosty raport rynkowy: mediana ceny za m², rotacja ofert, udział biur."""
    since = utcnow() - timedelta(days=days)
    stmt = select(Listing).where(Listing.is_original.is_(True), Listing.first_seen_at >= since)
    if city:
        stmt = stmt.where(Listing.city.ilike(f"%{city}%"))
    rows = list(db.scalars(stmt))
    prices = sorted(x.price_per_m2 for x in rows if x.price_per_m2)
    sold = [x for x in rows if x.removed_at]
    return {
        "city": city,
        "days": days,
        "listings": len(rows),
        "median_price_m2": prices[len(prices) // 2] if prices else None,
        "min_price_m2": prices[0] if prices else None,
        "max_price_m2": prices[-1] if prices else None,
        "avg_days_on_market": round(sum(x.days_on_market for x in sold) / len(sold), 1)
        if sold else None,
        "removed": len(sold),
        "agency_share": round(
            100 * sum(1 for x in rows if x.seller_type.value in ("posrednik", "deweloper"))
            / max(len(rows), 1), 1
        ),
    }

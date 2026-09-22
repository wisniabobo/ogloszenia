"""FastAPI: interfejs webowy + REST.

Ten sam builder filtrów obsługuje widok HTML i endpointy JSON, więc lista
w przeglądarce i wynik `/api/listings` zawsze się zgadzają.
"""

from __future__ import annotations

import math
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qsl, quote_plus, urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.responses import Response as FastResponse
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
    ListingStatus,
    MarketStat,
    OfferKind,
    SavedSearch,
    ScanRun,
    Source,
    TransactionType,
    utcnow,
)
from .pipeline.market import market_levels
from .query import (
    SORT_LABELS,
    Filters,
    apply_filters,
    apply_sort,
    dashboard_stats,
    map_points,
    phone_lookup,
    place_clause,
    search_listings,
    similar_listings,
    street_terms,
)
from .settings import get_settings
from .utils.text import to_polish_time

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
templates.env.filters["pl"] = to_polish_time
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


def popular_places(limit: int = 16) -> list[str]:
    """Miasta z największą liczbą ofert — do linków w stopce.

    Linki w stopce są dla robota jedyną drogą do stron miast: same filtry
    w formularzu wysyłają POST-a, którego wyszukiwarka nie kliknie.
    """
    with session_scope() as session:
        return suggest_cities(session)[:limit]


templates.env.globals["popular_places"] = popular_places
templates.env.globals["active_filters"] = active_filters
templates.env.globals["hidden_fields"] = hidden_fields

app = FastAPI(
    title="Metruj — API ofert nieruchomości",
    version=__version__,
    description=(
        "Oferty nieruchomości, licytacje komornicze i skarbowe oraz przetargi "
        "z całej Polski."
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


#: Ile sekund wolno podawać zapamiętaną odpowiedź na pytania, których
#: odpowiedź zmienia się raz na przebieg zbierania.
CACHE_TTL = 180

_cache: dict[str, tuple[float, Any]] = {}
_refreshing: set[str] = set()


def cached(key: str, build, db: Session, ttl: int = CACHE_TTL):
    """Wynik zapamiętany na krótko — dla zapytań zbiorczych po całej bazie.

    Lista podpowiadanych miejscowości i liczby na pulpicie wymagają przejścia
    po wszystkich ofertach — przy 180 tys. ofert to kilka sekund. Zmieniają się
    raz na przebieg zbierania, więc wystarczy je liczyć co kilka minut.

    Po wygaśnięciu oddajemy od razu ostatni wynik, a nowy liczymy w tle
    (z własną sesją bazy). Wcześniej co trzy minuty pierwszy odwiedzający
    „Rynku" czekał ponad cztery sekundy na przeliczenie.
    """
    import threading
    import time

    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    if hit:
        if key not in _refreshing:
            _refreshing.add(key)
            threading.Thread(target=_refresh, args=(key, build), daemon=True).start()
        return hit[1]
    value = build(db)
    _cache[key] = (now, value)
    return value


def _refresh(key: str, build) -> None:
    import logging
    import time

    try:
        with session_scope() as session:
            _cache[key] = (time.monotonic(), build(session))
    except Exception as exc:  # pamięć podręczna nie może wywrócić strony
        logging.getLogger("metruj.web").warning("Nie przeliczono %s: %s", key, exc)
    finally:
        _refreshing.discard(key)


def suggest_cities(db: Session, limit: int = 400) -> list[str]:
    """Miejscowości do podpowiedzi w wyszukiwarce.

    Najpierw te, które faktycznie mają oferty — posortowane od najliczniejszych,
    bo o nie ludzie pytają najczęściej. Przy pustej bazie (świeża instalacja)
    zostaje krajowa lista miast z rejestru TERYT, żeby pole podpowiedzi nie
    świeciło pustką.
    """

    def build(db: Session) -> list[str]:
        rows = db.execute(
            select(Listing.city, func.count(Listing.id).label("n"))
            .where(Listing.city.is_not(None))
            .group_by(Listing.city)
            .order_by(desc("n"))
            .limit(limit)
        ).all()
        found = [row[0] for row in rows if row[0]]
        return found or town_names()[:limit]

    return cached(f"cities:{limit}", build, db)


#: Filtry schowane w rozwijanej sekcji. Sekcja otwiera się, gdy użytkownik
#: ustawił którykolwiek z nich — ale tylko wtedy, gdy zrobił to sam. Wartości
#: domyślne strony (np. próg okazji na „/okazje") nie mogą rozwijać całego
#: formularza i spychać wyników pod zgięcie.
ADVANCED_FILTERS = (
    "county", "district", "street", "price_m2_min", "price_m2_max",
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


# --------------------------------------------------------------------------- #
# Zapis: schowek i alerty
# --------------------------------------------------------------------------- #
#: Nazwa ciasteczka z hasłem zapisu — ustawiane przez formularz na /wejscie.
ADMIN_COOKIE = "metruj_token"

#: Adresy uznawane za „swoje", gdy hasło zapisu nie jest ustawione: localhost
#: i sieci prywatne. Uruchomienie na własnym komputerze ma działać od razu,
#: bez konfiguracji — publiczna instancja bez hasła jest tylko do czytania.
def _is_local(request: Request) -> bool:
    import ipaddress

    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def require_writer(request: Request) -> None:
    """Wpuszcza do zapisu właściciela instancji, a nie każdego przechodnia."""
    import secrets

    token = get_settings().admin_token
    if not token:
        if _is_local(request):
            return
        raise HTTPException(
            403,
            "Zapis jest wyłączony: ta instancja nie ma ustawionego "
            "OGL_ADMIN_TOKEN (schowek i alerty działają tylko lokalnie).",
        )
    given = request.headers.get("X-Admin-Token") or request.cookies.get(ADMIN_COOKIE) or ""
    if not secrets.compare_digest(given, token):
        raise HTTPException(401, "Potrzebne hasło zapisu — zaloguj się na /wejscie")


Writer = Annotated[None, Depends(require_writer)]

#: Prosty licznik odsłon numerów: {adres IP: [znaczniki czasu]}. Trzymany
#: w pamięci procesu — przy jednym procesie web to wystarcza, a nie wymaga
#: Redisa. Limit chroni bazę numerów przed pobraniem oferta po ofercie.
_reveals: dict[str, list[float]] = {}


def rate_limit_phone(request: Request) -> None:
    import time

    limit = get_settings().phone_reveal_limit
    if limit <= 0:
        return
    host = request.client.host if request.client else "?"
    now = time.monotonic()
    recent = [t for t in _reveals.get(host, []) if now - t < 3600]
    if len(recent) >= limit:
        raise HTTPException(429, "Za dużo zapytań o numery — spróbuj za godzinę")
    recent.append(now)
    _reveals[host] = recent
    if len(_reveals) > 10000:  # nie rośnie bez końca
        for key in [k for k, v in _reveals.items() if not v or now - v[-1] > 3600]:
            _reveals.pop(key, None)


PhoneLimit = Annotated[None, Depends(rate_limit_phone)]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()

    # Rozgrzanie pamięci podręcznej w tle: pierwszy odwiedzający po
    # restarcie nie czeka na przeliczenie statystyk całej bazy.
    import threading

    def warm() -> None:
        with session_scope() as session:
            cached("stats", dashboard_stats, session)
            suggest_cities(session)

    threading.Thread(target=warm, daemon=True).start()
    yield


app.router.lifespan_context = lifespan


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
    def _many(name: str) -> list[str]:
        """Wielokrotny wybór z formularza, bez pustych pozycji.

        Lista rozwijana, w której nie wybrano nic, wysyła `source=` — pustą
        wartość. Wpadała ona do filtru jako „pokaż oferty z serwisu o pustej
        nazwie", czyli **zero wyników**. Ponieważ sekcja z tym polem jest
        w formularzu zawsze (tylko zwinięta), zerowało to wyniki przy każdym
        naciśnięciu „Szukaj" i przy każdym przełączniku — a wyglądało jak
        zepsute sortowanie i zepsute przyciski.
        """
        return [value for value in params.getlist(name) if value]

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
        source=_many("source"),
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
        deal_level=_many("deal_level"),
        period=params.get("period") or None,
        days_on_market_min=num("days_on_market_min", int),
        days_on_market_max=num("days_on_market_max", int),
        q=params.get("q") or None,
        sort=params.get("sort") or "najnowsze",
        # `per_page=-1` dawało w SQLite `LIMIT -1`, czyli całą bazę naraz,
        # a `page=abc` kończyło się błędem 500.
        page=max(num("page", int) or 1, 1),
        per_page=min(max(num("per_page", int) or 25, 1), 100),
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
        # data wystawienia na rynku (z portalu, a w jej braku — nasze pierwsze
        # spotkanie); po niej sortuje „od najnowszych"
        "listed_at": listing.market_since.isoformat(),
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
    """Strona główna to wyszukiwarka — ta sama co „Oferty".

    Wcześniej była tu uproszczona wyszukiwarka z czterema polami, blok
    tekstu i kafelki ze statystykami: kto przyszedł po mieszkanie, musiał
    przejść na drugą stronę, żeby wybrać województwo czy metraż. Zestawienia
    rynkowe są na /rynek.
    """
    # Strona główna to ta sama lista, ale jej tytuł ma mówić, czym jest serwis.
    return view_listings(request, db, seo={
        "title": "Metruj — ogłoszenia nieruchomości, licytacje i przetargi z całej Polski",
        "description": (
            "Oferty z portali, licytacje komornicze i skarbowe oraz przetargi gmin "
            "w jednym miejscu. Bez powtórek, z ceną za m² porównaną z medianą okolicy."
        ),
        "canonical": site_url("/"),
        "robots": "index, follow",
    })


@app.get("/rynek", response_class=HTMLResponse)
def view_dashboard(request: Request, db: DB):
    stats = cached("stats", dashboard_stats, db)
    newest = list(
        db.scalars(
            apply_sort(apply_filters(select(Listing), {"only_original": True}), "najnowsze").limit(6)
        )
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"stats": stats, "newest": newest, "active": "rynek",
         **static_seo(
             request,
             "Rynek nieruchomości w Polsce — mediany cen i liczba ofert | Metruj",
             "Mediany cen za m², liczba nowych ofert i obniżek, miasta z największą "
             "liczbą ogłoszeń. Liczone z ofert zebranych z kilkudziesięciu źródeł.",
         )},
    )


#: Parametry miejsca, które mają jeden poprawny zapis — z rejestru TERYT.
PLACE_PARAMS = ("city", "county", "voivodeship")


def canonical_place_redirect(request: Request, db: Session) -> RedirectResponse | None:
    """„?city=wroclaw" → „?city=Wrocław", jednym przekierowaniem 301.

    Ten sam wynik pod kilkoma adresami to dla wyszukiwarki kilka konkurujących
    stron, a dla człowieka tytuł „Mieszkania — wroclaw". Wyszukiwanie nadal
    przyjmuje dowolny zapis; adres po nim się porządkuje.
    """
    from .geo import lookup

    params = list(request.query_params.multi_items())
    changed = False
    fixed: list[tuple[str, str]] = []
    for key, value in params:
        new_value = value
        if key in PLACE_PARAMS and value.strip():
            if hits := lookup(value):
                new_value = hits[0].name
        elif key == "street" and value.strip():
            # „milosza" zamieniamy na nazwę, która naprawdę stoi w ofertach
            # („Czesława Miłosza") — razem z ogonkami i imieniem patrona.
            city = request.query_params.get("city")
            new_value = _street_label(value) or value
            # Poprawiamy wyłącznie pisownię tych samych słów: „milosza" →
            # „Miłosza". Nazwy nie zamieniamy na inną („Miłosza" →
            # „Miłoszycka") ani na dłuższą („Jana" → „Jana Pawła II") — to
            # byłoby zawężenie wyszukiwania za plecami szukającego.
            wanted = street_terms(value)
            for match in suggest_streets(db, city, value, 10):
                if street_terms(match["name"]) == wanted:
                    new_value = match["name"]
                    break
                # Jedno słowo bez ogonków („milosza") dopisujemy z ogonkami
                # („Miłosza") — słowo z ofert, a nie cała ich nazwa ulicy.
                if len(wanted) == 1:
                    same = [
                        word for word in match["name"].split()
                        if street_terms(word) == wanted
                    ]
                    if same:
                        new_value = street_display(same[0])
                        break
        if new_value != value:
            changed = True
        fixed.append((key, new_value))
    if not changed:
        return None
    return RedirectResponse(
        f"{request.url.path}?{urlencode(fixed)}", status_code=301
    )


@app.get("/nieruchomosci", response_class=HTMLResponse)
def view_listings(request: Request, db: DB, seo: dict[str, Any] | None = None):
    if (redirect := canonical_place_redirect(request, db)) is not None:
        return redirect
    filters = _filters_from_query(request)
    _default_to_sale(request, filters)
    listings, total = search_listings(db, filters)
    pages = max(1, math.ceil(total / filters.per_page))
    context = listings_seo(request, filters, total, pages=pages)
    if seo:
        context["seo"] = seo
    return templates.TemplateResponse(
        request,
        "listings.html",
        {
            "listings": listings,
            "total": total,
            "filters": filters,
            "pages": pages,
            "active": "nieruchomosci",
            **context,
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
    if (redirect := canonical_place_redirect(request, db)) is not None:
        return redirect
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
            **listings_seo(
                request, filters, total, heading="Okazje",
                pages=max(1, math.ceil(total / filters.per_page)),
            ),
            "reset_url": "/okazje",
            "form_action": "/okazje",
            **filter_context(db, request),
        },
    )


@app.get("/licytacje", response_class=HTMLResponse)
def view_auctions(request: Request, db: DB):
    if (redirect := canonical_place_redirect(request, db)) is not None:
        return redirect
    filters = _filters_from_query(request)
    filters.kind = filters.kind or OfferKind.LICYTACJA.value
    filters.sort = request.query_params.get("sort") or "termin_licytacji"
    listings, total = search_listings(db, filters)
    return templates.TemplateResponse(
        request,
        "auctions.html",
        {"listings": listings, "total": total, "filters": filters,
         "pages": max(1, math.ceil(total / filters.per_page)), "active": "licytacje",
         **listings_seo(
             request, filters, total, heading="Licytacje komornicze i skarbowe",
             pages=max(1, math.ceil(total / filters.per_page)),
         ),
         "query_string": str(request.query_params)},
    )


@app.get("/przetargi", response_class=HTMLResponse)
def view_tenders(request: Request, db: DB):
    if (redirect := canonical_place_redirect(request, db)) is not None:
        return redirect
    filters = _filters_from_query(request)
    filters.kind = filters.kind or OfferKind.PRZETARG.value
    listings, total = search_listings(db, filters)
    return templates.TemplateResponse(
        request,
        "auctions.html",
        {"listings": listings, "total": total, "filters": filters,
         "pages": max(1, math.ceil(total / filters.per_page)), "active": "przetargi",
         **listings_seo(
             request, filters, total, heading="Przetargi i wykazy nieruchomości",
             pages=max(1, math.ceil(total / filters.per_page)),
         ),
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
    similar = similar_listings(db, listing)
    return templates.TemplateResponse(
        request,
        "listing.html",
        {"listing": listing, "copies": copies, "original": original, "agency": agency,
         "similar": similar, "similar_url": "/nieruchomosci?" + urlencode(similar.more_query),
         "market": market_levels(db, listing), "active": "nieruchomosci",
         **listing_seo(request, listing)},
    )


@app.get("/mapa", response_class=HTMLResponse)
def view_map(request: Request, db: DB):
    if (redirect := canonical_place_redirect(request, db)) is not None:
        return redirect
    filters = _filters_from_query(request)
    _default_to_sale(request, filters)
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "filters": filters, "active": "mapa",
            "seo": {
                "title": "Mapa ofert nieruchomości w Polsce | Metruj",
                "description": "Wszystkie zebrane oferty na mapie — pinezki w kolorze "
                               "ceny za m², te same filtry co na liście.",
                "canonical": site_url("/mapa"),
                "robots": "index, follow",
            },
            **filter_context(db, request),
        },
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
            **static_seo(
                request,
                "Biura nieruchomości i deweloperzy w Polsce — lista z telefonami | Metruj",
                "Rejestr pośredników i deweloperów: miasto, telefon i liczba ofert. "
                "Sprawdź, czy oferta prywatna nie jest kolejnym ogłoszeniem biura.",
                robots="index, follow" if not request.query_params else "noindex, follow",
            ),
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
        {"sources": sources, "runs": runs, "recent": recent, "active": "zrodla",
         **static_seo(
             request,
             "Skąd pochodzą dane — źródła ogłoszeń | Metruj",
             "Portale, licytacje komornicze i skarbowe, BIP gmin i instytucje "
             "publiczne: pełna lista źródeł wraz ze stanem ostatniego pobrania.",
         )},
    )


@app.get("/poszukiwania", response_class=HTMLResponse)
def view_searches(request: Request, db: DB):
    searches = list(db.scalars(select(SavedSearch).order_by(desc(SavedSearch.created_at))))
    return templates.TemplateResponse(
        request, "searches.html",
        {"searches": searches, "active": "poszukiwania",
         **static_seo(request, "Alerty o nowych ofertach | Metruj",
                      "Zapisane poszukiwania i powiadomienia o nowych ofertach.",
                      robots="noindex, nofollow")}
    )


@app.get("/schowek", response_class=HTMLResponse)
def view_favorites(request: Request, db: DB):
    rows = db.execute(
        select(Favorite, Listing).join(Listing, Listing.id == Favorite.listing_id)
        .order_by(desc(Favorite.created_at))
    ).all()
    return templates.TemplateResponse(
        request, "favorites.html",
        {"rows": rows, "active": "schowek",
         **static_seo(request, "Schowek | Metruj", "Zapisane oferty.",
                      robots="noindex, nofollow")}
    )


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
@app.get("/wejscie", response_class=HTMLResponse)
def view_login(request: Request):
    """Formularz hasła zapisu — jedna instancja, jeden właściciel."""
    return templates.TemplateResponse(
        request, "login.html",
        {"active": "",
         **static_seo(request, "Hasło zapisu | Metruj", "Panel właściciela instancji.",
                      robots="noindex, nofollow")},
    )


@app.post("/wejscie", response_class=HTMLResponse)
async def do_login(request: Request):
    import secrets

    # Formularz czytamy sami z ciała zapytania: `request.form()` wymaga
    # biblioteki python-multipart, a jedno pole nie jest tego warte.
    body = (await request.body()).decode("utf-8", "replace")
    token = get_settings().admin_token
    given = dict(parse_qsl(body)).get("token", "")
    if not token:
        return templates.TemplateResponse(
            request, "login.html",
            {"active": "", "error": "Ta instancja nie ma ustawionego OGL_ADMIN_TOKEN."},
            status_code=503,
        )
    if not secrets.compare_digest(given, token):
        return templates.TemplateResponse(
            request, "login.html", {"active": "", "error": "Błędne hasło."},
            status_code=401,
        )
    response = RedirectResponse("/poszukiwania", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=60 * 60 * 24 * 90, httponly=True,
        samesite="lax", secure=request.url.scheme == "https",
    )
    return response


@app.post("/wyjscie")
def do_logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response


# --------------------------------------------------------------------------- #
# SEO: tytuły, opisy, linki kanoniczne, dane strukturalne
# --------------------------------------------------------------------------- #
#: Parametry, które tworzą stronę wartą zaindeksowania („mieszkania na sprzedaż
#: we Wrocławiu"). Wszystko poza nimi — sortowania, widełki cen, kombinacje
#: dziesięciu filtrów — to ta sama treść w tysiącu wariantów; dla wyszukiwarki
#: jest to duplikat, który rozmywa stronę, więc dostaje `noindex`.
INDEXABLE_PARAMS = {"city", "county", "voivodeship", "street", "property_type", "transaction", "page"}

TYPE_PLURAL = {
    "mieszkanie": "Mieszkania", "dom": "Domy", "dzialka": "Działki",
    "lokal": "Lokale użytkowe", "biuro": "Biura", "hala": "Hale i magazyny",
    "garaz": "Garaże", "gospodarstwo": "Gospodarstwa", "kamienica": "Kamienice",
    "pokoj": "Pokoje", "inne": "Nieruchomości",
}
TYPE_SINGULAR = {
    "mieszkanie": "Mieszkanie", "dom": "Dom", "dzialka": "Działka",
    "lokal": "Lokal użytkowy", "biuro": "Biuro", "hala": "Hala",
    "garaz": "Garaż", "gospodarstwo": "Gospodarstwo", "kamienica": "Kamienica",
    "pokoj": "Pokój", "inne": "Nieruchomość",
}
TRANSACTION_PHRASE = {
    "sprzedaz": "na sprzedaż", "wynajem": "do wynajęcia",
    "dzierzawa": "w dzierżawę", "zamiana": "na zamianę",
}


def site_url(path: str = "/") -> str:
    return get_settings().site_url.rstrip("/") + path


def canonical_for(request: Request, keep: set[str] | None = None) -> str:
    """Adres kanoniczny: ścieżka i tylko te parametry, które zmieniają treść.

    Bez tego każdy link z `?sort=`, `?per_page=` czy śmieciem z kampanii
    („utm_source") był dla wyszukiwarki osobną stroną z tą samą listą ofert.
    """
    keep = keep if keep is not None else INDEXABLE_PARAMS
    pairs = [
        (key, value)
        for key, value in sorted(request.query_params.multi_items())
        if key in keep and value and not (key == "page" and value == "1")
    ]
    query = urlencode(pairs)
    return site_url(request.url.path) + (f"?{query}" if query else "")


def _page_url(request: Request, page: int) -> str | None:
    if page < 1:
        return None
    params = [
        (key, value)
        for key, value in sorted(request.query_params.multi_items())
        if key in INDEXABLE_PARAMS and key != "page" and value
    ]
    if page > 1:
        params.append(("page", str(page)))
    query = urlencode(params)
    return site_url(request.url.path) + (f"?{query}" if query else "")


def listings_seo(
    request: Request, filters: Filters, total: int, *, heading: str | None = None,
    pages: int = 1,
) -> dict[str, Any]:
    """Tytuł, opis i nagłówek listy ułożone z tego, czego ktoś naprawdę szuka."""
    kind = (filters.property_type or "").strip()
    what = TYPE_PLURAL.get(kind, "Nieruchomości")
    deal = TRANSACTION_PHRASE.get(filters.transaction or "", "")
    where_bits = []
    if filters.street:
        where_bits.append(f"ul. {filters.street.strip()}")
    if filters.city and filters.city.strip() != (filters.street or "").strip():
        where_bits.append(filters.city.strip())
    elif filters.county:
        where_bits.append(f"powiat {filters.county.strip()}")
    elif filters.voivodeship:
        where_bits.append(f"woj. {filters.voivodeship.strip()}")
    where = ", ".join(where_bits)

    # Przy własnym nagłówku („Okazje", „Licytacje") nie doklejamy „na sprzedaż" —
    # „Okazje na sprzedaż" to nie po polsku.
    headline = heading or " ".join(x for x in (what, deal) if x)
    if where:
        headline = f"{headline} — {where}"
    title = f"{headline} | Metruj" if len(headline) <= 60 else f"{headline[:60]}… | Metruj"
    counted = f"{total:,}".replace(",", " ")
    description = (
        f"{counted} "
        + ("ogłoszeń" if total != 1 else "ogłoszenie")
        + f" — {headline.lower()}. Powtórki z różnych portali sklejone w jeden wpis, "
        "cena za m² porównana z medianą okolicy, historia obniżek i kontakt."
    )

    extra = {
        key for key, value in request.query_params.multi_items()
        if value and key not in INDEXABLE_PARAMS
    }
    indexable = not extra and total > 0 and filters.page <= 20
    page = max(filters.page, 1)
    return {
        "seo": {
            "title": title,
            "description": description[:300],
            "robots": "index, follow" if indexable else "noindex, follow",
            "canonical": canonical_for(request),
            "prev": _page_url(request, page - 1) if page > 1 else None,
            "next": _page_url(request, page + 1) if page < pages else None,
        },
        "seo_headline": headline,
    }


def listing_seo(request: Request, listing: Listing, market: Any = None) -> dict[str, Any]:
    """Dane strukturalne oferty — po nich wyniki Google pokazują cenę i metraż."""
    import json

    what = TYPE_SINGULAR.get(
        listing.property_type.value if listing.property_type else "", "Nieruchomość"
    )
    deal = TRANSACTION_PHRASE.get(
        listing.transaction.value if listing.transaction else "", ""
    )
    # Ulica bywa zapisana tą samą nazwą co miasto („Nysa, Nysa") — raz wystarczy.
    where_parts = [x for x in (listing.street, listing.city) if x]
    if len(where_parts) == 2 and _street_label(where_parts[0]) == where_parts[1]:
        where_parts = where_parts[1:]
    where = ", ".join(where_parts)
    bits = [x for x in (
        f"{listing.area:g} m²" if listing.area else "",
        f"{listing.rooms} pok." if listing.rooms else "",
        f"{listing.price:,.0f} zł".replace(",", " ") if listing.price else "",
    ) if x]
    title = " ".join(x for x in (what, deal) if x)
    if where:
        title += f", {where}"
    if bits:
        title += " — " + " · ".join(bits)

    description = (listing.description or listing.title or "").strip().replace("\n", " ")
    description = re.sub(r"\s+", " ", description)[:280]

    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "RealEstateListing",
        "name": listing.title[:150],
        "url": site_url(f"/oferta/{listing.id}"),
        "datePosted": listing.listed_at.isoformat() if listing.listed_at else None,
        "description": description or None,
    }
    address = {
        "@type": "PostalAddress",
        "addressCountry": "PL",
        "addressLocality": listing.city,
        "addressRegion": listing.voivodeship,
        "streetAddress": listing.street,
    }
    data["address"] = {k: v for k, v in address.items() if v}
    if listing.lat is not None and listing.lon is not None:
        data["geo"] = {"@type": "GeoCoordinates", "latitude": listing.lat, "longitude": listing.lon}
    if listing.price:
        data["offers"] = {
            "@type": "Offer",
            "price": round(listing.price, 2),
            "priceCurrency": "PLN",
            "availability": "https://schema.org/InStock"
            if listing.status == ListingStatus.AKTYWNA
            else "https://schema.org/SoldOut",
        }
    if listing.area or listing.rooms:
        accommodation: dict[str, Any] = {"@type": "Accommodation"}
        if listing.area:
            accommodation["floorSize"] = {
                "@type": "QuantitativeValue", "value": listing.area, "unitCode": "MTK",
            }
        if listing.rooms:
            accommodation["numberOfRoomsTotal"] = listing.rooms
        data["about"] = accommodation
    if listing.images:
        data["image"] = listing.images[:5]

    crumbs = [("Oferty", "/nieruchomosci")]
    if listing.voivodeship:
        crumbs.append((listing.voivodeship,
                       f"/nieruchomosci?voivodeship={quote_plus(listing.voivodeship)}"))
    if listing.city:
        crumbs.append((listing.city, f"/nieruchomosci?city={quote_plus(listing.city)}"))
    crumbs.append((title[:80], f"/oferta/{listing.id}"))
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": site_url(path)}
            for i, (name, path) in enumerate(crumbs)
        ],
    }

    return {
        "seo": {
            "title": f"{title[:90]} | Metruj",
            "description": description or f"{title}. Oferta w serwisie Metruj.",
            "canonical": site_url(f"/oferta/{listing.id}"),
            "robots": "index, follow" if listing.status == ListingStatus.AKTYWNA
            else "noindex, follow",
            "image": listing.images[0] if listing.images else None,
            "jsonld": json.dumps(
                [{k: v for k, v in data.items() if v is not None}, breadcrumbs],
                ensure_ascii=False,
            ),
        }
    }


def static_seo(
    request: Request, title: str, description: str, *, robots: str = "index, follow"
) -> dict[str, Any]:
    """SEO strony, której treść nie zależy od filtrów."""
    return {"seo": {
        "title": title, "description": description, "robots": robots,
        "canonical": site_url(request.url.path),
    }}


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots_txt() -> str:
    """Co wolno robotom. Adresy zapisu i zapytania API nie mają czego szukać."""
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /schowek",
        "Disallow: /poszukiwania",
        "Disallow: /wejscie",
        "Crawl-delay: 2",
        "",
        f"Sitemap: {site_url('/sitemap.xml')}",
        "",
    ])


#: Ile adresów w jednym pliku sitemapy. Limit protokołu to 50 000.
SITEMAP_CHUNK = 20000


def _xml(entries: list[tuple[str, str | None, str]]) -> FastResponse:
    from xml.sax.saxutils import escape as xml_escape

    rows = []
    for loc, lastmod, freq in entries:
        row = [f"<loc>{xml_escape(loc)}</loc>"]
        if lastmod:
            row.append(f"<lastmod>{lastmod}</lastmod>")
        row.append(f"<changefreq>{freq}</changefreq>")
        rows.append("<url>" + "".join(row) + "</url>")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(rows)
        + "</urlset>"
    )
    return FastResponse(body, media_type="application/xml",
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_index(db: DB) -> FastResponse:
    """Spis sitemap — osobno strony przeglądania, osobno same oferty."""
    from xml.sax.saxutils import escape as xml_escape

    count = cached("sitemap:count", lambda db: int(db.scalar(
        select(func.count(Listing.id)).where(
            Listing.is_original.is_(True), Listing.status == ListingStatus.AKTYWNA
        )
    ) or 0), db, ttl=3600)
    parts = [site_url("/sitemap-strony.xml")]
    for index in range(math.ceil(max(count, 1) / SITEMAP_CHUNK)):
        parts.append(site_url(f"/sitemap-oferty-{index + 1}.xml"))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<sitemap><loc>{xml_escape(url)}</loc></sitemap>" for url in parts)
        + "</sitemapindex>"
    )
    return FastResponse(body, media_type="application/xml",
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sitemap-strony.xml", include_in_schema=False)
def sitemap_pages(db: DB) -> FastResponse:
    """Strony przeglądania: sekcje serwisu, miasta, powiaty i ulice z ofertami."""

    def build(db: Session) -> list[tuple[str, str | None, str]]:
        entries: list[tuple[str, str | None, str]] = [
            (site_url("/"), None, "hourly"),
            (site_url("/nieruchomosci"), None, "hourly"),
            (site_url("/okazje"), None, "hourly"),
            (site_url("/mapa"), None, "daily"),
            (site_url("/licytacje"), None, "daily"),
            (site_url("/przetargi"), None, "daily"),
            (site_url("/biura"), None, "weekly"),
            (site_url("/rynek"), None, "daily"),
            (site_url("/zrodla"), None, "weekly"),
        ]
        active = (Listing.is_original.is_(True), Listing.status == ListingStatus.AKTYWNA)
        cities = db.execute(
            select(Listing.city, func.count(Listing.id).label("n"))
            .where(Listing.city.is_not(None), *active)
            .group_by(Listing.city).having(func.count(Listing.id) >= 3)
            .order_by(desc("n")).limit(3000)
        ).all()
        for city, _ in cities:
            base = f"/nieruchomosci?city={quote_plus(city)}"
            entries.append((site_url(base), None, "daily"))
            for ptype in ("mieszkanie", "dom", "dzialka"):
                entries.append((site_url(f"{base}&property_type={ptype}"), None, "daily"))
        counties = db.execute(
            select(Listing.county, func.count(Listing.id).label("n"))
            .where(Listing.county.is_not(None), *active)
            .group_by(Listing.county).having(func.count(Listing.id) >= 3)
            .order_by(desc("n")).limit(400)
        ).all()
        for county, _ in counties:
            entries.append((site_url(f"/nieruchomosci?county={quote_plus(county)}"), None, "daily"))
        # Ulice tylko tam, gdzie jest co pokazać — strona z jedną ofertą nie ma
        # po co stać w sitemapie. Wszystkie miasta liczymy jednym zapytaniem:
        # osobne pytanie o każde z nich to kilkaset przejść po całej tabeli.
        big_cities = {city for city, _ in cities[:300]}
        streets: dict[tuple[str, str], int] = {}
        rows = db.execute(
            select(Listing.city, Listing.street, func.count(Listing.id))
            .where(Listing.city.in_(big_cities), Listing.street.is_not(None), *active)
            .group_by(Listing.city, Listing.street)
        ).all()
        for city, street, n in rows:
            name = _street_label(street)
            if name:
                streets[(city, name)] = streets.get((city, name), 0) + n
        for (city, street), n in streets.items():
            if n < 3:
                continue
            entries.append((
                site_url(
                    f"/nieruchomosci?city={quote_plus(city)}&street={quote_plus(street)}"
                ), None, "weekly",
            ))
        return entries

    return _xml(cached("sitemap:pages", build, db, ttl=3600))


@app.get("/sitemap-oferty-{part}.xml", include_in_schema=False)
def sitemap_listings(part: int, db: DB) -> FastResponse:
    """Adresy samych ofert, porcjami po 20 tysięcy."""
    if part < 1:
        raise HTTPException(404, "Nie ma takiej części sitemapy")
    rows = db.execute(
        select(Listing.id, Listing.last_seen_at)
        .where(Listing.is_original.is_(True), Listing.status == ListingStatus.AKTYWNA)
        .order_by(Listing.id)
        .offset((part - 1) * SITEMAP_CHUNK)
        .limit(SITEMAP_CHUNK)
    ).all()
    if not rows:
        raise HTTPException(404, "Nie ma takiej części sitemapy")
    return _xml([
        (site_url(f"/oferta/{listing_id}"),
         updated.date().isoformat() if updated else None, "weekly")
        for listing_id, updated in rows
    ])


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


@app.get("/api/listings/{listing_id}/podobne")
def api_similar(listing_id: int, db: DB, limit: int = Query(8, ge=1, le=24)) -> dict:
    """Oferty podobne do wskazanej: ten sam rodzaj i transakcja, cena i metraż
    w paśmie wokół niej, najpierw w tej samej miejscowości."""
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    similar = similar_listings(db, listing, limit=limit)
    return {
        "place": similar.place,
        "widened_to": similar.widened_to,
        "more": "/nieruchomosci?" + urlencode(similar.more_query),
        "items": [listing_to_dict(item) for item in similar.listings],
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
def api_listing_contacts(listing_id: int, db: DB, _: PhoneLimit) -> dict:
    """Numery kontaktowe oferty wraz z pochodzeniem (ogłoszenie / centrala biura)."""
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Nie ma takiej oferty")
    return {
        "listing_id": listing.id,
        "kontakty": [c.as_dict() for c in contacts_for(listing)],
    }


@app.get("/api/listings/{listing_id}/phone")
def api_listing_phone(listing_id: int, db: DB, _: PhoneLimit) -> dict:
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
def api_phone_lookup(db: DB, _: PhoneLimit, number: str = Query(min_length=4)) -> dict:
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
def api_geojson(
    request: Request, db: DB, limit: int = Query(5000, ge=1, le=20000)
) -> JSONResponse:
    """Punkty na mapę w formacie GeoJSON.

    Zwraca tylko to, czego mapa naprawdę potrzebuje (bez opisów i zdjęć), więc
    nawet kilka tysięcy ofert to kilkaset kilobajtów. Odpowiedź jest oznaczona
    do cache'owania na minutę — przesuwanie mapy nie odpytuje bazy od nowa.
    """
    filters = _filters_from_query(request)
    filters.per_page = limit
    features = map_points(db, filters)
    payload = {
        "type": "FeatureCollection",
        "count": len(features),
        "features": features,
    }
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})


@app.get("/api/listings/{listing_id}/okolica")
async def api_surroundings(
    listing_id: int, db: DB, radius: int = Query(1000, ge=100, le=3000)
) -> dict:
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


@app.get("/api/ulice")
def api_streets(
    db: DB,
    city: str | None = None,
    q: str | None = None,
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    """Podpowiedzi ulic — te, przy których faktycznie są oferty.

    W bazie ulica bywa zapisana z numerem, przedrostkiem albo dzielnicą po
    przecinku („Czesława Miłosza, Kleczków"); podpowiadamy samą nazwę,
    zsumowaną po wszystkich wariantach zapisu.
    """
    return {"city": city, "items": suggest_streets(db, city, q, limit)}


#: „okolice ul. Emila Zoli", „w pobliżu ulicy…" — to nie część nazwy.
_NEAR = re.compile(r"^\s*(?:w\s+)?(?:okolic[aey]?|pobliżu|rejon(?:ie)?)\s+", re.I)
#: „Ozimskiej", „Kolejowej" → „Ozimska", „Kolejowa" (tylko ostatnie słowo).
_LOCATIVE = re.compile(r"(?:(sk|ck|dzk)iej|(ow|n)ej)$", re.I)


def street_display(name: str) -> str:
    """Nazwa ulicy w zapisie do pokazania: „jana pawła ii" → „Jana Pawła II".

    Do bazy trafiają nazwy tak, jak je napisał autor ogłoszenia — od „ALEJA"
    po „jana pawła ii". W podpowiedziach i tytułach ma być jeden zapis.
    """
    words = []
    for word in name.split():
        if word.lower() in ("ii", "iii", "iv", "vi", "vii"):
            words.append(word.upper())
        elif word.lower() in ("i", "de", "von", "na", "pod", "im"):
            words.append(word.lower())
        elif word.isupper() and len(word) > 3:
            words.append(word.capitalize())
        elif word[:1].isalpha():
            words.append(word[0].upper() + word[1:])
        else:
            words.append(word)
    out = " ".join(words)
    return out[0].upper() + out[1:] if out else out


def _street_label(raw: str | None) -> str | None:
    """Nazwa ulicy do pokazania: bez przedrostka, numeru i dopisków.

    W bazie ta sama ulica bywa zapisana jako „Ozimska", „ul. Ozimskiej",
    „okolice ul. Ozimskiej 3" albo „Ozimska, Śródmieście" — dla człowieka
    szukającego to jedno i to samo.
    """
    from .geo.streets import split_house_number
    from .query import strip_street_prefix

    name = strip_street_prefix(_NEAR.sub("", (raw or "").split(",")[0]).strip())
    name = split_house_number(name)[0] if name else None
    if not name or len(name) < 3 or (name[0].isdigit() and " " not in name):
        return None
    name = street_display(name)
    return _LOCATIVE.sub(lambda m: (m.group(1) or m.group(2)) + "a", name)


def suggest_streets(db: Session, city: str | None, q: str | None, limit: int = 30) -> list[dict]:
    from .utils.text import deaccent

    def build(db: Session) -> list[tuple[str, str, int]]:
        stmt = (
            select(Listing.street, func.count(Listing.id))
            .where(
                Listing.street.is_not(None),
                Listing.is_original.is_(True),
                Listing.status == ListingStatus.AKTYWNA,
            )
            .group_by(Listing.street)
        )
        if city:
            stmt = stmt.where(place_clause(Listing.city, city))
        # Warianty zapisu tej samej ulicy („Al. Jana Pawła II", „aleja jana
        # pawła ii") sumujemy w jedną pozycję — dla szukającego to jedna ulica.
        totals: dict[str, tuple[str, int]] = {}
        for raw, n in db.execute(stmt):
            name = _street_label(raw)
            if not name:
                continue
            key = deaccent(name).lower()
            label, count = totals.get(key, (name, 0))
            totals[key] = (label, count + n)
        return sorted(
            ((label, key, count) for key, (label, count) in totals.items()),
            key=lambda row: (-row[2], row[0]),
        )

    rows = cached(f"streets:{(city or '').strip().lower()}", build, db)
    if q:
        wanted = [deaccent(t).lower() for t in street_terms(q)]
        rows = [row for row in rows if all(t in row[1] for t in wanted)]
    return [{"name": name, "count": n} for name, _, n in rows[:limit]]


@app.get("/api/stats")
def api_stats(db: DB) -> dict:
    return cached("stats", dashboard_stats, db)


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
def api_add_favorite(
    listing_id: int, db: DB, _: Writer, folder: str = "domyslny"
) -> JSONResponse:
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
def api_remove_favorite(
    listing_id: int, db: DB, _: Writer, folder: str = "domyslny"
) -> JSONResponse:
    favorite = db.scalar(
        select(Favorite).where(Favorite.listing_id == listing_id, Favorite.folder == folder)
    )
    if favorite:
        db.delete(favorite)
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/searches")
def api_create_search(payload: dict, db: DB, _: Writer) -> dict:
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
def api_delete_search(search_id: int, db: DB, _: Writer) -> dict:
    search = db.get(SavedSearch, search_id)
    if search:
        db.delete(search)
        db.commit()
    return {"ok": True}


@app.get("/api/runs")
def api_runs(db: DB, limit: int = Query(50, ge=1, le=500)) -> dict:
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
def api_market_report(
    db: DB, city: str | None = None, days: int = Query(90, ge=1, le=365)
) -> dict:
    """Prosty raport rynkowy: mediana ceny za m², rotacja ofert, udział biur."""
    since = utcnow() - timedelta(days=days)
    # Same potrzebne kolumny, nie całe obiekty ofert: raport dla dużego miasta
    # wciągał wcześniej do pamięci kilkadziesiąt tysięcy ogłoszeń z opisami
    # i surowym JSON-em ze źródła.
    stmt = select(
        Listing.price_per_m2, Listing.removed_at, Listing.listed_at, Listing.seller_type
    ).where(Listing.is_original.is_(True), Listing.listed_at >= since)
    if city:
        stmt = stmt.where(place_clause(Listing.city, city))
    rows = db.execute(stmt).all()
    prices = sorted(row.price_per_m2 for row in rows if row.price_per_m2)
    sold = [row for row in rows if row.removed_at and row.listed_at]
    days_on_market = [
        (row.removed_at - row.listed_at).days for row in sold
    ]
    return {
        "city": city,
        "days": days,
        "listings": len(rows),
        "median_price_m2": prices[len(prices) // 2] if prices else None,
        "min_price_m2": prices[0] if prices else None,
        "max_price_m2": prices[-1] if prices else None,
        "avg_days_on_market": round(sum(days_on_market) / len(days_on_market), 1)
        if days_on_market else None,
        "removed": len(sold),
        "agency_share": round(
            100 * sum(
                1 for row in rows
                if row.seller_type and row.seller_type.value in ("posrednik", "deweloper")
            ) / max(len(rows), 1), 1
        ),
    }

"""Przegląd serwisu: każdy widok, każdy endpoint, każdy filtr i każde sortowanie.

Uruchamiany na żywej bazie, odpytuje aplikację przez `TestClient` i sprawdza
nie tylko to, czy odpowiedź przychodzi, ale czy **ma sens**: czy sortowanie
naprawdę sortuje, czy filtr naprawdę zawęża, czy licznik zgadza się z listą.

    .venv/bin/python scripts/audit.py

Skrypt jest po to, żeby „nie działa filtrowanie i sortowanie" dało się
sprawdzić jedną komendą zamiast klikaniem po stronie.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from metruj.api import app
from metruj.query import PERIODS, SORTS

PASS, FAIL = "\033[32m ok \033[0m", "\033[31mBŁĄD\033[0m"

problems: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        problems.append(f"{name}: {detail}")
    return condition


def get(client: TestClient, path: str) -> Any:
    response = client.get(path)
    if response.status_code != 200:
        return {"__status": response.status_code, "__body": response.text[:200]}
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return {"__html": response.text}


def audit_pages(client: TestClient) -> None:
    print("\n=== Widoki HTML ===")
    for path in (
        "/", "/nieruchomosci", "/licytacje", "/przetargi", "/mapa", "/biura",
        "/zrodla", "/rynek", "/poszukiwania", "/schowek", "/okazje", "/docs",
    ):
        response = client.get(path)
        check(f"GET {path}", response.status_code == 200, f"status {response.status_code}")


def audit_api(client: TestClient) -> None:
    print("\n=== API ===")
    health = get(client, "/api/health")
    check("/api/health", health.get("status") == "ok", str(health)[:120])

    listings = get(client, "/api/listings?per_page=5")
    check("/api/listings zwraca pozycje", bool(listings.get("items")), str(listings)[:160])

    if listings.get("items"):
        listing_id = listings["items"][0]["id"]
        for path in (
            f"/api/listings/{listing_id}",
            f"/api/listings/{listing_id}/kontakt",
            f"/api/listings/{listing_id}/phone",
        ):
            data = get(client, path)
            check(f"GET {path}", "__status" not in data, str(data)[:120])

    for path in ("/api/stats", "/api/sources", "/api/agencies", "/api/runs",
                 "/api/geojson?per_page=50", "/api/market-report"):
        data = get(client, path)
        check(f"GET {path}", "__status" not in data, str(data)[:120])


def audit_sorting(client: TestClient) -> None:
    print("\n=== Sortowanie ===")
    checks = {
        "cena_rosnaco": ("price", False),
        "cena_malejaco": ("price", True),
        "cena_m2_rosnaco": ("price_per_m2", False),
        "cena_m2_malejaco": ("price_per_m2", True),
        "powierzchnia": ("area", True),
        "powierzchnia_rosnaco": ("area", False),
        # po dacie wystawienia na portalu, nie po dacie naszego zebrania
        "najnowsze": ("listed_at", True),
        "najstarsze": ("listed_at", False),
        "najdluzej_wisi": ("listed_at", False),
    }
    for sort in SORTS:
        data = get(client, f"/api/listings?sort={sort}&per_page=30")
        items = data.get("items") or []
        if not check(f"sort={sort} zwraca wyniki", bool(items), str(data)[:120]):
            continue
        if sort not in checks:
            continue
        field, descending = checks[sort]
        values = [x.get(field) for x in items]
        missing = [v for v in values if v in (None, 0)]
        check(f"sort={sort}: brak pustych na początku",
              not missing or values.index(missing[0]) == len(values) - len(missing),
              f"puste wartości wśród {values[:6]}")
        known = [v for v in values if v not in (None, 0)]
        ordered = sorted(known, reverse=descending)
        check(f"sort={sort}: kolejność", known == ordered, f"{known[:6]}")


def audit_dates(client: TestClient) -> None:
    print("\n=== Daty ===")
    from datetime import datetime, timedelta

    from metruj.models import utcnow
    from metruj.utils.text import to_polish_time

    now = utcnow()
    newest = (get(client, "/api/listings?sort=najnowsze&per_page=50") or {}).get("items") or []
    future = [x["listed_at"] for x in newest
              if datetime.fromisoformat(x["listed_at"]) > now + timedelta(hours=1)]
    check('„od najnowszych" nie zaczyna się od dat z przyszłości', not future, str(future[:3]))
    stale = [x["listed_at"] for x in newest[:10]
             if datetime.fromisoformat(x["listed_at"]) < now - timedelta(days=3)]
    check('na górze „od najnowszych" są oferty z ostatnich dni', not stale, str(stale[:3]))

    auctions = (get(client, "/api/listings?kind=licytacja&sort=termin_licytacji&per_page=50")
                or {}).get("items") or []
    terms = [(x.get("auction") or {}).get("event_date") for x in auctions]
    terms = [datetime.fromisoformat(t) for t in terms if t]
    local_now = to_polish_time(now)
    upcoming = [t for t in terms if t >= local_now]
    check('„najbliższy termin licytacji" zaczyna od licytacji przed nami',
          not terms or terms[0] >= local_now, str(terms[:3]))
    check("terminy licytacji rosnąco", upcoming == sorted(upcoming), str(upcoming[:5]))


def audit_filters(client: TestClient) -> None:
    print("\n=== Filtry ===")
    total = (get(client, "/api/listings?per_page=1") or {}).get("total", 0)
    check("baza ma oferty", total > 0, f"total={total}")

    cases: list[tuple[str, str, Any]] = [
        ("property_type=dzialka", "property_type", "dzialka"),
        ("property_type=mieszkanie", "property_type", "mieszkanie"),
        ("transaction=wynajem", "transaction", "wynajem"),
        ("seller_type=prywatna", "seller.type", "prywatna"),
        ("kind=licytacja", "kind", "licytacja"),
    ]
    for query, path, expected in cases:
        data = get(client, f"/api/listings?{query}&per_page=20")
        items = data.get("items") or []
        if not items:
            print(f"[    ] {query} — brak danych w bazie, pomijam")
            continue
        def dig(item: dict, dotted: str):
            cur: Any = item
            for part in dotted.split("."):
                cur = (cur or {}).get(part)
            return cur
        bad = [dig(x, path) for x in items if dig(x, path) != expected]
        check(f"filtr {query}", not bad, f"obce wartości: {bad[:5]}")

    ranges = [
        ("price_min=200000&price_max=300000", "price", 200000, 300000),
        ("area_min=50&area_max=70", "area", 50, 70),
        ("rooms_min=3&rooms_max=3", "rooms", 3, 3),
    ]
    for query, field, low, high in ranges:
        data = get(client, f"/api/listings?{query}&per_page=30")
        items = data.get("items") or []
        if not items:
            print(f"[    ] {query} — brak danych, pomijam")
            continue
        bad = [x.get(field) for x in items if not (low <= (x.get(field) or -1) <= high)]
        check(f"filtr {query}", not bad, f"poza zakresem: {bad[:5]}")

    # zawężanie musi zmniejszać wynik
    wide = (get(client, "/api/listings?per_page=1") or {}).get("total", 0)
    narrow = (get(client, "/api/listings?per_page=1&price_max=150000") or {}).get("total", 0)
    check("filtr ceny zawęża wynik", narrow < wide, f"{narrow} vs {wide}")

    for city in ("Warszawa", "Kraków", "Wrocław"):
        data = get(client, f"/api/listings?city={city}&per_page=10")
        items = data.get("items") or []
        if not items:
            continue
        bad = [x["location"]["city"] for x in items if city.lower() not in (x["location"]["city"] or "").lower()]
        check(f"filtr city={city}", not bad, f"obce miasta: {bad[:5]}")

    for period in PERIODS:
        data = get(client, f"/api/listings?period={period}&per_page=1")
        check(f"filtr period={period}", "__status" not in data, str(data)[:100])

    for flag in ("with_phone=1", "price_dropped=1", "only_original=0", "only_active=0"):
        data = get(client, f"/api/listings?{flag}&per_page=5")
        check(f"filtr {flag}", "__status" not in data, str(data)[:100])

    # telefon: filtr i licznik muszą mówić to samo. Liczymy oba w jednej
    # transakcji i bez pamięci podręcznej — przez API każdy z nich jest
    # zapamiętany w innej chwili, a w trakcie skanu baza rośnie.
    from metruj.db import session_scope
    from metruj.query import Filters, dashboard_stats, search_listings

    with session_scope() as session:
        counter = dashboard_stats(session).get("with_phone") or 0
        _, with_phone = search_listings(session, Filters(with_phone=True, per_page=1))
    check("licznik ofert z kontaktem zgadza sie z filtrem", with_phone == counter,
          f"filtr {with_phone} vs licznik {counter}")


#: Tak wygląda zapytanie wysłane przez formularz: wszystkie pola, także puste.
#: Pusta wartość w polu wielokrotnego wyboru potrafiła wyzerować wyniki, więc
#: każdy filtr sprawdzamy również w tym kształcie.
FORM_FIELDS = (
    "city", "voivodeship", "county", "district", "street", "source", "market",
    "period", "seller_type", "q", "price_min", "price_max", "area_min", "area_max",
    "price_m2_min", "price_m2_max", "plot_area_min", "plot_area_max",
    "rooms_min", "rooms_max", "floor_min", "floor_max", "year_min", "deal_max",
    "days_on_market_min", "days_on_market_max",
)


def _form_query(**values: str) -> str:
    """Zapytanie w kształcie, jaki wysyła formularz — z pustymi polami."""
    parts = [f"{name}={values.pop(name, '')}" for name in FORM_FIELDS]
    parts += ["only_original=0", "only_original=1", "only_active=0", "only_active=1",
              "with_phone=0", "price_dropped=0"]
    parts += [f"{k}={v}" for k, v in values.items()]
    return "&".join(parts)


def audit_form_shape(client: TestClient) -> None:
    """Każdy filtr przez formularz: puste pola nie mogą zmieniać wyniku."""
    print("\n=== Filtry w kształcie formularza ===")
    plain = (get(client, "/api/listings?per_page=1") or {}).get("total", 0)
    empty = (get(client, f"/api/listings?per_page=1&{_form_query()}") or {}).get("total", 0)
    check("puste pola formularza nie zmieniają wyniku", plain == empty, f"{plain} vs {empty}")

    for field, value in (
        ("city", "Warszawa"), ("voivodeship", "opolskie"), ("county", "nyski"),
        ("property_type", "mieszkanie"), ("transaction", "sprzedaz"),
        ("seller_type", "prywatna"), ("market", "wtorny"), ("period", "30dni"),
        ("price_min", "200000"), ("price_max", "400000"),
        ("area_min", "40"), ("area_max", "80"), ("rooms_min", "2"), ("rooms_max", "3"),
        ("floor_min", "1"), ("floor_max", "5"), ("year_min", "1990"),
        ("price_m2_min", "3000"), ("price_m2_max", "12000"),
        ("plot_area_min", "500"), ("plot_area_max", "5000"),
        ("days_on_market_min", "1"), ("days_on_market_max", "400"),
        ("deal_max", "0.9"), ("q", "balkon"),
    ):
        query = _form_query(**{field: value})
        narrowed = get(client, f"/api/listings?per_page=1&{query}")
        if "__status" in narrowed:
            check(f"formularz: {field}={value}", False, str(narrowed)[:110])
            continue
        total = narrowed.get("total", 0)
        check(f"formularz: {field}={value}", total > 0,
              f"zero wyników (bez filtra byłoby {plain})")

    # sortowanie w kształcie formularza — z zachowaniem pozostałych filtrów
    for sort in SORTS:
        query = _form_query(city="Warszawa") + f"&sort={sort}"
        data = get(client, f"/api/listings?per_page=5&{query}")
        check(f"formularz: sort={sort}", bool(data.get("items")), str(data)[:110])


def audit_completeness(client: TestClient) -> None:
    """Czy z ofert czytamy komplet danych, czy karta świeci pustkami."""
    print("\n=== Kompletność danych ===")
    from sqlalchemy import case, func, select

    from metruj.db import session_scope
    from metruj.models import Listing, ListingStatus

    def share(column) -> object:
        return func.round(100.0 * func.sum(case((column.is_not(None), 1), else_=0))
                          / func.count(Listing.id), 0)

    with session_scope() as session:
        rows = session.execute(
            select(
                Listing.source_key,
                func.count(Listing.id),
                share(Listing.price), share(Listing.area), share(Listing.rooms),
                share(Listing.description), share(Listing.city),
                func.round(100.0 * func.sum(case((func.json_array_length(Listing.images) > 0, 1),
                                                 else_=0)) / func.count(Listing.id), 0),
            )
            .where(Listing.status == ListingStatus.AKTYWNA)
            .group_by(Listing.source_key)
            .order_by(func.count(Listing.id).desc())
            .limit(12)
        ).all()

        print(f"  {'źródło':24s} {'ofert':>7s} {'cena':>6s} {'metraż':>7s} {'pokoje':>7s} "
              f"{'opis':>6s} {'miasto':>7s} {'zdjęcia':>8s}")
        for key, total, price, area, rooms, desc, city, images in rows:
            print(f"  {key[:24]:24s} {total:>7} {price or 0:>5.0f}% {area or 0:>6.0f}% "
                  f"{rooms or 0:>6.0f}% {desc or 0:>5.0f}% {city or 0:>6.0f}% {images or 0:>7.0f}%")
            # Oferta bez ceny i bez metrażu jest na liście bezużyteczna.
            # Ogłoszenia z BIP-ów podają cenę w załączniku, często zeskanowanym
            # bez warstwy tekstu (Kluczbork) — tam jej brak to cecha źródła.
            if total >= 50 and not key.startswith("bip_"):
                check(f"{key}: cena przy większości ofert", (price or 0) >= 50, f"{price}%")
            if total >= 50:
                check(f"{key}: miejscowość przy wszystkich", (city or 0) >= 99, f"{city}%")


def audit_presentation() -> None:
    """Czy dane nadają się do pokazania — bez znaczników i śmieci w treści."""
    print("\n=== Jak to wygląda na karcie ===")
    from sqlalchemy import func, select

    from metruj.db import session_scope
    from metruj.models import Listing, ListingStatus

    with session_scope() as session:
        def count(*where) -> int:
            return int(session.scalar(
                select(func.count(Listing.id)).where(Listing.status == ListingStatus.AKTYWNA, *where)
            ) or 0)

        tags = (Listing.description.like("%<p>%") | Listing.description.like("%<br%")
                | Listing.description.like("%</%") | Listing.description.like("%<div%")
                | Listing.description.like("%<script%") | Listing.description.like("%$(%"))
        check("opisy bez znaczników HTML i skryptów", count(tags) == 0, f"{count(tags)} ofert")
        check("tytuły niepuste", count(Listing.title == "") == 0, "puste tytuły")
        check("tytuły bez encji HTML",
              count(Listing.title.like("%&amp;%") | Listing.title.like("%&nbsp;%")) == 0,
              f"{count(Listing.title.like('%&amp;%'))} tytułów")
        absurd = count(Listing.price > 500_000_000)
        check("brak absurdalnych cen", absurd == 0, f"{absurd} ofert powyżej 500 mln")
        no_photo = count(func.json_array_length(Listing.images) == 0)
        total = count()
        check("zdjęcia przy większości ofert", no_photo < total * 0.5,
              f"{no_photo} z {total} bez zdjęcia")


def audit_data_quality() -> None:
    print("\n=== Jakość danych ===")
    from sqlalchemy import func, select

    from metruj.db import session_scope
    from metruj.models import Listing

    with session_scope() as session:
        def count(*where) -> int:
            return int(session.scalar(select(func.count(Listing.id)).where(*where)) or 0)

        check("brak ofert z ceną 0", count(Listing.price == 0) == 0,
              f"{count(Listing.price == 0)} ofert")
        check("brak ofert z metrażem 0", count(Listing.area == 0) == 0,
              f"{count(Listing.area == 0)} ofert")
        check("brak ofert z ceną za m² = 0", count(Listing.price_per_m2 == 0) == 0,
              f"{count(Listing.price_per_m2 == 0)} ofert")
        from datetime import timedelta

        from metruj.models import utcnow

        tomorrow = utcnow() + timedelta(days=1)
        future = count(Listing.published_at > tomorrow) + count(Listing.listed_at > tomorrow)
        check("żadna oferta nie jest wystawiona w przyszłości", future == 0, f"{future} ofert")
        total = count()
        no_city = count(Listing.city.is_(None))
        check("wszystkie oferty maja miejscowosc", no_city == 0, f"{no_city} z {total}")
        no_voiv = count(Listing.voivodeship.is_(None))
        check("wszystkie oferty mają województwo", no_voiv == 0, f"{no_voiv} z {total}")

        outside = session.execute(
            select(Listing.id, Listing.city, Listing.lat, Listing.lon)
            .where(Listing.lat.is_not(None))
            .where((Listing.lat < 48.9) | (Listing.lat > 55.05)
                   | (Listing.lon < 13.9) | (Listing.lon > 24.25))
            .limit(5)
        ).all()
        check("żaden punkt nie leży poza Polską", not outside, str(outside))


def main() -> int:
    with TestClient(app) as client:
        audit_pages(client)
        audit_api(client)
        audit_sorting(client)
        audit_dates(client)
        audit_filters(client)
        audit_form_shape(client)
        audit_completeness(client)
    audit_presentation()
    audit_data_quality()

    print("\n" + "=" * 70)
    if problems:
        print(f"Znalezione problemy: {len(problems)}")
        for problem in problems:
            print("  -", problem)
        return 1
    print("Wszystko sprawdzone, bez zastrzeżeń.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

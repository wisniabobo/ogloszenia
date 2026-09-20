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

    # telefon: filtr i licznik muszą mówić to samo
    stats = get(client, "/api/stats")
    with_phone = (get(client, "/api/listings?with_phone=1&per_page=1") or {}).get("total", 0)
    check("licznik ofert z kontaktem zgadza sie z filtrem",
          abs(with_phone - (stats.get("with_phone") or 0)) <= 0,
          f"filtr {with_phone} vs licznik {stats.get('with_phone')}")


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
        audit_filters(client)
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

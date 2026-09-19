"""FastAPI: interfejs webowy + REST.

Ten sam builder filtrów obsługuje widok HTML i endpointy JSON, więc lista
w przeglądarce i wynik `/api/listings` zawsze się zgadzają.
"""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
from starlette.middleware.cors import CORSMiddleware

from . import __version__
from .db import get_session_factory, init_db
from .models import (
    Agency,
    DuplicateLink,
    Favorite,
    Listing,
    OfferKind,
    SavedSearch,
    ScanRun,
    Source,
    TransactionType,
    utcnow,
)
from .query import (
    Filters,
    apply_filters,
    apply_sort,
    dashboard_stats,
    map_points,
    phone_lookup,
    search_listings,
)
from .settings import get_settings
from .utils.geo import all_cities, counties

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

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
        rooms_min=num("rooms_min", int),
        rooms_max=num("rooms_max", int),
        floor_min=num("floor_min", int),
        floor_max=num("floor_max", int),
        year_min=num("year_min", int),
        market=params.get("market") or None,
        only_original=params.get("only_original", "1") not in ("0", "false", ""),
        only_active=params.get("only_active", "1") not in ("0", "false", ""),
        with_phone=params.get("with_phone") in ("1", "true", "on"),
        price_dropped=params.get("price_dropped") in ("1", "true", "on"),
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
        "rooms": listing.rooms,
        "floor": listing.floor,
        "floors_total": listing.floors_total,
        "year_built": listing.year_built,
        "market": listing.market,
        "location": {
            "voivodeship": listing.voivodeship,
            "county": listing.county,
            "commune": listing.commune,
            "city": listing.city,
            "district": listing.district,
            "street": listing.street,
            "lat": listing.lat,
            "lon": listing.lon,
        },
        "seller": {
            "type": listing.seller_type.value,
            "name": listing.seller_name,
            "agency_id": listing.agency_id,
        },
        "phones": [p.masked if not reveal_phone else (p.national or p.masked) for p in listing.phones],
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
def view_dashboard(request: Request, db: DB):
    stats = dashboard_stats(db)
    recent_runs = list(
        db.scalars(select(ScanRun).order_by(desc(ScanRun.started_at)).limit(12))
    )
    sources = list(db.scalars(select(Source).order_by(Source.category, Source.name)))
    newest = list(
        db.scalars(
            apply_sort(apply_filters(select(Listing), {"only_original": True}), "najnowsze").limit(8)
        )
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"stats": stats, "runs": recent_runs, "sources": sources, "newest": newest,
         "active": "pulpit"},
    )


@app.get("/nieruchomosci", response_class=HTMLResponse)
def view_listings(request: Request, db: DB):
    filters = _filters_from_query(request)
    _default_to_sale(request, filters)
    listings, total = search_listings(db, filters)
    sources = list(db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.name)))
    return templates.TemplateResponse(
        request,
        "listings.html",
        {
            "listings": listings,
            "total": total,
            "filters": filters,
            "pages": max(1, math.ceil(total / filters.per_page)),
            "sources": sources,
            "cities": all_cities(),
            "counties": counties(),
            "active": "nieruchomosci",
            "query_string": str(request.query_params),
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
    sources = list(db.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.name)))
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "filters": filters,
            "sources": sources,
            "cities": all_cities(),
            "counties": counties(),
            "active": "mapa",
            "query_string": str(request.query_params),
        },
    )


@app.get("/biura", response_class=HTMLResponse)
def view_agencies(request: Request, db: DB):
    q = request.query_params.get("q")
    stmt = select(Agency).order_by(desc(Agency.listings_count), Agency.name)
    if q:
        stmt = stmt.where(Agency.name.ilike(f"%{q}%"))
    agencies = list(db.scalars(stmt.limit(500)))
    return templates.TemplateResponse(
        request, "agencies.html", {"agencies": agencies, "q": q or "", "active": "biura"}
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
    return templates.TemplateResponse(
        request, "sources.html", {"sources": sources, "runs": runs, "active": "zrodla"}
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

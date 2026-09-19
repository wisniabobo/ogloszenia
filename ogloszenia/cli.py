"""CLI: `ogl <komenda>`."""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, select

from . import __version__
from .db import init_db, session_scope
from .models import Agency, Listing, ListingStatus, SavedSearch, Source
from .query import Filters, dashboard_stats, phone_lookup, search_listings
from .settings import get_settings, sources_config
from .utils.http import HttpClient

app = typer.Typer(
    add_completion=False,
    help="Bot monitorująco-scrapujący: nieruchomości, licytacje i przetargi (woj. opolskie).",
)
console = Console()


def _money(value: float | None) -> str:
    return f"{value:,.0f}".replace(",", " ") if value else "—"


# --------------------------------------------------------------------------- #
@app.command("init-db")
def cmd_init_db() -> None:
    """Tworzy bazę i wczytuje rejestr źródeł."""
    from .pipeline.runner import sync_sources

    init_db()
    with session_scope() as session:
        added = sync_sources(session)
    console.print(f"[green]Baza gotowa.[/] Dodano źródeł: {added}")
    console.print(f"Plik bazy: [dim]{get_settings().database_url}[/]")


@app.command("sources")
def cmd_sources(
    category: str = typer.Option(None, help="Filtruj po kategorii"),
    enabled_only: bool = typer.Option(False, "--enabled", help="Tylko włączone"),
) -> None:
    """Wypisuje rejestr źródeł."""
    init_db()
    from .pipeline.runner import sync_sources

    with session_scope() as session:
        sync_sources(session)
        stmt = select(Source).order_by(Source.category, Source.name)
        if category:
            stmt = stmt.where(Source.category == category)
        if enabled_only:
            stmt = stmt.where(Source.enabled.is_(True))
        sources = list(session.scalars(stmt))

        table = Table(title=f"Źródła ({len(sources)})", show_lines=False)
        for column in ("klucz", "nazwa", "kategoria", "rodzaj", "co ile", "oferty", "stan"):
            table.add_column(column)
        for source in sources:
            state = (
                "[dim]wyłączone[/]" if not source.enabled
                else ("[red]błąd[/]" if source.last_error
                      else ("[green]ok[/]" if source.last_ok_at else "[yellow]nieuruchomione[/]"))
            )
            table.add_row(
                source.key, source.name[:38], source.category, source.kind.value,
                f"{source.interval_minutes}m", str(source.total_listings), state,
            )
        console.print(table)


@app.command("check-sources")
def cmd_check_sources(
    only: list[str] = typer.Option(None, "--only", help="Sprawdź wybrane klucze"),
    timeout: float = typer.Option(12.0, help="Limit czasu na adres [s]"),
) -> None:
    """Odpytuje adresy wszystkich źródeł i pokazuje, które odpowiadają.

    Przydaje się przed włączeniem źródła oznaczonego w YAML-u jako `verify: true`.
    """

    async def _check() -> list[tuple[str, str, str]]:
        entries = sources_config().get("sources", [])
        if only:
            entries = [e for e in entries if e.get("key") in only]
        results: list[tuple[str, str, str]] = []
        async with HttpClient(timeout=timeout, concurrency=6) as client:
            async def probe(entry: dict) -> None:
                urls = entry.get("config", {}).get("urls") or []
                url = (urls[0].format(page=1) if urls else entry.get("base_url"))
                if not url:
                    results.append((entry["key"], "—", "brak adresu"))
                    return
                try:
                    resp = await client.request(url, retries=0)
                    results.append((entry["key"], url, f"[green]{resp.status_code}[/]"))
                except Exception as exc:
                    results.append((entry["key"], url, f"[red]{str(exc)[:60]}[/]"))

            await asyncio.gather(*(probe(e) for e in entries))
        return results

    results = asyncio.run(_check())
    table = Table(title="Dostępność źródeł")
    table.add_column("klucz")
    table.add_column("adres", overflow="fold")
    table.add_column("wynik")
    for key, url, status in sorted(results):
        table.add_row(key, url[:72], status)
    console.print(table)


@app.command("scan")
def cmd_scan(
    source: list[str] = typer.Option(None, "--source", "-s", help="Skanuj tylko te źródła"),
    category: list[str] = typer.Option(None, "--category", "-c", help="Skanuj całą kategorię"),
    region: str = typer.Option(None, help="Województwo (domyślnie z konfiguracji)"),
    pages: int = typer.Option(None, help="Ile stron na sekcję"),
    limit: int = typer.Option(None, help="Maks. ofert na źródło"),
    details: bool = typer.Option(True, help="Dociągać karty ofert (wolniej, więcej danych)"),
    all_sources: bool = typer.Option(False, "--all", help="Także źródła wyłączone w YAML-u"),
    notify: bool = typer.Option(False, help="Wyślij powiadomienia o nowych ofertach"),
) -> None:
    """Uruchamia jednorazowy skan."""
    from .alerts import dispatch_alerts
    from .pipeline.runner import run_scan

    init_db()
    console.print("[bold]Skanowanie…[/]")
    result = asyncio.run(
        run_scan(
            only=list(source) or None,
            categories=list(category) or None,
            max_pages=pages,
            max_items=limit,
            voivodeship=region,
            fetch_details=details,
            include_disabled=all_sources,
        )
    )

    table = Table(title="Wynik skanu")
    for column in ("źródło", "pobrane", "nowe", "zmienione", "kopie", "zmiany cen", "wygaszone", "błędy"):
        table.add_column(column, justify="right" if column != "źródło" else "left")
    for entry in sorted(result.sources, key=lambda s: -s.new):
        table.add_row(
            entry.source_key + ("" if entry.ok else " [red]✗[/]"),
            str(entry.fetched), f"[green]{entry.new}[/]" if entry.new else "0",
            str(entry.updated), str(entry.duplicates), str(entry.price_changes),
            str(entry.removed), str(entry.errors),
        )
    console.print(table)
    totals = result.totals
    console.print(
        f"Razem: pobrane {totals['fetched']}, [green]nowe {totals['new']}[/], "
        f"zmienione {totals['updated']}, kopie {totals['duplicates']}, błędy {totals['errors']}"
    )
    for entry in result.sources:
        if not entry.ok:
            console.print(f"  [red]{entry.source_key}[/]: {entry.message[:160]}")

    if notify and result.new_listing_ids:
        sent = asyncio.run(dispatch_alerts(result.new_listing_ids))
        console.print(f"Wysłano powiadomień: {sent}")


@app.command("watch")
def cmd_watch() -> None:
    """Uruchamia ciągły monitoring według harmonogramu."""
    from .scheduler import run_forever

    init_db()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        console.print("\n[dim]Zatrzymano.[/]")


@app.command("web")
def cmd_web(
    host: str = typer.Option(None), port: int = typer.Option(None),
    reload: bool = typer.Option(False, help="Tryb deweloperski"),
) -> None:
    """Startuje interfejs webowy i API."""
    import uvicorn

    settings = get_settings()
    init_db()
    host = host or settings.web_host
    port = port or settings.web_port
    console.print(f"[green]Interfejs:[/] http://{host}:{port}  ·  [dim]API: /docs[/]")
    uvicorn.run("ogloszenia.api:app", host=host, port=port, reload=reload, log_level="info")


@app.command("search")
def cmd_search(
    city: str = typer.Option(None), q: str = typer.Option(None),
    price_max: float = typer.Option(None), area_min: float = typer.Option(None),
    kind: str = typer.Option(None, help="nieruchomosc | licytacja | przetarg | wykaz"),
    seller: str = typer.Option(None, help="prywatna | posrednik | deweloper"),
    limit: int = typer.Option(20),
) -> None:
    """Szuka w bazie z linii poleceń."""
    init_db()
    filters = Filters(city=city, q=q, price_max=price_max, area_min=area_min,
                      kind=kind, seller_type=seller, per_page=limit)
    with session_scope() as session:
        listings, total = search_listings(session, filters)
        table = Table(title=f"Wyniki ({total})")
        for column in ("id", "tytuł", "cena", "zł/m²", "m²", "miejscowość", "oferent", "źródło", "stoi"):
            table.add_column(column, overflow="ellipsis")
        for listing in listings:
            table.add_row(
                str(listing.id), listing.title[:46], _money(listing.price),
                _money(listing.price_per_m2), f"{listing.area:g}" if listing.area else "—",
                listing.city or "—", listing.seller_type.value, listing.source_key,
                f"{listing.days_on_market}d",
            )
        console.print(table)


@app.command("phone")
def cmd_phone(number: str) -> None:
    """Pokazuje wszystkie oferty powiązane z numerem telefonu."""
    init_db()
    with session_scope() as session:
        listings = phone_lookup(session, number)
        if not listings:
            console.print("[yellow]Brak ofert z tym numerem.[/]")
            return
        table = Table(title=f"Oferty numeru {number} ({len(listings)})")
        for column in ("id", "tytuł", "cena", "miejscowość", "oferent", "źródło", "dodana"):
            table.add_column(column, overflow="ellipsis")
        for listing in listings:
            table.add_row(
                str(listing.id), listing.title[:44], _money(listing.price), listing.city or "—",
                listing.seller_type.value, listing.source_key,
                listing.first_seen_at.strftime("%d.%m.%Y"),
            )
        console.print(table)
        console.print(
            "[dim]Ten sam numer w wielu ofertach oznacza zwykle biuro podszywające się "
            "pod ofertę prywatną albo tę samą nieruchomość wystawioną wielokrotnie.[/]"
        )


@app.command("agencies")
def cmd_agencies(
    min_offers: int = typer.Option(0, help="Pokaż biura z co najmniej tyloma ofertami"),
    export: Path = typer.Option(None, help="Zapisz listę do pliku YAML"),
) -> None:
    """Rejestr biur nieruchomości zbudowany z zebranych ofert."""
    import yaml

    init_db()
    with session_scope() as session:
        agencies = list(
            session.scalars(
                select(Agency)
                .where(Agency.listings_count >= min_offers)
                .order_by(desc(Agency.listings_count), Agency.name)
            )
        )
        table = Table(title=f"Biura nieruchomości ({len(agencies)})")
        for column in ("id", "nazwa", "miasto", "oferty", "telefony", "www"):
            table.add_column(column, overflow="ellipsis")
        for agency in agencies:
            table.add_row(
                str(agency.id), agency.name[:44], agency.city or "—", str(agency.listings_count),
                ", ".join(agency.phones or [])[:32] or "—", (agency.website or "—")[:34],
            )
        console.print(table)

        if export:
            payload = {
                "meta": {"wojewodztwo": "opolskie", "zrodlo": "auto (z zebranych ofert)"},
                "lokalne": [
                    {
                        "slug": a.slug, "name": a.name, "city": a.city, "website": a.website,
                        "phones": a.phones or [], "listings": a.listings_count,
                        "verified": a.verified,
                    }
                    for a in agencies
                ],
            }
            export.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            console.print(f"[green]Zapisano[/] {len(agencies)} biur do {export}")


@app.command("stats")
def cmd_stats() -> None:
    """Podsumowanie zawartości bazy."""
    init_db()
    with session_scope() as session:
        stats = dashboard_stats(session)
        table = Table(title="Stan bazy")
        table.add_column("miara")
        table.add_column("wartość", justify="right")
        for label, key in [
            ("Oferty aktywne", "total"), ("W tym oryginalne", "original"), ("Kopie", "copies"),
            ("Nowe (24h)", "today"), ("Nowe (7 dni)", "week"), ("Po obniżce ceny", "price_drops"),
            ("Licytacje", "auctions"), ("Przetargi", "tenders"), ("Z numerem telefonu", "with_phone"),
            ("Biura w rejestrze", "agencies"),
        ]:
            table.add_row(label, str(stats[key]))
        if stats["avg_price_m2"]:
            table.add_row("Średnia cena m² (mieszkania)", _money(stats["avg_price_m2"]) + " zł")
        console.print(table)

        if stats["by_source"]:
            per_source = Table(title="Oferty według źródła")
            per_source.add_column("źródło")
            per_source.add_column("oferty", justify="right")
            for row in stats["by_source"]:
                per_source.add_row(row["source"], str(row["count"]))
            console.print(per_source)


@app.command("export")
def cmd_export(
    output: Path = typer.Argument(..., help="Plik .csv albo .json"),
    city: str = typer.Option(None), kind: str = typer.Option(None),
    limit: int = typer.Option(5000),
) -> None:
    """Eksportuje oferty do CSV/JSON (numery telefonów w postaci zamaskowanej)."""
    init_db()
    filters = Filters(city=city, kind=kind, per_page=limit, only_original=True)
    with session_scope() as session:
        listings, total = search_listings(session, filters)
        rows = [
            {
                "id": x.id, "zrodlo": x.source_key, "url": x.url, "tytul": x.title,
                "rodzaj": x.kind.value, "typ": x.property_type.value,
                "transakcja": x.transaction.value, "cena": x.price, "cena_m2": x.price_per_m2,
                "powierzchnia": x.area, "pokoje": x.rooms, "pietro": x.floor,
                "miejscowosc": x.city, "dzielnica": x.district, "ulica": x.street,
                "powiat": x.county, "oferent": x.seller_type.value, "nazwa_oferenta": x.seller_name,
                "telefon": (x.phones[0].masked if x.phones else None),
                "dni_na_rynku": x.days_on_market, "kopie": x.copies_count,
                "pierwszy_raz": x.first_seen_at.isoformat(),
                "termin": x.event_date.isoformat() if x.event_date else None,
                "sygnatura": x.case_number,
            }
            for x in listings
        ]

    if output.suffix.lower() == ".json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["id"])
            writer.writeheader()
            writer.writerows(rows)
    console.print(f"[green]Zapisano[/] {len(rows)} z {total} ofert do {output}")


@app.command("searches")
def cmd_searches() -> None:
    """Lista zapisanych poszukiwań."""
    init_db()
    with session_scope() as session:
        searches = list(session.scalars(select(SavedSearch)))
        table = Table(title=f"Poszukiwania ({len(searches)})")
        for column in ("id", "nazwa", "filtry", "kanały", "trafienia"):
            table.add_column(column, overflow="fold")
        for search in searches:
            table.add_row(
                str(search.id), search.name,
                ", ".join(f"{k}={v}" for k, v in (search.query or {}).items())[:70],
                ", ".join(search.channels or []), str(search.hits),
            )
        console.print(table)


@app.command("prune")
def cmd_prune(days: int = typer.Option(None, help="Usuń oferty nieaktywne starsze niż N dni")) -> None:
    """Czyści stare, nieaktywne oferty (RODO: dane nie leżą bez końca)."""
    from datetime import timedelta

    from .models import utcnow

    init_db()
    days = days or get_settings().retention_days
    cutoff = utcnow() - timedelta(days=days)
    with session_scope() as session:
        stale = list(
            session.scalars(
                select(Listing).where(
                    Listing.status != ListingStatus.AKTYWNA, Listing.last_seen_at < cutoff
                )
            )
        )
        for listing in stale:
            session.delete(listing)
    console.print(f"[green]Usunięto[/] {len(stale)} ofert starszych niż {days} dni")


@app.command("version")
def cmd_version() -> None:
    """Wersja."""
    console.print(f"ogloszenia {__version__} (Python {sys.version.split()[0]})")


@app.callback()
def main() -> None:
    """Monitor rynku nieruchomości, licytacji i przetargów — start: woj. opolskie."""


if __name__ == "__main__":
    app()

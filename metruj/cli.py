"""CLI: `metruj <komenda>` (stara nazwa `ogl` działa dalej)."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, func, select

from . import __version__
from .db import init_db, refresh_statistics, session_scope
from .models import Agency, Listing, ListingStatus, SavedSearch, Source
from .query import Filters, dashboard_stats, phone_lookup, search_listings
from .settings import get_settings, sources_config
from .utils.http import HttpClient

app = typer.Typer(
    add_completion=False,
    help="Metruj — monitor rynku nieruchomości: oferty, licytacje i przetargi z całej Polski.",
)
console = Console()

# Przebiegi chodzą z systemd, gdzie jedynym oknem na to, co się dzieje, jest
# dziennik. Bez tego `journalctl` pokazywał „Skanowanie…" i nic więcej przez
# godzinę, a operator nie miał jak odróżnić pracy od zawieszenia.
logging.basicConfig(
    level=os.environ.get("METRUJ_LOG_LEVEL", os.environ.get("OGL_LOG_LEVEL", "INFO")).upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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
            if (source.config or {}).get("requires_js"):
                state = "[magenta]wymaga JS[/]"
            elif not source.enabled:
                state = "[dim]wyłączone[/]"
            elif source.last_error:
                state = "[red]błąd[/]"
            elif source.last_ok_at:
                state = "[green]ok[/]"
            else:
                state = "[yellow]nieuruchomione[/]"
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
    region: list[str] = typer.Option(
        None, "--region", help="Zawęź do województw (domyślnie cała Polska)"
    ),
    pages: int = typer.Option(None, help="Ile stron na sekcję"),
    limit: int = typer.Option(None, help="Maks. ofert na źródło"),
    details: bool = typer.Option(True, help="Dociągać karty ofert (wolniej, więcej danych)"),
    all_sources: bool = typer.Option(False, "--all", help="Także źródła wyłączone w YAML-u"),
    deep: bool = typer.Option(
        False, "--deep", help="Przejdź wyniki do końca zamiast pierwszych stron"
    ),
    notify: bool = typer.Option(False, help="Wyślij powiadomienia o nowych ofertach"),
) -> None:
    """Uruchamia jednorazowy skan.

    Domyślnie bierze najnowsze strony każdej sekcji — chodzi o to, żeby nowa
    oferta trafiła do bazy w kilka minut. `--deep` przechodzi wyniki do końca
    i zbiera komplet; to robota na raz na dobę, nie co kwadrans.
    """
    from .alerts import dispatch_alerts
    from .pipeline.runner import run_scan

    init_db()
    console.print("[bold]Skanowanie…[/]")
    result = asyncio.run(
        run_scan(
            only=list(source or []) or None,
            categories=list(category or []) or None,
            max_pages=pages,
            max_items=limit,
            scope=list(region or []) or None,
            fetch_details=details,
            include_disabled=all_sources,
            deep=deep,
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
    uvicorn.run("metruj.api:app", host=host, port=port, reload=reload, log_level="info")


@app.command("napraw")
def cmd_repair(
    regeocode_all: bool = typer.Option(
        False, "--przelicz-wszystko", help="Unieważnij współrzędne wszystkich ofert"
    ),
) -> None:
    """Przelicza pola policzone starym, błędnym kodem.

    Uruchamiane raz po wdrożeniu: skan aktualizuje tylko to, co akurat przyszło
    ze źródła, a ogłoszenie sprzed pół roku może już ze źródła nie przychodzić.
    """
    from .pipeline.market import recompute as recompute_market
    from .pipeline.repair import repair

    init_db()
    with session_scope() as session:
        stats = repair(session, regeocode_all=regeocode_all)
    with session_scope() as session:
        market = recompute_market(session)
    refresh_statistics()
    console.print(
        f"[green]Naprawa:[/] ceny {stats.prices}, cena za m² {stats.price_per_m2}, "
        f"powierzchnia gruntu {stats.land_area}, regiony {stats.regions}, "
        f"poprawione lokalizacje {stats.relocated}, powiat w polu miasta "
        f"{stats.counties_as_cities}, nieaktualne punkty {stats.stale_points}, "
        f"wygaszone śmieci {stats.navigation}, rozpięte kopie {stats.unlinked}, "
        f"do przeliczenia na mapie {stats.regeocode}"
    )
    console.print(
        f"[green]Daty:[/] przeczytane od nowa z portalu {stats.dates_reparsed}, "
        f"odwrócona zamiana dnia z miesiącem {stats.dates_unswapped}, "
        f"wyczyszczone z przyszłości {stats.dates_cleared}, "
        f"uzupełniona data wystawienia {stats.listed_at}"
    )
    console.print(
        f"[green]Treść:[/] odtworzone miejscowości {stats.cities_restored}, "
        f"odkodowane tytuły {stats.titles}, opisy ze skryptami {stats.descriptions}, "
        f"licytacje i przetargi po terminie "
        f"{stats.concluded}, stare ogłoszenia z BIP {stats.old_notices}"
    )
    console.print(f"[green]Odniesienie rynkowe:[/] {market}")


@app.command("okazje")
def cmd_deals(
    limit: int = typer.Option(15, help="Ile ofert pokazać"),
    transaction: str = typer.Option("sprzedaz", help="sprzedaz / wynajem / wszystkie"),
) -> None:
    """Przelicza odniesienie rynkowe i wypisuje największe okazje.

    Domyślnie sprzedaż: wynajem i sprzedaż na jednej liście nie mają wspólnej
    miary, bo „cena za m²" znaczy w nich co innego.
    """
    from .models import Listing, TransactionType
    from .pipeline.market import recompute as recompute_market

    init_db()
    with session_scope() as session:
        console.print(f"[green]Odniesienie rynkowe:[/] {recompute_market(session)}")
        stmt = select(Listing).where(
            Listing.deal_ratio.is_not(None),
            Listing.deal_level.in_(["miasto", "powiat"]),
        )
        if transaction != "wszystkie":
            stmt = stmt.where(Listing.transaction == TransactionType(transaction))
        rows = list(session.scalars(stmt.order_by(Listing.deal_ratio).limit(limit)))
        table = Table(title="Największe okazje wobec mediany okolicy")
        for column in ("różnica", "cena", "m²", "zł/m²", "miejscowość", "wobec", "tytuł"):
            table.add_column(column, justify="right" if column != "tytuł" else "left")
        for listing in rows:
            table.add_row(
                f"-{round((1 - listing.deal_ratio) * 100)}%",
                f"{listing.price:,.0f}".replace(",", " ") if listing.price else "—",
                f"{listing.area:g}" if listing.area else "—",
                f"{listing.price_per_m2:,.0f}".replace(",", " ") if listing.price_per_m2 else "—",
                (listing.city or "—")[:18],
                listing.deal_level or "—",
                listing.title[:46],
            )
        console.print(table)


@app.command("kontakty")
def cmd_details(
    limit: int = typer.Option(300, help="Ile kart ofert dociągnąć w tym przebiegu"),
    source: list[str] = typer.Option(None, "--source", "-s", help="Tylko te źródła"),
) -> None:
    """Dociąga karty ofert po numery telefonu i pełne opisy.

    Listy wyników portali numeru nie podają; karta pojedynczej oferty — owszem.
    Wejście na kartę to jedno zapytanie na ofertę, więc przebieg jest
    budżetowany: bierze oferty bez kontaktu, najnowsze najpierw.
    """
    from .pipeline.details import enrich_details

    init_db()
    stats = asyncio.run(
        enrich_details(limit=limit, sources=list(source or []) or None)
    )
    console.print(
        f"[green]Karty ofert:[/] sprawdzone {stats.checked}, pobrane {stats.fetched}, "
        f"z numerem {stats.with_phone}, uzupełnionych pól {stats.enriched}, "
        f"nieudane {stats.failed}"
    )


@app.command("geocode")
def cmd_geocode(
    limit: int = typer.Option(400, help="Ile ofert geokodować w tym przebiegu"),
    region: list[str] = typer.Option(
        None, "--region", help="Zawęź do województw (domyślnie cała Polska)"
    ),
    all_listings: bool = typer.Option(False, "--all", help="Także oferty nieaktywne"),
) -> None:
    """Nadaje ofertom współrzędne (GUGiK, zapasowo OpenStreetMap).

    Wyniki trafiają do cache'u, więc ten sam adres pytamy raz w życiu —
    powtórne uruchomienie jest niemal darmowe i nie obciąża cudzych serwerów.
    """
    from .pipeline.geocode import geocode_pending

    init_db()
    stats = asyncio.run(
        geocode_pending(
            limit=limit, only_active=not all_listings, scope=list(region or []) or None
        )
    )
    console.print(
        f"[green]Geokodowanie:[/] sprawdzone {stats.checked}, z cache {stats.from_cache}, "
        f"nowe {stats.geocoded}, poprawione regiony {stats.corrected}, "
        f"nieudane {stats.failed}"
    )
    with session_scope() as session:
        total = session.scalar(select(func.count(Listing.id))) or 0
        located = (
            session.scalar(select(func.count(Listing.id)).where(Listing.lat.is_not(None))) or 0
        )
        console.print(f"Na mapie: {located}/{total} ofert")


@app.command("add-site")
def cmd_add_site(
    url: str = typer.Argument(..., help="Adres strony biura lub małego portalu"),
    name: str = typer.Option(None, help="Nazwa źródła (domyślnie z domeny)"),
    key: str = typer.Option(None, help="Klucz źródła (domyślnie z domeny)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Tylko sprawdź, nie zapisuj"),
    max_offers: int = typer.Option(300, help="Limit ofert pobieranych z tej strony"),
) -> None:
    """Sprawdza stronę i dopisuje ją do rejestru źródeł.

    Tak rośnie pokrycie: większość „portali" w tej branży to w rzeczywistości
    strony pojedynczych biur. Uniwersalny scraper czyta je po mapie strony
    i danych strukturalnych, więc dołożenie kolejnej witryny to jedna komenda,
    a nie nowy kawałek kodu.
    """
    import yaml

    from .scrapers import ScrapeContext
    from .scrapers.sitemap import SitemapScraper
    from .settings import CONFIG_DIR
    from .utils.http import HttpClient
    from .utils.text import slugify

    base = url if url.startswith("http") else f"https://{url}"
    host = base.split("://", 1)[-1].split("/")[0]
    source_key = key or slugify(host.replace("www.", "")).replace("-pl", "_pl").replace("-", "_")
    source_name = name or host.replace("www.", "")

    async def probe() -> tuple[int, list]:
        async with HttpClient(concurrency=3) as client:
            scraper = SitemapScraper(
                client, {"base_url": base, "max_offers": max_offers}, source_key=source_key
            )
            ctx = ScrapeContext(max_pages=1, max_items=5)
            found = await scraper.discover_offers(base, ctx)
            samples = [item async for item in scraper.run(ctx)]
            return len(found), samples

    console.print(f"[bold]Sprawdzam[/] {base} …")
    try:
        candidates, samples = asyncio.run(probe())
    except Exception as exc:
        console.print(f"[red]Nie udało się:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc

    console.print(f"adresów wyglądających na oferty: [bold]{candidates}[/]")
    if not samples:
        console.print(
            "[yellow]Nie udało się sparsować żadnej oferty.[/] "
            "Strona może renderować treść skryptem albo mieć nietypowe adresy — "
            "spróbuj podać wzorzec przez `config.offer_pattern` w sources.yaml."
        )
        raise typer.Exit(1)

    table = Table(title=f"Próbka z {source_name}")
    for column in ("cena", "m²", "pokoje", "typ", "transakcja", "tytuł"):
        table.add_column(column, overflow="ellipsis")
    for item in samples:
        table.add_row(
            _money(item.price), str(item.area or "—"), str(item.rooms or "—"),
            item.property_type.value, item.transaction.value, item.title[:46],
        )
    console.print(table)

    entry = {
        "key": source_key,
        "name": source_name,
        "scraper": "sitemap",
        "kind": "nieruchomosc",
        "category": "strony_biur",
        "base_url": base,
        "enabled": True,
        "interval_minutes": 360,
        "coverage": "lokalny",
        "sprawdzono": str(__import__("datetime").date.today()),
        "config": {"base_url": base, "max_offers": max_offers},
    }

    if dry_run:
        console.print("[dim]--dry-run: nic nie zapisuję. Wpis wyglądałby tak:[/]")
        console.print(yaml.safe_dump([entry], allow_unicode=True, sort_keys=False))
        return

    path = CONFIG_DIR / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    existing = {s.get("key") for s in data.get("sources", [])}
    if source_key in existing:
        console.print(f"[yellow]Źródło {source_key} już jest w rejestrze.[/]")
        return
    data.setdefault("sources", []).append(entry)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )
    console.print(
        f"[green]Dodano[/] {source_key} do config/sources.yaml "
        f"({len(data['sources'])} źródeł łącznie)"
    )
    console.print(f"Zbierz oferty: [bold]ogl scan -s {source_key} --deep[/]")


@app.command("regeocode")
def cmd_regeocode(
    imprecise: bool = typer.Option(
        True, "--imprecise/--all",
        help="Tylko oferty z ulicą, które wylądowały na środku miejscowości",
    ),
    run_now: bool = typer.Option(True, "--run/--no-run", help="Od razu przelicz od nowa"),
    limit: int = typer.Option(3000, help="Ile ofert przeliczyć w tym przebiegu"),
) -> None:
    """Unieważnia współrzędne i liczy je od nowa.

    Potrzebne, gdy poprawka w geokoderze sprawia, że dawne wyniki są gorsze,
    niż mogłyby być. Cache trzyma bowiem także **nieudane** dopasowania —
    bez wyczyszczenia go poprawka nie miałaby żadnego skutku dla danych,
    które już zebraliśmy.
    """
    from sqlalchemy import delete, update

    from .models import GeocodeCache
    from .pipeline.geocode import geocode_pending

    init_db()
    with session_scope() as session:
        if imprecise:
            # Oferty, które MAJĄ ulicę, a mimo to wylądowały na środku miasta —
            # dokładnie te, które poprawka geokodera potrafi teraz ustawić lepiej.
            target = select(Listing.id).where(
                Listing.street.is_not(None),
                Listing.geo_precision.in_(["city", "district"]),
            )
            # Uwaga: o tym, czy wpis nadaje się do przeliczenia, decyduje
            # ZAPYTANIE, a nie wynik. Gdy GUGiK odpowie poziomem miejscowości,
            # pole `street` w wyniku jest puste — filtrowanie po nim nie
            # znajdowało niczego i cache zostawał z błędnymi danymi.
            keys = {
                row[0]
                for row in session.execute(
                    select(GeocodeCache.query_hash).where(
                        GeocodeCache.precision.in_(["city", "district"]),
                        GeocodeCache.query.contains(","),
                    )
                )
            }
        else:
            target = select(Listing.id).where(Listing.lat.is_not(None))
            keys = {row[0] for row in session.execute(select(GeocodeCache.query_hash))}

        ids = [row[0] for row in session.execute(target)]
        if keys:
            session.execute(delete(GeocodeCache).where(GeocodeCache.query_hash.in_(keys)))
        if ids:
            session.execute(
                update(Listing).where(Listing.id.in_(ids)).values(
                    lat=None, lon=None, geo_precision=None, geo_source=None
                )
            )
    console.print(
        f"[yellow]Unieważniono[/] współrzędne {len(ids)} ofert i {len(keys)} wpisów cache'u"
    )

    if run_now and ids:
        stats = asyncio.run(geocode_pending(limit=limit))
        console.print(f"[green]Przeliczono:[/] {stats}")
        with session_scope() as session:
            from sqlalchemy import func as sql_func

            rows = session.execute(
                select(Listing.geo_precision, sql_func.count(Listing.id))
                .where(Listing.lat.is_not(None))
                .group_by(Listing.geo_precision)
            ).all()
        table = Table(title="Dokładność położenia po przeliczeniu")
        table.add_column("poziom")
        table.add_column("ofert", justify="right")
        labels = {"address": "dokładny adres", "street": "ulica", "city": "środek miejscowości"}
        for precision, count in sorted(rows, key=lambda r: -r[1]):
            table.add_row(labels.get(precision, str(precision)), str(count))
        console.print(table)


@app.command("apify-actors")
def cmd_apify_actors(
    query: str = typer.Argument("nieruchomosci", help="Czego szukać w katalogu Apify"),
    limit: int = typer.Option(10),
) -> None:
    """Pokazuje gotowe scrapery z katalogu Apify (katalog jest publiczny, bez tokenu).

    Przydaje się dla portali, które renderują wyniki w przeglądarce i których
    nie da się odczytać samym pobieraniem HTML.
    """
    from .apis.apify import search_actors

    actors = asyncio.run(search_actors(query, limit))
    if not actors:
        console.print("[yellow]Nic nie znaleziono albo katalog nie odpowiedział.[/]")
        return
    table = Table(title=f"Apify — wyniki dla: {query} ({len(actors)})")
    for column in ("aktor", "tytuł", "uruchomienia", "adres"):
        table.add_column(column, overflow="ellipsis")
    for actor in actors:
        table.add_row(actor.actor_id, actor.title[:44], str(actor.total_runs or "—"), actor.url)
    console.print(table)
    console.print(
        "[dim]Uruchamianie aktorów wymaga tokenu (OGL_APIFY_TOKEN) i zużywa "
        "jednostki z pakietu Apify. Reszta bota działa bez tego.[/]"
    )


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
        for column in ("id", "nazwa", "miasto", "zebrane", "deklarowane", "pokrycie", "telefony"):
            table.add_column(column, overflow="ellipsis")
        for agency in agencies:
            expected = agency.listings_expected or 0
            if expected:
                ratio = 100 * agency.listings_count / expected
                colour = "green" if ratio >= 80 else ("yellow" if ratio >= 40 else "red")
                coverage = f"[{colour}]{ratio:.0f}%[/]"
            else:
                coverage = "—"
            table.add_row(
                str(agency.id), agency.name[:38], agency.city or "—",
                str(agency.listings_count), str(expected or "—"), coverage,
                ", ".join(agency.phones or [])[:26] or "—",
            )
        console.print(table)

        missing = sum(
            max(0, (a.listings_expected or 0) - a.listings_count) for a in agencies
        )
        if missing:
            console.print(
                f"[dim]Do zebrania: {missing} ofert, których biura deklarują więcej niż mamy. "
                f"Uruchom `ogl scan --deep`, żeby przejść wyniki do końca.[/]"
            )

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
    from .pipeline.prune import delete_listings

    with session_scope() as session:
        ids = [
            row[0]
            for row in session.execute(
                select(Listing.id).where(
                    Listing.status != ListingStatus.AKTYWNA, Listing.last_seen_at < cutoff
                )
            )
        ]
        removed = delete_listings(session, ids)
    console.print(f"[green]Usunięto[/] {removed} ofert starszych niż {days} dni")


@app.command("drop-source")
def cmd_drop_source(
    source: str = typer.Argument(..., help="Klucz źródła do wyczyszczenia"),
    yes: bool = typer.Option(False, "--yes", help="Nie pytaj o potwierdzenie"),
) -> None:
    """Usuwa wszystkie oferty danego źródła (np. po zmianie jego klucza)."""
    from .pipeline.prune import delete_by_source

    init_db()
    with session_scope() as session:
        count = session.scalar(
            select(func.count(Listing.id)).where(Listing.source_key == source)
        ) or 0
    if not count:
        console.print(f"[yellow]Źródło {source} nie ma żadnych ofert.[/]")
        return
    if not yes and not typer.confirm(f"Usunąć {count} ofert ze źródła {source}?"):
        console.print("[dim]Anulowano.[/]")
        return
    with session_scope() as session:
        removed = delete_by_source(session, source)
    console.print(f"[green]Usunięto[/] {removed} ofert ze źródła {source}")


@app.command("version")
def cmd_version() -> None:
    """Wersja."""
    console.print(f"ogloszenia {__version__} (Python {sys.version.split()[0]})")


@app.callback()
def main() -> None:
    """Monitor rynku nieruchomości, licytacji i przetargów — start: woj. opolskie."""


if __name__ == "__main__":
    app()

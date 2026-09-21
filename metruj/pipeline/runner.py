"""Orkiestracja przebiegu: źródła -> scrapery -> normalizacja -> baza -> alerty."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import (
    Listing,
    ListingStatus,
    OfferKind,
    Phone,
    PriceHistory,
    ScanRun,
    Source,
    utcnow,
)
from ..scrapers import SCRAPERS, ScrapeContext
from ..scrapers.base import RawListing
from ..scrapers.generic_html import GenericHtmlScraper
from ..settings import get_settings, sources_config
from ..utils.http import HttpClient
from ..utils.phones import PhoneNumber
from .dedup import compute_fingerprints, link_duplicates
from .enrich import enrich_listing, recount_agencies, upsert_agency_record
from .market import recompute as recompute_market
from .normalize import normalize

log = logging.getLogger("metruj.runner")

#: Po ilu dniach bez zobaczenia oferty uznajemy ją za zdjętą.
#: Liczymy w dobach, bo tylko pełne przejście wyników widzi CAŁY zasób —
#: a ono chodzi raz na dobę.
DAYS_MISSING_BEFORE_REMOVAL = 3

#: Co tyle ofert zamykamy transakcję. Przy pełnym przejściu jedno źródło
#: potrafi dać kilka tysięcy pozycji — jedna transakcja na całość blokowałaby
#: bazę na kilkanaście minut i każdy inny zapis kończyłby się błędem.
COMMIT_EVERY = 100


@dataclass
class SourceResult:
    source_key: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    price_changes: int = 0
    removed: int = 0
    errors: int = 0
    ok: bool = True
    message: str = ""


@dataclass
class ScanResult:
    sources: list[SourceResult] = field(default_factory=list)
    new_listing_ids: list[int] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "fetched": sum(s.fetched for s in self.sources),
            "new": sum(s.new for s in self.sources),
            "updated": sum(s.updated for s in self.sources),
            "duplicates": sum(s.duplicates for s in self.sources),
            "price_changes": sum(s.price_changes for s in self.sources),
            "removed": sum(s.removed for s in self.sources),
            "errors": sum(s.errors for s in self.sources),
        }


# --------------------------------------------------------------------------- #
# Synchronizacja rejestru źródeł
# --------------------------------------------------------------------------- #
def sync_sources(session: Session) -> int:
    """Wczytuje config/sources.yaml do tabeli `sources` (idempotentnie)."""
    cfg = sources_config()
    defaults = cfg.get("defaults", {})
    count = 0
    for entry in cfg.get("sources", []):
        key = entry.get("key")
        if not key:
            continue
        source = session.scalar(select(Source).where(Source.key == key))
        if source is None:
            source = Source(key=key)
            session.add(source)
            count += 1
        source.name = entry.get("name", key)
        source.kind = OfferKind(entry.get("kind", "nieruchomosc"))
        source.category = entry.get("category", "portal")
        source.base_url = entry.get("base_url")
        source.scraper = entry.get("scraper", "generic_html")
        source.coverage = entry.get("coverage", "krajowy")
        source.interval_minutes = int(
            entry.get("interval_minutes", defaults.get("interval_minutes", 15))
        )
        source.notes = entry.get("notes")
        source.config = entry.get("config", {}) or {}
        # config/sources.yaml rozstrzyga, czy źródło jest włączone. Wcześniej
        # ustawienie z pliku obowiązywało tylko do pierwszego uruchomienia
        # źródła — miało to chronić ręczne przełączenia w bazie, ale poza tą
        # funkcją nikt tego pola nie zapisuje. Efekt był taki, że raz
        # uruchomionego źródła nie dało się już wyłączyć przez plik.
        source.enabled = bool(entry.get("enabled", False))
    return count


def _build_scraper(source: Source, client: HttpClient):
    """Tworzy scraper dla źródła.

    Scrapery uniwersalne (generic_html, sitemap) obsługują wiele różnych
    witryn, więc muszą dostać klucz źródła — inaczej wszystkie strony biur
    zapisywałyby się pod jednym wspólnym kluczem i zlewały w jedno źródło.
    """
    import inspect

    cls = SCRAPERS.get(source.scraper or "")
    if cls is None:
        cls = GenericHtmlScraper

    if "source_key" in inspect.signature(cls.__init__).parameters:
        return cls(
            client, source.config, source_key=source.key, kind=source.kind, name=source.name
        )
    return cls(client, source.config)


# --------------------------------------------------------------------------- #
# Zapis pojedynczej oferty
# --------------------------------------------------------------------------- #
def _sync_phones(session: Session, listing: Listing, phones: list[PhoneNumber]) -> None:
    settings = get_settings()
    existing = {p.hashed for p in listing.phones}
    for phone in phones:
        if phone.hashed in existing:
            continue
        listing.phones.append(
            Phone(
                e164="" if settings.store_phone_hash_only else phone.e164,
                national="" if settings.store_phone_hash_only else phone.national,
                masked=phone.masked,
                hashed=phone.hashed,
                origin=phone.origin,
            )
        )


def upsert_listing(
    session: Session, data: dict, phones: list[PhoneNumber], source: Source,
    cleared: tuple[str, ...] = (),
) -> tuple[Listing, str, bool]:
    """Wstawia albo aktualizuje ofertę.

    Zwraca (oferta, 'new'|'updated'|'unchanged', czy_zmiana_ceny).
    """
    compute_fingerprints(data, phones)
    enrich_listing(session, data, phones)

    listing = session.scalar(
        select(Listing).where(
            Listing.source_key == data["source_key"],
            Listing.external_id == data["external_id"],
        )
    )
    price_changed = False

    if listing is None:
        listing = Listing(**data, source_id=source.id, initial_price=data.get("price"))
        session.add(listing)
        session.flush()
        _sync_phones(session, listing, phones)
        if data.get("price"):
            session.add(PriceHistory(listing_id=listing.id, price=data["price"]))
        return listing, "new", False

    changed = False
    old_price = listing.price
    old_address = (listing.city, listing.street, listing.district, listing.voivodeship)
    for key, value in data.items():
        if key in {"raw", "extra", "first_seen_at"}:
            continue
        # Pustej wartości nie wpisujemy w miejsce istniejącej: portal potrafi
        # raz nie oddać pola, a to nie znaczy, że dane zniknęły. Wyjątkiem są
        # pola, które źródło świadomie wyczyściło — wtedy scraper podaje je
        # w `data["wyczyszczone"]`. Bez tego poprawka typu „ta ulica to adres
        # urzędu, nie nieruchomości" nie miała jak dojść do bazy.
        if value in (None, "", [], {}) and key not in cleared:
            continue
        if getattr(listing, key, None) != value:
            setattr(listing, key, value)
            changed = True

    # Zmiana adresu unieważnia punkt na mapie. Bez tego poprawiona lokalizacja
    # zostawała ze współrzędnymi sprzed poprawki: oferta z Opola stała na mapie
    # tam, gdzie kiedyś rozpoznano ją jako Kamienicę, a jedna pinezka zbierała
    # oferty z kilku różnych miejscowości.
    if (listing.city, listing.street, listing.district, listing.voivodeship) != old_address:
        if listing.geo_precision != "portal" or data.get("lat"):
            listing.lat = data.get("lat")
            listing.lon = data.get("lon")
            listing.geo_precision = data.get("geo_precision")
            listing.geo_source = "portal" if data.get("lat") else None

    if data.get("price") and old_price and abs(data["price"] - old_price) > 0.5:
        session.add(
            PriceHistory(listing_id=listing.id, price=data["price"], previous_price=old_price)
        )
        price_changed = True
    if data.get("raw"):
        listing.raw = data["raw"]
    if data.get("extra"):
        listing.extra = {**(listing.extra or {}), **data["extra"]}

    listing.last_seen_at = utcnow()
    listing.status = ListingStatus.AKTYWNA
    listing.removed_at = None
    _sync_phones(session, listing, phones)
    return listing, ("updated" if changed else "unchanged"), price_changed


# --------------------------------------------------------------------------- #
# Przebieg jednego źródła
# --------------------------------------------------------------------------- #
#: komunikat dla źródeł, których wyniki powstają dopiero w przeglądarce
JS_REQUIRED_MESSAGE = (
    "źródło renderuje wyniki po stronie klienta — potrzebny silnik JS "
    "(patrz README: Źródła wymagające przeglądarki)"
)


def _source_context(source: Source, ctx: ScrapeContext) -> ScrapeContext:
    """Kontekst z limitami danego źródła.

    Wspólny limit „400 ofert na źródło" ma sens przy BIP-ie gminy i jest
    bez sensu przy OLX-ie, który dzieli zasób na 16 województw razy 10
    kategorii — przy takim limicie skan nie docierał poza pierwszy region.
    Dlatego źródło może podnieść swój limit w `config/sources.yaml`.
    """
    if ctx.limits_explicit:
        return ctx
    config = source.config or {}
    prefix = "deep_" if ctx.deep else ""
    pages = config.get(f"{prefix}max_pages", config.get("max_pages"))
    items = config.get(f"{prefix}max_items", config.get("max_items"))
    if pages is None and items is None:
        return ctx
    return replace(
        ctx,
        max_pages=int(pages) if pages is not None else ctx.max_pages,
        max_items=int(items) if items is not None else ctx.max_items,
    )


#: Co tyle pozycji zebrane oferty idą do bazy.
#:
#: Wcześniej źródło trzymało w pamięci **wszystko**, co zebrało, i zapisywało
#: dopiero na końcu. Przy jednym województwie było to kilka tysięcy obiektów.
#: Przy pełnym przejściu przez kraj Otodom to kilkaset tysięcy ofert, każda
#: z kompletem danych z portalu — nocny przebieg zajmował całą pamięć serwera
#: i system go zabijał (`oom-kill`), zanim OLX, Otodom i Morizon cokolwiek
#: zapisały. Teraz pamięć trzyma najwyżej jedną paczkę na źródło, a przerwany
#: przebieg zostawia po sobie wszystko, co zdążył zebrać.
PERSIST_EVERY = 500


def _persist_batch(source_key: str, items: list[RawListing], scope: list[str] | None,
                   result: SourceResult, new_ids: list[int]) -> None:
    """Zapisuje jedną paczkę pozycji źródła. Liczniki dopisuje do `result`.

    Wywoływane w osobnym wątku, pod wspólną blokadą zapisu — SQLite i tak
    przyjmuje jeden zapis naraz, a tak unikamy błędów „database is locked"
    przy kilku źródłach kończących paczkę w tej samej chwili.
    """
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.key == source_key))
        if source is None:
            result.ok = False
            result.message = "brak źródła w rejestrze"
            return

        # Katalog biur nie produkuje ogłoszeń — zasila rejestr pośredników.
        if items and (items[0].extra or {}).get("katalog"):
            for raw in items:
                try:
                    if upsert_agency_record(session, raw):
                        result.new += 1
                    else:
                        result.updated += 1
                except Exception as exc:
                    session.rollback()
                    result.errors += 1
                    if not result.message:
                        result.message = f"{type(exc).__name__}: {exc}"[:400]
            return

        processed = 0
        for raw in items:
            try:
                normalized = normalize(raw, scope=scope)
            except Exception as exc:
                log.debug("Normalizacja odrzuciła ofertę %s: %s", raw.url, exc)
                result.errors += 1
                if not result.message:
                    result.message = f"normalizacja — {type(exc).__name__}: {exc}"[:400]
                continue
            if normalized is None:
                continue
            try:
                listing, action, price_changed = upsert_listing(
                    session, normalized.data, normalized.phones, source, normalized.cleared
                )
            except Exception as exc:
                session.rollback()
                log.debug("Zapis oferty %s nie powiódł się: %s", raw.url, exc)
                result.errors += 1
                # Licznik błędów bez powodu jest bezużyteczny: „285 błędów"
                # w tabeli przebiegu nie mówi, czy portal przemeblował front,
                # czy baza była zajęta. Pierwszy powód zapamiętujemy i pokazujemy.
                if not result.message:
                    result.message = f"{type(exc).__name__}: {exc}"[:400]
                continue

            if action == "new":
                result.new += 1
                new_ids.append(listing.id)
                result.duplicates += link_duplicates(session, listing)
            elif action == "updated":
                result.updated += 1
            if price_changed:
                result.price_changes += 1

            processed += 1
            if processed % COMMIT_EVERY == 0:
                # oddajemy blokadę zapisu, żeby inne zadania mogły się wcisnąć
                session.commit()


def _finish_source(source_key: str, result: SourceResult, error: str,
                   complete_pass: bool, started_at) -> None:
    """Zamyka przebieg źródła: wpis w dzienniku, wygaszanie, liczniki."""
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.key == source_key))
        if source is None:
            return

        # Oferty wygaszamy WYŁĄCZNIE po pełnym i bezbłędnym przejściu wyników.
        # Zwykły skan bierze tylko najnowsze strony, więc z definicji nie widzi
        # starszych ofert — uznawanie ich wtedy za zdjęte kasowało z widoku
        # prawie całą bazę w kilkanaście minut od jej zebrania.
        if not error and result.fetched and complete_pass:
            result.removed = _mark_missing(session, source)

        source.last_run_at = utcnow()
        source.total_listings = int(
            session.scalar(select(func.count(Listing.id)).where(Listing.source_key == source_key))
            or 0
        )
        if error:
            source.last_error = error[:1000]
        else:
            source.last_ok_at = utcnow()
            source.last_error = None

        session.add(
            ScanRun(
                source_key=source_key,
                started_at=started_at,
                finished_at=utcnow(),
                fetched=result.fetched,
                new=result.new,
                updated=result.updated,
                duplicates=result.duplicates,
                errors=result.errors,
                ok=result.ok,
                message=(result.message or "")[:1000] or None,
            )
        )


async def _collect_and_persist(
    source: Source, client: HttpClient, ctx: ScrapeContext,
    scope: list[str] | None, deep: bool, write_lock: asyncio.Lock,
) -> tuple[SourceResult, list[int]]:
    """Zbiera jedno źródło i zapisuje je paczkami w trakcie zbierania."""
    result = SourceResult(source_key=source.key)
    new_ids: list[int] = []
    started_at = utcnow()

    if (source.config or {}).get("requires_js"):
        # lepiej powiedzieć wprost, że się nie da, niż zwrócić ciche zero
        result.ok = False
        result.message = JS_REQUIRED_MESSAGE
        async with write_lock:
            await asyncio.to_thread(
                _finish_source, source.key, result, JS_REQUIRED_MESSAGE, False, started_at
            )
        return result, new_ids

    ctx = _source_context(source, ctx)
    scraper = _build_scraper(source, client)
    buffer: list[RawListing] = []
    error = ""

    async def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        batch, buffer = buffer, []
        async with write_lock:
            await asyncio.to_thread(_persist_batch, source.key, batch, scope, result, new_ids)

    try:
        async for raw in scraper.run(ctx):
            raw.source_key = raw.source_key or source.key
            buffer.append(raw)
            result.fetched += 1
            if len(buffer) >= PERSIST_EVERY:
                await flush()
    except Exception as exc:  # scraper nie może wywrócić całego skanu
        error = f"{type(exc).__name__}: {exc}"
        log.warning("Źródło %s zakończyło się błędem: %s", source.key, error)
    # To, co zebrano przed błędem, i tak trafia do bazy.
    await flush()

    if error:
        result.ok = False
        result.message = result.message or error
    async with write_lock:
        await asyncio.to_thread(_finish_source, source.key, result, error, deep, started_at)
    return result, new_ids


def _mark_missing(session: Session, source: Source) -> int:
    """Wygasza oferty, których od kilku dni nie ma już w źródle.

    Wywoływane tylko po PEŁNYM przejściu wyników — tylko ono widzi cały zasób
    źródła. Dodatkowo dajemy kilka dni zapasu, żeby jeden nieudany przebieg
    albo chwilowa awaria portalu nie wygasiły ofert, które istnieją.
    """
    stale_before = utcnow() - timedelta(days=DAYS_MISSING_BEFORE_REMOVAL)
    stale = session.scalars(
        select(Listing)
        .where(
            Listing.source_key == source.key,
            Listing.status == ListingStatus.AKTYWNA,
            Listing.last_seen_at < stale_before,
        )
        .limit(1000)
    )
    count = 0
    for listing in stale:
        listing.status = ListingStatus.NIEAKTYWNA
        listing.removed_at = listing.removed_at or utcnow()
        count += 1
    return count


# --------------------------------------------------------------------------- #
# API publiczne
# --------------------------------------------------------------------------- #
async def run_scan(
    *,
    only: list[str] | None = None,
    categories: list[str] | None = None,
    max_pages: int | None = None,
    max_items: int | None = None,
    scope: list[str] | None = None,
    fetch_details: bool = True,
    include_disabled: bool = False,
    deep: bool = False,
) -> ScanResult:
    """Uruchamia skan wybranych źródeł równolegle.

    `scope` zawęża skan do wybranych województw; pusta lista (domyślnie) znaczy
    **cała Polska**. `deep=True` przechodzi wyniki do końca zamiast brać tylko
    pierwsze strony: zwykły przebieg ma łapać nowości w kilka minut, głęboki —
    zebrać komplet, i dlatego puszcza się go raz na dobę, a nie co kwadrans.
    """
    settings = get_settings()
    scope = scope if scope is not None else settings.scope

    with session_scope() as session:
        sync_sources(session)

    with session_scope() as session:
        stmt = select(Source)
        if not include_disabled:
            stmt = stmt.where(Source.enabled.is_(True))
        if only:
            stmt = stmt.where(Source.key.in_(only))
        if categories:
            stmt = stmt.where(Source.category.in_(categories))
        sources = list(session.scalars(stmt))
        session.expunge_all()

    if not sources:
        return ScanResult()

    defaults = sources_config().get("defaults", {})
    ctx = ScrapeContext(
        voivodeships=scope,
        max_pages=max_pages or int(defaults.get("deep_max_pages" if deep else "max_pages", 60 if deep else 5)),
        max_items=max_items or int(defaults.get("deep_max_items" if deep else "max_items", 20000 if deep else 400)),
        fetch_details=fetch_details,
        deep=deep,
        limits_explicit=bool(max_pages or max_items),
    )

    # Każde źródło zbiera i zapisuje równolegle z innymi, paczkami — pamięć
    # trzyma najwyżej jedną paczkę na źródło, a zapisy idą po kolei pod wspólną
    # blokadą, bo SQLite i tak przyjmuje jeden zapis naraz.
    result = ScanResult()
    write_lock = asyncio.Lock()
    async with HttpClient() as client:
        tasks = [
            asyncio.create_task(
                _collect_and_persist(source, client, ctx, scope, deep, write_lock)
            )
            for source in sources
        ]
        for finished in asyncio.as_completed(tasks):
            source_result, new_ids = await finished
            result.sources.append(source_result)
            result.new_listing_ids.extend(new_ids)
            log.info(
                "Źródło %s: pobrane %s, nowe %s, błędy %s%s",
                source_result.source_key, source_result.fetched, source_result.new,
                source_result.errors,
                f" ({source_result.message})" if source_result.message else "",
            )

    with session_scope() as session:
        recount_agencies(session)

    # Odniesienie rynkowe liczymy po skanie, a nie przy wyświetlaniu strony:
    # mediana ceny za metr dla miasta zmienia się raz na przebieg, a lista
    # okazji ma się otwierać natychmiast.
    with session_scope() as session:
        recompute_market(session)

    return result

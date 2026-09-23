"""Wspólna baza scraperów.

Scraper ma jedno zadanie: zwrócić strumień `RawListing` — surowych, jeszcze
nieznormalizowanych ogłoszeń. Całą resztę (normalizacja, geokodowanie, wykrycie
pośrednika, deduplikacja, historia cen, alerty) robi pipeline. Dzięki temu
dopisanie kolejnego portalu to zwykle 40–80 linii kodu.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from selectolax.parser import HTMLParser

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.http import HttpClient
from ..utils.text import clean


@dataclass
class ScrapeContext:
    """Parametry jednego przebiegu.

    Domyślnie skan obejmuje **całą Polskę**: `voivodeships` puste znaczy „bez
    zawężania". Scrapery, które muszą odpytać portal region po regionie, biorą
    listę z `regions()`; te, które potrafią odpytać kraj naraz (Otodom, BIP-y),
    robią jedno zapytanie.
    """

    voivodeships: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    max_pages: int = 5
    max_items: int = 400
    since: datetime | None = None
    fetch_details: bool = True
    categories: list[str] = field(default_factory=list)
    #: tryb głęboki — przechodzimy wyniki do końca, a nie tylko pierwsze strony.
    #: Zwykły skan ma łapać nowości szybko; ten ma zebrać *wszystko*.
    deep: bool = False
    #: Czy limity podał człowiek (`--limit`, `--pages`). Wtedy mają pierwszeństwo
    #: przed tym, co źródło ustawia w konfiguracji — kto wpisuje limit w komendzie,
    #: chce zobaczyć próbkę, a nie osiem tysięcy ofert.
    limits_explicit: bool = False

    @property
    def regions(self) -> list[dict]:
        """Wpisy regionów z config/regions.yaml objęte tym przebiegiem."""
        from ..settings import region, regions

        if not self.voivodeships:
            return regions()
        found = [region(name) for name in self.voivodeships]
        return [entry for entry in found if entry]

    @property
    def voivodeship(self) -> str:
        """Zgodność wstecz: pierwsze województwo albo pusty łańcuch."""
        return self.voivodeships[0] if self.voivodeships else ""

    def expand(self, template: str, field: str = "klucz") -> list[str]:
        """Rozwija `{region}` w adresie na wszystkie województwa przebiegu.

        Portale, które dzielą wyniki po województwach, mają w ścieżce jego
        nazwę. Bez rozwinięcia scraper odpytywałby dosłowny adres
        `/mieszkania/{region}/` i dostawał 404 — a w tabeli przebiegów
        wyglądałoby to jak „źródło nic nie oddało".
        """
        if "{region}" not in template:
            return [template]
        return [
            template.replace("{region}", str(entry.get(field) or entry.get("klucz") or ""))
            for entry in self.regions
        ]


@dataclass
class RawListing:
    """Surowe ogłoszenie zwracane przez scraper."""

    external_id: str
    url: str
    title: str
    source_key: str = ""
    kind: OfferKind = OfferKind.NIERUCHOMOSC
    transaction: TransactionType = TransactionType.SPRZEDAZ
    property_type: PropertyType = PropertyType.INNE
    description: str | None = None
    price: float | None = None
    currency: str = "PLN"
    area: float | None = None
    plot_area: float | None = None
    rooms: int | None = None
    floor: int | None = None
    floors_total: int | None = None
    year_built: int | None = None
    building_type: str | None = None
    market: str | None = None
    location_text: str | None = None
    city: str | None = None
    district: str | None = None
    street: str | None = None
    commune: str | None = None
    county: str | None = None
    #: Województwo podane przez portal w osobnym polu (OLX: `location.region`,
    #: Otodom: `location.address.province`). To najtańszy i najpewniejszy sposób
    #: rozróżnienia dwóch miejscowości o tej samej nazwie — a takich w Polsce
    #: są setki.
    voivodeship: str | None = None
    lat: float | None = None
    lon: float | None = None
    seller_type: SellerType = SellerType.NIEZNANY
    seller_name: str | None = None
    contact_email: str | None = None
    phones_raw: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    # licytacje / przetargi
    event_date: datetime | None = None
    deadline: datetime | None = None
    opening_price: float | None = None
    estimate_value: float | None = None
    deposit: float | None = None
    case_number: str | None = None
    authority: str | None = None
    share: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    #: Źródło było już odpytane z filtrem regionu (np. adres kategorii zawiera
    #: województwo). Wtedy brak rozpoznanego miasta nie jest powodem do
    #: odrzucenia oferty — portal sam zagwarantował, że jest z tego regionu.
    region_assured: bool = False
    #: Czy wolno szukać ulicy w treści ogłoszenia. Na stronach urzędowych
    #: treść zaczyna się od adresu urzędu — dla ogłoszenia z BIP-u Kluczborka
    #: dawało to „ul. Katowicka", czyli siedzibę ratusza, a nie działkę
    #: wystawioną na sprzedaż. Takie źródła ustawiają tu False.
    street_from_body: bool = True


class BaseScraper:
    """Interfejs scrapera. Podklasy nadpisują `run()`."""

    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    kind: ClassVar[OfferKind] = OfferKind.NIERUCHOMOSC
    base_url: ClassVar[str] = ""
    coverage: ClassVar[str] = "krajowy"
    #: czy źródło wymaga logowania / ręcznej konfiguracji (nie uruchamiamy automatem)
    requires_credentials: ClassVar[bool] = False

    async def crawl_sections(
        self,
        sections: list,
        page_items,
        *,
        max_pages: int,
        empty_pages_before_stop: int = 1,
    ) -> AsyncIterator[RawListing]:
        """Przechodzi sekcje **wszerz**: strona 1 każdej, potem strona 2 każdej.

        Sekcja to zwykle jedno województwo albo jedna kategoria. Przy
        przechodzeniu sekcja po sekcji (najpierw całe Dolnośląskie do ostatniej
        strony, potem Kujawsko-Pomorskie…) nocne przejście ucięte limitem czasu
        zostawiało końcówkę listy bez jednej oferty — a trwa ono kilkanaście
        godzin i rzadko kiedy zdąży. Wszerz każde województwo ma zebrane
        pierwsze strony, a brakuje najwyżej najgłębszych.

        `page_items(section, page)` oddaje listę ofert z jednej strony sekcji
        albo `None`, gdy sekcja się skończyła. Puste strony pod rząd kończą
        sekcję, a powtórzone oferty (portal oddaje tę samą stronę) nie liczą
        się jako świeże.
        """
        seen: set[str] = set()
        streak = dict.fromkeys(range(len(sections)), 0)
        for page in range(1, max_pages + 1):
            active = [i for i, empty in streak.items() if empty < empty_pages_before_stop]
            if not active:
                return
            for index in active:
                try:
                    items = await page_items(sections[index], page)
                except Exception:  # jedna strona nie może wywrócić przebiegu
                    streak[index] = empty_pages_before_stop
                    continue
                if items is None:
                    streak[index] = empty_pages_before_stop
                    continue
                fresh = 0
                for item in items:
                    if item is None or item.external_id in seen:
                        continue
                    seen.add(item.external_id)
                    fresh += 1
                    yield item
                streak[index] = 0 if fresh else streak[index] + 1

    def __init__(self, client: HttpClient, config: dict | None = None) -> None:
        self.client = client
        self.config = config or {}

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:  # pragma: no cover
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    # --------------------------- narzędzia --------------------------- #
    async def html(self, url: str, **kw) -> HTMLParser:
        return HTMLParser(await self.client.get_text(url, **kw))

    @staticmethod
    def text(node, selector: str, default: str = "") -> str:
        if node is None:
            return default
        found = node.css_first(selector)
        return clean(found.text()) if found else default

    @staticmethod
    def attr(node, selector: str, name: str, default: str | None = None) -> str | None:
        if node is None:
            return default
        found = node.css_first(selector)
        return (found.attributes.get(name) or default) if found else default

    @staticmethod
    def next_data(tree: HTMLParser) -> dict:
        """Wyciąga `__NEXT_DATA__` (Otodom, Gratka i inne aplikacje Next.js)."""
        node = tree.css_first("script#__NEXT_DATA__")
        if not node:
            return {}
        try:
            return json.loads(node.text())
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def json_ld(tree: HTMLParser) -> list[dict]:
        """Zwraca wszystkie bloki schema.org JSON-LD ze strony."""
        out: list[dict] = []
        for node in tree.css('script[type="application/ld+json"]'):
            try:
                payload = json.loads(node.text())
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, list):
                out.extend(p for p in payload if isinstance(p, dict))
            elif isinstance(payload, dict):
                out.append(payload)
                out.extend(g for g in payload.get("@graph", []) if isinstance(g, dict))
        return out

    @staticmethod
    def nuxt_state(html: str) -> dict:
        """Wyciąga `window.__NUXT__` (część portali lokalnych)."""
        m = re.search(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", html, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def dig(data: Any, *path: str | int, default: Any = None) -> Any:
        """Bezpieczne wejście w zagnieżdżony JSON: dig(d, 'a', 0, 'b')."""
        cur = data
        for step in path:
            try:
                cur = cur[step]
            except (KeyError, IndexError, TypeError):
                return default
        return cur if cur is not None else default

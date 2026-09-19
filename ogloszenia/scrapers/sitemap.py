"""Uniwersalny scraper stron biur nieruchomości i mniejszych portali.

Skąd bierze się liczba „300 portali": to w przeważającej części **strony
własne biur nieruchomości**, a nie serwisy ogłoszeniowe. Serwisów z prawdziwego
zdarzenia jest w Polsce kilkanaście; biur z własną stroną — tysiące.

Utrzymywanie selektorów dla tysiąca stron jest niewykonalne. Ale te strony mają
dwie wspólne cechy, które wystarczą:

1. **sitemap.xml** — prawie każdy CMS ją generuje, a znajdziemy ją przez
   `robots.txt` albo pod standardowymi ścieżkami,
2. **dane strukturalne** — JSON-LD `schema.org`, microdata albo przynajmniej
   OpenGraph. Wtyczki SEO wstawiają je automatycznie, więc siedzą tam nawet
   na stronach, których nikt świadomie pod to nie przygotował.

Dzięki temu jeden scraper obsługuje dowolną liczbę witryn bez pisania kodu
per strona — wystarczy adres. Gdzie danych strukturalnych nie ma, schodzimy
na nagłówki i treść strony i wyciągamy, co się da (cena, metraż, pokoje).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..models import OfferKind, SellerType
from ..utils.text import (
    clean,
    extract_area,
    extract_floor,
    extract_rooms,
    parse_datetime,
    parse_number,
    sha1,
)
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type, guess_transaction

#: gdzie szukać mapy strony, gdy robots.txt jej nie wskazuje
SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
    "/sitemap/sitemap-index.xml",
    "/sitemapindex.xml",
]

#: po tych fragmentach adresu poznajemy stronę pojedynczej oferty
OFFER_URL_HINTS = re.compile(
    r"/(?:oferta|oferty|nieruchomosc|nieruchomosci|ogloszenie|ogloszenia|listing|"
    r"listings|property|properties|obiekt|mieszkanie|mieszkania|dom|domy|dzialka|"
    r"dzialki|lokal|lokale|apartament)[/-]",
    re.I,
)

#: adresy, które na pewno nie są ofertą
OFFER_URL_SKIP = re.compile(
    r"/(?:kategoria|category|tag|autor|author|strona|page|blog|aktualnosci|news|"
    r"kontakt|contact|o-nas|about|polityka|regulamin|cookie|feed|wp-content|"
    r"wp-json|szukaj|search|logowanie|login)(?:/|$)",
    re.I,
)

#: Strona POJEDYNCZEJ oferty, w odróżnieniu od listy wyników. Rozpoznajemy ją po
#: końcówce adresu: identyfikator liczbowy (`…-16141.html`) albo długi, opisowy
#: slug (`…/mieszkanie-opole-centrum-3-pokoje`). Bez tego scraper wciągał strony
#: kategorii — „Mieszkania na sprzedaż Opole" to nie jest oferta.
OFFER_DETAIL_URL = re.compile(
    r"[-_/]\d{4,}(?:\.html?|/)?$"
    r"|/[^/]*?[a-z0-9](?:-[a-z0-9]+){3,}(?:\.html?|/)?$",
    re.I,
)

#: składnia filtrów w adresie (`region:opole,pokoje:1-1`) = strona wyników
FILTER_SYNTAX = re.compile(r"[:,]|[?&]")

#: tytuły, które zdradzają stronę listy, a nie oferty
LIST_PAGE_TITLE = re.compile(
    r"^(?:wyniki|oferty|mieszkania|domy|dzia[łl]ki|lokale|nieruchomo[śs]ci)\b"
    r"|wyszukiwania|na sprzeda[żz]\s*$|do wynaj",
    re.I,
)

#: Powyżej tylu różnych cen na stronie mamy do czynienia z listą wyników.
#: Próg jest wysoki, bo strona pojedynczej oferty często ma w boku „podobne
#: oferty" z własnymi cenami — zbyt ostry filtr wycinałby prawdziwe ogłoszenia.
#: Sprawdzenie stosujemy wyłącznie tam, gdzie brak danych strukturalnych;
#: jeśli JSON-LD podaje cenę, to jest wiarygodniejsze niż liczenie kwot w tekście.
MAX_DISTINCT_PRICES = 12

#: elementy, które trzeba wyciąć przed czytaniem treści — inaczej opisem
#: oferty staje się menu witryny, a wtedy lokalizacja bierze się z listy miast
#: w nawigacji ("Opole Krapkowice Brzeg") zamiast z samej oferty
CHROME_SELECTORS = (
    "nav", "header", "footer", "aside", "script", "style", "noscript", "form",
    "[class*=menu]", "[class*=nav]", "[class*=breadcrumb]", "[class*=footer]",
    "[class*=header]", "[class*=sidebar]", "[id*=menu]", "[id*=nav]",
    "[class*=cookie]", "[class*=popup]", "[class*=modal]", "[class*=similar]",
    "[class*=podobne]", "[class*=related]", "[class*=polecane]",
)

#: Elementy, w których strony trzymają właściwy opis oferty. Bez tego opisem
#: stawał się blok parametrów („Powierzchnia 220 m² Cena za metr 1 341 zł…"),
#: czyli tabelka, a nie opis.
DESCRIPTION_SELECTORS = (
    "[class*=description]", "[id*=description]", "[class*=opis]", "[id*=opis]",
    "[class*=tresc]", "[itemprop=description]", ".offer-description", ".property-description",
)

#: rozszerzenia plików graficznych w linkach galerii
IMAGE_LINK = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.I)

#: obrazki, które nie są zdjęciem nieruchomości
NON_PHOTO = re.compile(
    r"logo|baner|banner|nagrod|award|laureat|konkurs|ikon|icon|sprite|avatar|"
    r"placeholder|pixel|tracking|facebook|instagram|youtube|certyfikat|plebiscyt|"
    r"aside|social|\.svg\b|/tr\?|badge|emblem|stopka|naglowek|header|footer|"
    r"/agenci/|agent_|pracownik|zespol|team|avatar",
    re.I,
)

SCHEMA_TYPES = {
    "product", "offer", "residence", "apartment", "house", "singlefamilyresidence",
    "realestatelisting", "accommodation", "place",
}


@dataclass(slots=True)
class SitemapEntry:
    url: str
    lastmod: str | None = None


class SitemapScraper(BaseScraper):
    """Czyta dowolną witrynę z mapą strony i danymi strukturalnymi.

    Konfiguracja (w `config/sources.yaml`)::

        base_url: https://przykladowe-biuro.pl
        config:
          sitemap: https://…/sitemap.xml   # opcjonalnie, gdy autowykrywanie zawiedzie
          offer_pattern: "/oferta/"        # opcjonalnie, gdy adresy są nietypowe
          max_offers: 300
    """

    key = "sitemap"
    name = "Strona biura (sitemap + dane strukturalne)"
    kind = OfferKind.NIERUCHOMOSC
    coverage = "lokalny"

    def __init__(self, client, config: dict | None = None, source_key: str | None = None,
                 kind: OfferKind | None = None, name: str | None = None) -> None:
        super().__init__(client, config)
        self.source_key = source_key or self.key
        self._kind = kind or self.kind
        self._name = name or self.name

    # ------------------------------------------------------------------ #
    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        base = (self.config.get("base_url") or self.base_url or "").rstrip("/")
        if not base:
            return

        entries = await self.discover_offers(base, ctx)
        limit = min(ctx.max_items, int(self.config.get("max_offers", 400)))
        produced = 0

        for entry in entries[:limit]:
            try:
                item = await self.parse_offer(entry, base)
            except Exception:
                continue
            if item is None:
                continue
            yield item
            produced += 1
            if produced >= limit:
                return

    # ------------------------------------------------------------------ #
    async def discover_offers(self, base: str, ctx: ScrapeContext) -> list[SitemapEntry]:
        """Znajduje adresy ofert: z podanych stron wyników albo z mapy strony.

        Duże portale generują nazwy klas CSS przy każdym wdrożeniu, więc
        selektory pękają co tydzień. Adresy ofert są za to stabilne — dlatego
        przechodzimy strony wyników wyłącznie po to, by zebrać odnośniki,
        a dane czytamy już ze stron pojedynczych ofert, gdzie prawie zawsze
        są znaczniki schema.org albo OpenGraph.
        """
        listing_urls = self.config.get("listing_urls")
        if listing_urls:
            found: dict[str, SitemapEntry] = {}
            for template in listing_urls:
                pages = ctx.max_pages if "{page}" in template else 1
                for page in range(1, pages + 1):
                    url = template.format(page=page) if "{page}" in template else template
                    for entry in await self._crawl_links(url):
                        found.setdefault(entry.url, entry)
                    if len(found) >= int(self.config.get("max_offers", 400)):
                        break
            if found:
                return list(found.values())

        sitemaps = await self._find_sitemaps(base)
        entries: dict[str, SitemapEntry] = {}

        for sitemap_url in sitemaps[:12]:
            for entry in await self._read_sitemap(sitemap_url, depth=0):
                if self._looks_like_offer(entry.url):
                    entries.setdefault(entry.url, entry)
            if len(entries) >= int(self.config.get("max_offers", 400)):
                break

        if not entries:
            # brak mapy strony — próbujemy wyłuskać oferty z linków strony głównej
            entries = {e.url: e for e in await self._crawl_links(base)}

        # najświeższe najpierw, o ile sitemap podała daty
        return sorted(
            entries.values(),
            key=lambda e: (e.lastmod or ""),
            reverse=True,
        )

    async def _find_sitemaps(self, base: str) -> list[str]:
        configured = self.config.get("sitemap")
        if configured:
            return [configured] if isinstance(configured, str) else list(configured)

        found: list[str] = []
        try:
            robots = await self.client.get_text(urljoin(base + "/", "robots.txt"), retries=0)
            found += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
        except Exception:
            pass
        for path in SITEMAP_PATHS:
            candidate = base + path
            if candidate not in found:
                found.append(candidate)
        return found

    async def _read_sitemap(self, url: str, depth: int) -> list[SitemapEntry]:
        """Czyta mapę strony; indeksy map rozwija rekurencyjnie (płytko)."""
        if depth > 2:
            return []
        try:
            xml = await self.client.get_text(url, retries=0)
        except Exception:
            return []
        if "<" not in xml[:200]:
            return []

        # indeks map strony
        if "<sitemapindex" in xml[:2000].lower():
            children = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml, re.I)
            out: list[SitemapEntry] = []
            for child in children[:20]:
                if re.search(r"(post|page|kategor|tag|author|image|video)", child, re.I):
                    continue  # mapy, w których na pewno nie ma ofert
                out += await self._read_sitemap(child, depth + 1)
                if len(out) > 5000:
                    break
            return out

        entries: list[SitemapEntry] = []
        for block in re.findall(r"<url>(.*?)</url>", xml, re.S | re.I):
            loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block, re.I)
            if not loc:
                continue
            lastmod = re.search(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", block, re.I)
            entries.append(SitemapEntry(url=loc.group(1), lastmod=lastmod.group(1) if lastmod else None))
        return entries

    async def _crawl_links(self, base: str) -> list[SitemapEntry]:
        """Zbiera ze strony wyników odnośniki wyglądające na pojedyncze oferty."""
        try:
            tree = await self.html(base)
        except Exception:
            return []
        host = urlparse(base).netloc
        out: dict[str, SitemapEntry] = {}
        for anchor in tree.css("a"):
            href = anchor.attributes.get("href")
            if not href:
                continue
            full = urljoin(base + "/", href)
            if urlparse(full).netloc != host:
                continue
            if self._looks_like_offer(full):
                out.setdefault(full.split("#")[0], SitemapEntry(url=full.split("#")[0]))
        return list(out.values())

    def _looks_like_offer(self, url: str) -> bool:
        path = urlparse(url).path
        if OFFER_URL_SKIP.search(path) or FILTER_SYNTAX.search(url.split("://", 1)[-1]):
            return False
        pattern = self.config.get("offer_pattern")
        if pattern:
            return bool(re.search(pattern, url, re.I))
        # musi wyglądać i na nieruchomość, i na stronę pojedynczego obiektu
        return bool(OFFER_URL_HINTS.search(path) or OFFER_DETAIL_URL.search(path)) and bool(
            OFFER_DETAIL_URL.search(path)
        )

    # ------------------------------------------------------------------ #
    async def parse_offer(self, entry: SitemapEntry, base: str) -> RawListing | None:
        html = await self.client.get_text(entry.url)
        tree = HTMLParser(html)

        # Do czytania treści potrzebujemy strony BEZ nawigacji i stopki, ale
        # cena i zdjęcia siedzą w nagłówku oferty. Czyszczenie usuwa elementy
        # z drzewa na trwałe, więc pracujemy na dwóch osobnych drzewach:
        # `tree` zostaje nietknięte, `content` jest przycięte.
        data = self._from_structured(tree) or {}
        price_from_page = self._price_from_page(tree, "")
        photos = self._photos(tree, entry.url)
        real_description = self._description_from_page(tree)
        text = self._content_text(HTMLParser(html))

        title = data.get("title") or self.text(tree, "h1") or self.text(tree, "title")
        title = clean(title)
        if not title or len(title) < 6:
            return None
        if LIST_PAGE_TITLE.search(title) and not data.get("price"):
            return None  # „Wyniki wyszukiwania", „Mieszkania na sprzedaż Opole"

        if not data.get("price"):
            # Brak danych strukturalnych — dopiero wtedy liczymy kwoty w treści,
            # żeby odsiać strony wyników udające ofertę.
            distinct_prices = {
                round(value)
                for value in (
                    parse_number(m.group(1))
                    for m in re.finditer(r"([\d\s\u00a0.,]{5,15})\s*(?:zł|PLN)", text, re.I)
                )
                if value and 10_000 <= value <= 50_000_000
            }
            if len(distinct_prices) > MAX_DISTINCT_PRICES:
                return None

        description = data.get("description") or real_description or text
        price = data.get("price") or price_from_page or self._price_from_text(text)

        # Parametry czytamy z TYTUŁU i początku opisu, nie z całej strony.
        # Strona biura ma w menu „mieszkania / domy / działki / wynajem", więc
        # klasyfikowanie po treści całej strony robiło z domu mieszkanie,
        # a ze sprzedaży wynajem.
        headline = f"{title} {description[:600]}"
        area = data.get("area") or extract_area(headline)
        rooms = extract_rooms(headline)
        floor, floors_total = extract_floor(headline)

        if price is None and area is None:
            return None  # strona bez ceny i metrażu to prawie na pewno nie oferta

        images = data.get("images") or photos

        return RawListing(
            external_id=self._external_id(entry.url),
            url=entry.url,
            source_key=self.source_key,
            kind=self._kind,
            title=title[:400],
            description=description[:20000] or None,
            price=price,
            area=area,
            rooms=rooms,
            floor=floor,
            floors_total=floors_total,
            city=data.get("city"),
            street=data.get("street"),
            # Lokalizację czytamy z TYTUŁU, nie z treści strony. Strona biura ma
            # w menu listę obsługiwanych miast i bez tego oferta z Opola lądowała
            # w Krapkowicach, bo tak akurat wypadło w nawigacji.
            location_text=data.get("location") or title,
            seller_type=SellerType(self.config["seller_type"])
            if self.config.get("seller_type")
            else SellerType.POSREDNIK,
            seller_name=self.config.get("agency_name"),
            images=[i for i in images if i][:12],
            published_at=parse_datetime(entry.lastmod) if entry.lastmod else None,
            property_type=guess_property_type(title, entry.url),
            transaction=guess_transaction(title, entry.url),
            extra={"strona": urlparse(base).netloc, "dane_strukturalne": bool(data)},
            raw={"sitemap_lastmod": entry.lastmod},
        )

    @staticmethod
    def _content_text(tree: HTMLParser) -> str:
        """Treść oferty bez nawigacji, stopki i bloków „podobne oferty"."""
        for selector in CHROME_SELECTORS:
            try:
                for node in tree.css(selector):
                    node.decompose()
            except Exception:
                continue
        main = tree.css_first("main") or tree.css_first("article") or tree.css_first("body")
        return clean(main.text())[:8000] if main else ""

    @staticmethod
    def _photos(tree: HTMLParser, page_url: str) -> list[str]:
        """Zdjęcia nieruchomości — z galerii, znaczników img i teł CSS.

        Same `<img>` nie wystarczają: galerie często wstawiają miniatury jako
        tło CSS, a pełne zdjęcia trzymają w odnośnikach `<a href="…jpg">`.
        Stąd trzy źródła naraz, z odsiewem logotypów, plakietek i portretów
        agentów — te ostatnie potrafią być jedynymi zdjęciami na stronie.
        """
        candidates: list[str] = []

        # 1. odnośniki galerii — zwykle pełna rozdzielczość
        for anchor in tree.css("a"):
            href = anchor.attributes.get("href") or ""
            if href and IMAGE_LINK.search(href):
                candidates.append(href)

        # 2. znaczniki obrazków
        for img in tree.css("img"):
            src = (
                img.attributes.get("src")
                or img.attributes.get("data-src")
                or img.attributes.get("data-lazy")
                or img.attributes.get("data-original")
                or ""
            )
            if src and not src.startswith("data:"):
                alt = img.attributes.get("alt", "")
                css = img.attributes.get("class", "")
                candidates.append(f"{src}\x00{alt} {css}")

        # 3. tła CSS
        html = tree.html or ""
        candidates += re.findall(r"background-image\s*:\s*url\([\"']?([^\"')]+)", html, re.I)

        out: list[str] = []
        for candidate in candidates:
            src, _, meta = candidate.partition("\x00")
            if NON_PHOTO.search(f"{src} {meta}"):
                continue
            if not IMAGE_LINK.search(src):
                continue
            full = urljoin(page_url, src)
            if full not in out:
                out.append(full)
            if len(out) >= 12:
                break
        return out

    def _description_from_page(self, tree: HTMLParser) -> str:
        """Właściwy opis oferty, a nie tabelka parametrów."""
        for selector in DESCRIPTION_SELECTORS:
            try:
                node = tree.css_first(selector)
            except Exception:
                continue
            if node is None:
                continue
            text = clean(node.text())
            if len(text) >= 120:
                return text[:20000]
        meta = tree.css_first('meta[name="description"]')
        if meta:
            text = clean(meta.attributes.get("content") or "")
            if len(text) >= 60:
                return text
        return ""

    def _from_structured(self, tree: HTMLParser) -> dict:
        """Wyciąga, co się da, z JSON-LD; potem z OpenGraph."""
        out: dict = {}
        for block in self.json_ld(tree):
            btype = block.get("@type")
            types = {btype.lower()} if isinstance(btype, str) else {
                str(t).lower() for t in (btype or [])
            }
            if not types & SCHEMA_TYPES:
                continue
            offers = block.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            floor_size = block.get("floorSize") or {}
            address = block.get("address") or {}

            out.setdefault("title", clean(block.get("name") or ""))
            out.setdefault("description", clean(block.get("description") or ""))
            price = parse_number(offers.get("price") or block.get("price"))
            if price:
                out.setdefault("price", price)
            if isinstance(floor_size, dict):
                area = parse_number(floor_size.get("value"))
                if area:
                    out.setdefault("area", area)
            if isinstance(address, dict):
                out.setdefault("city", clean(address.get("addressLocality") or "") or None)
                out.setdefault("street", clean(address.get("streetAddress") or "") or None)
            images = block.get("image") or []
            if isinstance(images, str):
                images = [images]
            if images:
                out.setdefault("images", [i for i in images if isinstance(i, str)][:12])

        if not out.get("title"):
            og = tree.css_first('meta[property="og:title"]')
            if og:
                out["title"] = clean(og.attributes.get("content", ""))
        if not out.get("description"):
            og = tree.css_first('meta[property="og:description"]')
            if og:
                out["description"] = clean(og.attributes.get("content", ""))
        return {k: v for k, v in out.items() if v}

    #: klasy i identyfikatory, pod którymi strony trzymają cenę oferty
    PRICE_SELECTORS = (
        "[itemprop=price]", "[class*=price]", "[class*=cena]",
        "[id*=price]", "[id*=cena]", ".offer-price", ".property-price",
    )

    def _price_from_page(self, tree: HTMLParser, text: str) -> float | None:
        """Cena oferty, gdy nie ma danych strukturalnych.

        Nie bierzemy największej kwoty ze strony — w bocznej kolumnie bywają
        „podobne oferty" i wtedy kawalerka dostawała cenę rezydencji. Najpierw
        szukamy elementu oznaczonego jako cena, a dopiero potem pierwszej kwoty
        w treści, czyli tej przy nagłówku oferty.
        """
        for selector in self.PRICE_SELECTORS:
            try:
                node = tree.css_first(selector)
            except Exception:
                continue
            if node is None:
                continue
            value = parse_number(clean(node.text()))
            if value and 1000 <= value <= 50_000_000:
                return value

        # `(?<![\w])` jest tu istotne: bez tego z "220m2 295 000 zł" wychodziło
        # 2 295 000, bo dwójka z "m2" doklejała się do kwoty.
        return self._price_from_text(text) if text else None

    @staticmethod
    def _price_from_text(text: str) -> float | None:
        r"""Pierwsza sensowna kwota w treści — czyli ta przy nagłówku oferty.

        `(?<![\w])` jest tu istotne: bez tego z "220m2 295 000 zł" wychodziło
        2 295 000, bo dwójka z "m2" doklejała się do kwoty.
        """
        for match in re.finditer(
            r"(?<![\w])([\d\u00a0 .,]{4,15})\s*(?:zł|PLN)\b", text or "", re.I
        ):
            value = parse_number(match.group(1))
            if value and 1000 <= value <= 50_000_000:
                return value
        return None

    @staticmethod
    def _external_id(url: str) -> str:
        m = re.search(r"(\d{4,})", urlparse(url).path)
        return m.group(1) if m else sha1(url)[:20]

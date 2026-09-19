"""Uniwersalny scraper HTML sterowany konfiguracją.

Używany przez źródła opisane wyłącznie w `config/sources.yaml` (portale lokalne,
BIP-y gmin i starostw, ogłoszenia instytucji) oraz jako klasa bazowa dla
konkretnych portali — te podmieniają tylko budowanie URL-i i mapowanie pól.

Kolejność strategii parsowania (pierwsza, która da wynik, wygrywa):
  1. JSON-LD (`schema.org/RealEstateListing`, `Product`, `Offer`) — najstabilniejsze,
  2. `__NEXT_DATA__` / `window.__NUXT__` — aplikacje SPA,
  3. selektory CSS z konfiguracji — ostatnia linia obrony.

Dzięki temu zmiana szaty graficznej portalu najczęściej nie wymaga zmian w kodzie,
a jedynie poprawienia selektorów w YAML-u.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import (
    clean,
    extract_area,
    extract_case_number,
    extract_rooms,
    parse_datetime,
    parse_number,
    sha1,
)
from .base import BaseScraper, RawListing, ScrapeContext

PROPERTY_HINTS: list[tuple[re.Pattern[str], PropertyType]] = [
    (re.compile(r"\bmieszkan|\blokal mieszkaln|\bapartament", re.I), PropertyType.MIESZKANIE),
    (re.compile(r"\bdom\b|\bdomu\b|\bbliźniak|\bszeregow", re.I), PropertyType.DOM),
    (re.compile(r"\bdziałk|\bgrunt|\bteren inwestycyjn", re.I), PropertyType.DZIALKA),
    (re.compile(r"\bgaraż|\bmiejsce postojow", re.I), PropertyType.GARAZ),
    (re.compile(r"\bhala\b|\bmagazyn", re.I), PropertyType.HALA),
    (re.compile(r"\bbiur[oa]\b", re.I), PropertyType.BIURO),
    (re.compile(r"\bkamienic", re.I), PropertyType.KAMIENICA),
    (re.compile(r"\bgospodarstw|\brolne\b|\bsiedlisk", re.I), PropertyType.GOSPODARSTWO),
    (re.compile(r"\blokal użytkow|\bhandlow|\busługow", re.I), PropertyType.LOKAL),
    (re.compile(r"\bpokój\b|\bpokoju do wynaj", re.I), PropertyType.POKOJ),
]

TRANSACTION_HINTS: list[tuple[re.Pattern[str], TransactionType]] = [
    (re.compile(r"wynaj|do wynaj|najem", re.I), TransactionType.WYNAJEM),
    (re.compile(r"dzierżaw", re.I), TransactionType.DZIERZAWA),
    (re.compile(r"zamian", re.I), TransactionType.ZAMIANA),
    (re.compile(r"sprzeda|na sprzedaż|licytacj|przetarg", re.I), TransactionType.SPRZEDAZ),
]


def guess_property_type(*texts: str | None) -> PropertyType:
    joined = " ".join(t for t in texts if t)
    for rx, ptype in PROPERTY_HINTS:
        if rx.search(joined):
            return ptype
    return PropertyType.INNE


def guess_transaction(*texts: str | None) -> TransactionType:
    joined = " ".join(t for t in texts if t)
    for rx, ttype in TRANSACTION_HINTS:
        if rx.search(joined):
            return ttype
    return TransactionType.SPRZEDAZ


class GenericHtmlScraper(BaseScraper):
    """Scraper sterowany słownikiem `config` ze źródła.

    Oczekiwany kształt konfiguracji (wszystkie pola opcjonalne poza `urls`)::

        urls:            ["https://.../szukaj?page={page}"]
        list_selector:   "article.offer"
        link_selector:   "a"
        title_selector:  "h2"
        price_selector:  ".price"
        area_selector:   ".area"
        location_selector: ".location"
        date_selector:   "time"
        image_selector:  "img"
        detail:          true            # czy wchodzić na kartę oferty
        detail_description_selector: "#opis"
        kind:            "licytacja"
        pagination:      {param: "page", start: 1}
    """

    key = "generic_html"
    name = "Generyczny scraper HTML"
    coverage = "lokalny"

    def __init__(self, client, config: dict | None = None, source_key: str | None = None,
                 kind: OfferKind | None = None, name: str | None = None) -> None:
        super().__init__(client, config)
        self.source_key = source_key or self.key
        self._kind = kind or self.kind
        self._name = name or self.name

    # ------------------------------------------------------------------ #
    def build_urls(self, ctx: ScrapeContext) -> list[str]:
        templates = self.config.get("urls") or ([self.config["url"]] if self.config.get("url") else [])
        urls: list[str] = []
        for template in templates:
            if "{page}" in template:
                start = int(self.dig(self.config, "pagination", "start", default=1))
                urls += [template.format(page=start + i) for i in range(ctx.max_pages)]
            else:
                urls.append(template)
        return urls

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        seen: set[str] = set()
        produced = 0
        for url in self.build_urls(ctx):
            if produced >= ctx.max_items:
                return
            try:
                tree = await self.html(url)
            except Exception:  # pojedyncza strona nie może wywrócić całego przebiegu
                continue
            for item in self.parse_list(tree, url):
                if item.external_id in seen:
                    continue
                seen.add(item.external_id)
                if self.config.get("detail") and ctx.fetch_details:
                    try:
                        await self.enrich_detail(item)
                    except Exception:
                        pass
                yield item
                produced += 1
                if produced >= ctx.max_items:
                    return

    # ------------------------------------------------------------------ #
    def parse_list(self, tree: HTMLParser, page_url: str) -> list[RawListing]:
        items = self._from_json_ld(tree, page_url)
        if items:
            return items
        return self._from_selectors(tree, page_url)

    def _from_json_ld(self, tree: HTMLParser, page_url: str) -> list[RawListing]:
        out: list[RawListing] = []
        for block in self.json_ld(tree):
            elements = []
            if block.get("@type") in {"ItemList", "CollectionPage"}:
                elements = [
                    e.get("item", e)
                    for e in block.get("itemListElement", [])
                    if isinstance(e, dict)
                ]
            elif block.get("@type") in {"Product", "Offer", "RealEstateListing", "Residence",
                                        "SingleFamilyResidence", "Apartment", "House"}:
                elements = [block]
            for element in elements:
                item = self._from_json_ld_element(element, page_url)
                if item:
                    out.append(item)
        return out

    def _from_json_ld_element(self, element: dict, page_url: str) -> RawListing | None:
        if not isinstance(element, dict):
            return None
        url = element.get("url") or element.get("@id") or ""
        title = clean(element.get("name") or element.get("headline") or "")
        if not url or not title:
            return None
        url = urljoin(page_url, url)
        offers = element.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = parse_number(offers.get("price") or element.get("price"))
        area = None
        floor_size = element.get("floorSize") or {}
        if isinstance(floor_size, dict):
            area = parse_number(floor_size.get("value"))
        description = clean(element.get("description") or "")
        address = element.get("address") or {}
        city = clean(address.get("addressLocality") or "") if isinstance(address, dict) else ""
        street = clean(address.get("streetAddress") or "") if isinstance(address, dict) else ""
        images = element.get("image") or []
        if isinstance(images, str):
            images = [images]
        elif isinstance(images, dict):
            images = [images.get("url", "")]

        return RawListing(
            external_id=self._external_id(url),
            url=url,
            source_key=self.source_key,
            kind=self._kind,
            title=title,
            description=description or None,
            price=price,
            area=area or extract_area(f"{title} {description}"),
            rooms=parse_number(element.get("numberOfRooms")) and int(parse_number(element["numberOfRooms"])),
            city=city or None,
            street=street or None,
            images=[urljoin(page_url, i) for i in images if isinstance(i, str) and i][:12],
            property_type=guess_property_type(title, description),
            transaction=guess_transaction(title, description, page_url),
            raw={"jsonld": element},
        )

    def _from_selectors(self, tree: HTMLParser, page_url: str) -> list[RawListing]:
        cfg = self.config
        list_sel = cfg.get("list_selector")
        if not list_sel:
            return []
        out: list[RawListing] = []
        for card in tree.css(list_sel):
            href = self.attr(card, cfg.get("link_selector", "a"), "href")
            if not href:
                continue
            url = urljoin(page_url, href)
            title = self.text(card, cfg.get("title_selector", "h2, h3, a")) or clean(card.text())[:180]
            if not title:
                continue
            body = clean(card.text())
            price_text = self.text(card, cfg["price_selector"]) if cfg.get("price_selector") else body
            location = self.text(card, cfg["location_selector"]) if cfg.get("location_selector") else None
            date_text = self.text(card, cfg["date_selector"]) if cfg.get("date_selector") else None
            image = self.attr(card, cfg.get("image_selector", "img"), "src") or self.attr(
                card, cfg.get("image_selector", "img"), "data-src"
            )
            area_text = self.text(card, cfg["area_selector"]) if cfg.get("area_selector") else body

            out.append(
                RawListing(
                    external_id=self._external_id(url),
                    url=url,
                    source_key=self.source_key,
                    kind=self._kind,
                    title=title,
                    description=None,
                    price=parse_number(price_text),
                    area=extract_area(area_text),
                    rooms=extract_rooms(body),
                    location_text=location or None,
                    images=[urljoin(page_url, image)] if image else [],
                    published_at=parse_datetime(date_text) if date_text else None,
                    property_type=guess_property_type(title, body),
                    transaction=guess_transaction(title, body, page_url),
                    case_number=extract_case_number(body) if self._kind == OfferKind.LICYTACJA else None,
                    seller_type=SellerType(cfg["seller_type"]) if cfg.get("seller_type") else SellerType.NIEZNANY,
                    authority=cfg.get("authority"),
                )
            )
        return out

    async def enrich_detail(self, item: RawListing) -> None:
        """Dociąga opis (i telefony z opisu) ze strony oferty."""
        cfg = self.config
        tree = await self.html(item.url)
        sel = cfg.get("detail_description_selector")
        node = tree.css_first(sel) if sel else (tree.css_first("main") or tree.css_first("body"))
        if node:
            item.description = clean(node.text())[:20000]
        if not item.price:
            item.price = parse_number(item.description or "")
        if not item.images:
            item.images = [
                urljoin(item.url, img.attributes.get("src", ""))
                for img in tree.css("img")
                if img.attributes.get("src")
            ][:8]

    # ------------------------------------------------------------------ #
    def _external_id(self, url: str) -> str:
        m = re.search(r"(\d{5,})", url)
        return m.group(1) if m else sha1(url)[:20]

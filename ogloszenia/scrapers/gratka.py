"""Gratka.pl — Next.js; próbujemy kolejno `__NEXT_DATA__`, JSON-LD i selektory CSS."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..models import OfferKind, SellerType
from ..utils.text import clean, parse_datetime, parse_number
from .base import RawListing, ScrapeContext
from .generic_html import GenericHtmlScraper, guess_property_type, guess_transaction

BASE = "https://gratka.pl"

SECTIONS = [
    "nieruchomosci/mieszkania/opolskie/sprzedaz",
    "nieruchomosci/mieszkania/opolskie/wynajem",
    "nieruchomosci/domy/opolskie/sprzedaz",
    "nieruchomosci/dzialki/opolskie/sprzedaz",
    "nieruchomosci/lokale-uzytkowe/opolskie/sprzedaz",
    "nieruchomosci/garaze/opolskie/sprzedaz",
]

DEFAULT_SELECTORS = {
    "list_selector": "article[data-testid='listing-item'], article.teaserUnified, li.teaserUnified",
    "link_selector": "a[href*='/oferta/'], a",
    "title_selector": "h2, h3, [data-testid='listing-title']",
    "price_selector": "[data-testid='listing-price'], .teaserUnified__price, p.price",
    "location_selector": "[data-testid='listing-location'], .teaserUnified__location",
}


class GratkaScraper(GenericHtmlScraper):
    key = "gratka"
    name = "Gratka.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULT_SELECTORS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

    #: robots.txt Gratki dopuszcza wyłącznie `page=2` … `page=10`
    MAX_ALLOWED_PAGE = 10

    def build_urls(self, ctx: ScrapeContext) -> list[str]:
        # `sort=` jest w robots.txt zabronione, więc korzystamy z domyślnej kolejności
        sections = self.config.get("sections") or SECTIONS
        last_page = min(ctx.max_pages, self.MAX_ALLOWED_PAGE)
        return [
            f"{BASE}/{section}?page={page}" if page > 1 else f"{BASE}/{section}"
            for section in sections
            for page in range(1, last_page + 1)
        ]

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        produced = 0
        seen: set[str] = set()
        for url in self.build_urls(ctx):
            if produced >= ctx.max_items:
                return
            try:
                tree = await self.html(url)
            except Exception:
                continue
            rows = self._items_from_next(self.next_data(tree))
            items = [self._parse_row(r, url) for r in rows] if rows else self.parse_list(tree, url)
            for item in filter(None, items):
                if item.external_id in seen:
                    continue
                seen.add(item.external_id)
                yield item
                produced += 1
                if produced >= ctx.max_items:
                    return

    # ------------------------------------------------------------------ #
    def _items_from_next(self, data: Any, depth: int = 0) -> list[dict]:
        if depth > 7 or data is None:
            return []
        if isinstance(data, list):
            hits = [r for r in data if isinstance(r, dict) and "id" in r and
                    ({"title", "price"} <= set(r) or {"name", "priceValue"} <= set(r))]
            if len(hits) >= 3:
                return hits
            for element in data:
                found = self._items_from_next(element, depth + 1)
                if found:
                    return found
            return []
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, (dict, list)):
                    found = self._items_from_next(value, depth + 1)
                    if found:
                        return found
        return []

    def _parse_row(self, row: dict, page_url: str) -> RawListing | None:
        url = row.get("url") or row.get("link") or row.get("slug")
        if url and not str(url).startswith("http"):
            url = f"{BASE}/{str(url).lstrip('/')}"
        title = clean(row.get("title") or row.get("name") or "")
        if not url or not title:
            return None
        location = row.get("location") or {}
        return RawListing(
            external_id=str(row.get("id")),
            url=url,
            source_key=self.key,
            title=title,
            description=clean(row.get("description") or "") or None,
            price=parse_number(row.get("price") or row.get("priceValue")),
            area=parse_number(row.get("area") or row.get("surface")),
            rooms=int(parse_number(row.get("rooms")) or 0) or None,
            city=clean(location.get("city") or location.get("cityName") or "") or None,
            district=clean(location.get("district") or "") or None,
            street=clean(location.get("street") or "") or None,
            seller_type=SellerType.POSREDNIK if row.get("isAgency") or row.get("agency")
            else SellerType.NIEZNANY,
            seller_name=clean(self.dig(row, "agency", "name", default="") or "") or None,
            images=[i for i in (row.get("images") or row.get("photos") or []) if isinstance(i, str)][:12],
            published_at=parse_datetime(row.get("createdAt") or row.get("publishedAt")),
            property_type=guess_property_type(title, page_url),
            transaction=guess_transaction(title, page_url),
            raw=row,
        )

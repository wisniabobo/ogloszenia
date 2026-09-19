"""Domiporta.pl — listing HTML z paginacją `?PageNumber=`."""

from __future__ import annotations

from ..models import OfferKind
from .base import ScrapeContext
from .generic_html import GenericHtmlScraper

BASE = "https://www.domiporta.pl"

SECTIONS = [
    "mieszkanie/sprzedam/opolskie",
    "mieszkanie/wynajme/opolskie",
    "dom/sprzedam/opolskie",
    "dom/wynajme/opolskie",
    "dzialka/sprzedam/opolskie",
    "lokal/sprzedam/opolskie",
    "garaz/sprzedam/opolskie",
]

DEFAULT_SELECTORS = {
    "list_selector": "article.sneakpeak, div.listing__item, article",
    "link_selector": "a.sneakpeak__title, a[href*='/nieruchomosci/'], a",
    "title_selector": ".sneakpeak__title, h2, h3",
    "price_selector": ".sneakpeak__value--price, .listing__price, .price",
    "area_selector": ".sneakpeak__details, .listing__area",
    "location_selector": ".sneakpeak__title--type, .listing__location",
}


class DomiportaScraper(GenericHtmlScraper):
    key = "domiporta"
    name = "Domiporta.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULT_SELECTORS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

    def build_urls(self, ctx: ScrapeContext) -> list[str]:
        sections = self.config.get("sections") or SECTIONS
        return [
            f"{BASE}/{section}?PageNumber={page}&Sort=Newest"
            for section in sections
            for page in range(1, ctx.max_pages + 1)
        ]

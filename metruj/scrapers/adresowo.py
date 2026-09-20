"""Adresowo.pl — agregator z podziałem na miasta (bez ofert biur w części kategorii)."""

from __future__ import annotations

from ..models import OfferKind
from ..utils.text import slugify
from .base import ScrapeContext
from .generic_html import GenericHtmlScraper

BASE = "https://adresowo.pl"

DEFAULT_CITIES = [
    "Opole", "Nysa", "Kędzierzyn-Koźle", "Brzeg", "Kluczbork", "Prudnik",
    "Strzelce Opolskie", "Krapkowice", "Namysłów", "Głubczyce", "Olesno", "Ozimek",
]

DEFAULT_SELECTORS = {
    "list_selector": "div.result-item, article.search-results__item, li.results__item",
    "link_selector": "a",
    "title_selector": "h2, h3, .result-item__header",
    "price_selector": ".result-price, .result-item__price, .price",
    "area_selector": ".result-params, .result-item__params",
    "location_selector": ".result-address, .result-item__location",
}


class AdresowoScraper(GenericHtmlScraper):
    key = "adresowo"
    name = "Adresowo.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULT_SELECTORS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

    def build_urls(self, ctx: ScrapeContext) -> list[str]:
        cities = ctx.cities or self.config.get("cities") or DEFAULT_CITIES
        sections = self.config.get("sections") or ["mieszkania", "domy", "dzialki"]
        return [
            f"{BASE}/{section}/{slugify(city)}/_s{page}" if page > 1
            else f"{BASE}/{section}/{slugify(city)}/"
            for city in cities
            for section in sections
            for page in range(1, min(ctx.max_pages, 3) + 1)
        ]

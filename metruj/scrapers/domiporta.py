"""Domiporta.pl — listing HTML z paginacją `?PageNumber=`."""

from __future__ import annotations

from ..models import OfferKind
from .base import ScrapeContext
from .generic_html import GenericHtmlScraper

BASE = "https://www.domiporta.pl"

SECTIONS = [
    "mieszkanie/sprzedam/{region}",
    "mieszkanie/wynajme/{region}",
    "dom/sprzedam/{region}",
    "dom/wynajme/{region}",
    "dzialka/sprzedam/{region}",
    "lokal/sprzedam/{region}",
    "garaz/sprzedam/{region}",
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

    def build_sections(self, ctx: ScrapeContext) -> list[list[str]]:
        """Jedna sekcja = jedna kategoria w jednym województwie.

        Podział ma znaczenie przy zatrzymywaniu: wyczerpane wyniki mieszkań
        w Opolskiem nie mogą przerwać zbierania domów na Mazowszu.
        """
        sections = self.config.get("sections") or SECTIONS
        return [
            [
                f"{BASE}/{path}?PageNumber={page}&Sort=Newest"
                for page in range(1, ctx.max_pages + 1)
            ]
            for section in sections
            for path in ctx.expand(section)
        ]

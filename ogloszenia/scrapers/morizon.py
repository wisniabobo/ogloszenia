"""Morizon.pl — klasyczny HTML + JSON-LD na kartach ofert."""

from __future__ import annotations

from ..models import OfferKind
from .base import ScrapeContext
from .generic_html import GenericHtmlScraper

BASE = "https://www.morizon.pl"

SECTIONS = [
    "mieszkania/opolskie",
    "domy/opolskie",
    "dzialki/opolskie",
    "lokale/opolskie",
    "garaze/opolskie",
    "do-wynajecia/mieszkania/opolskie",
    "do-wynajecia/domy/opolskie",
]

DEFAULT_SELECTORS = {
    "list_selector": "div[data-cy='listing__item'], section.single-result, div.card",
    "link_selector": "a[href*='/oferta/'], a.property_link, a",
    "title_selector": "h2, h3, .single-result__title",
    "price_selector": ".single-result__price, [data-cy='listing__price'], .price",
    "area_selector": ".single-result__info, .param_m, [data-cy='listing__area']",
    "location_selector": ".single-result__category, [data-cy='listing__location'], .location",
    "image_selector": "img",
}


class MorizonScraper(GenericHtmlScraper):
    key = "morizon"
    name = "Morizon.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULT_SELECTORS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

    def build_urls(self, ctx: ScrapeContext) -> list[str]:
        sections = self.config.get("sections") or SECTIONS
        return [
            f"{BASE}/{section}/?ps%5Bsorting%5D=newest&page={page}"
            for section in sections
            for page in range(1, ctx.max_pages + 1)
        ]

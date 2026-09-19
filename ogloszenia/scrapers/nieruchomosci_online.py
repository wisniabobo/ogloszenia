"""Nieruchomosci-online.pl — wyszukiwarka `szukaj.html` z parametrami pozycyjnymi."""

from __future__ import annotations

from ..models import OfferKind
from .base import ScrapeContext
from .generic_html import GenericHtmlScraper

BASE = "https://www.nieruchomosci-online.pl"

# format: szukaj.html?3,<typ>,<transakcja>,,<region>
SECTIONS = [
    ("mieszkanie", "sprzedaz"),
    ("mieszkanie", "wynajem"),
    ("dom", "sprzedaz"),
    ("dom", "wynajem"),
    ("dzialka", "sprzedaz"),
    ("lokal", "sprzedaz"),
    ("lokal", "wynajem"),
    ("garaz", "sprzedaz"),
]

DEFAULT_SELECTORS = {
    "list_selector": "div.tile-title, div.column-container, article.tile",
    "link_selector": "a[href*='.html']",
    "title_selector": "h2, h3, a",
    "price_selector": ".title-a, .price, p.title-b",
    "location_selector": ".province, .tile-address",
    "image_selector": "img",
}


class NieruchomosciOnlineScraper(GenericHtmlScraper):
    key = "nieruchomosci_online"
    name = "Nieruchomosci-online.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULT_SELECTORS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

    def build_urls(self, ctx: ScrapeContext) -> list[str]:
        region = self.config.get("region", "Opolskie")
        sections = self.config.get("sections") or SECTIONS
        return [
            f"{BASE}/szukaj.html?3,{ptype},{deal},,{region}&p={page}"
            for ptype, deal in sections
            for page in range(1, ctx.max_pages + 1)
        ]

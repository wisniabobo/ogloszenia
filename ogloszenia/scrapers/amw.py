"""AMW — Agencja Mienia Wojskowego. Sprzedaż mieszkań, lokali i gruntów po wojsku."""

from __future__ import annotations

from ..models import OfferKind, SellerType
from .generic_html import GenericHtmlScraper

BASE = "https://amw.com.pl"

DEFAULTS = {
    "urls": [
        f"{BASE}/pl/oferty?wojewodztwo=opolskie&page={{page}}",
        f"{BASE}/pl/sprzedaz/nieruchomosci?wojewodztwo=opolskie&page={{page}}",
    ],
    "list_selector": "article, div.offer, div.card, tr",
    "link_selector": "a",
    "title_selector": "h2, h3, a",
    "price_selector": ".price, .cena",
    "detail": True,
    "seller_type": SellerType.INSTYTUCJA.value,
    "authority": "Agencja Mienia Wojskowego",
}


class AMWScraper(GenericHtmlScraper):
    key = "amw"
    name = "Agencja Mienia Wojskowego"
    base_url = BASE
    kind = OfferKind.PRZETARG
    coverage = "krajowy"

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULTS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

"""KOWR — Krajowy Ośrodek Wsparcia Rolnictwa, OT Opole.

Przetargi i wykazy nieruchomości rolnych (grunty, zabudowania po dawnym Zasobie
Własności Rolnej Skarbu Państwa).
"""

from __future__ import annotations

from ..models import OfferKind, SellerType
from .generic_html import GenericHtmlScraper

BASE = "https://www.kowr.gov.pl"

DEFAULTS = {
    "urls": [
        f"{BASE}/nieruchomosci/ogloszenia-o-przetargach?wojewodztwo=opolskie&page={{page}}",
        f"{BASE}/nieruchomosci/wykazy-nieruchomosci?wojewodztwo=opolskie&page={{page}}",
    ],
    "list_selector": "div.list-item, article, tr",
    "link_selector": "a",
    "title_selector": "h2, h3, a",
    "date_selector": "time, .date",
    "detail": True,
    "seller_type": SellerType.INSTYTUCJA.value,
    "authority": "Krajowy Ośrodek Wsparcia Rolnictwa OT Opole",
}


class KOWRScraper(GenericHtmlScraper):
    key = "kowr"
    name = "KOWR — nieruchomości rolne"
    base_url = BASE
    kind = OfferKind.PRZETARG
    coverage = "regionalny"

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULTS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

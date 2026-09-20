"""ZUS — ogłoszenia o sprzedaży i najmie nieruchomości oraz licytacje majątku.

ZUS publikuje ogłoszenia w BIP-ie; oddział opolski ma własną sekcję. Strony są
statycznym HTML-em, więc wystarczy generyczny parser z selektorami.
"""

from __future__ import annotations

from ..models import OfferKind, SellerType
from .generic_html import GenericHtmlScraper

BASE = "https://bip.zus.pl"

DEFAULTS = {
    "urls": [
        f"{BASE}/nieruchomosci/sprzedaz-nieruchomosci",
        f"{BASE}/nieruchomosci/najem-i-dzierzawa",
        "https://www.zus.pl/o-zus/zamowienia-publiczne-i-ogloszenia/sprzedaz-nieruchomosci",
    ],
    "list_selector": "article, li.item, div.news-item, tr",
    "link_selector": "a",
    "title_selector": "h2, h3, a",
    "date_selector": "time, .date",
    "detail": True,
    "seller_type": SellerType.INSTYTUCJA.value,
    "authority": "Zakład Ubezpieczeń Społecznych",
}


class ZUSScraper(GenericHtmlScraper):
    key = "zus"
    name = "ZUS — sprzedaż nieruchomości"
    base_url = BASE
    kind = OfferKind.WYKAZ
    coverage = "krajowy"

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULTS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

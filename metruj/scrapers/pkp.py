"""PKP S.A. — sprzedaż nieruchomości kolejowych.

Dawne dworce, budynki przydworcowe, grunty i mieszkania zakładowe. Adres
zweryfikowany 19.09.2026: `pkp.pl/pl/sprzedaz` (dawne `nieruchomosci.pkp.pl`
nie rozwiązuje się w DNS).
"""

from __future__ import annotations

from ..models import OfferKind, SellerType
from .generic_html import GenericHtmlScraper

BASE = "https://www.pkp.pl"

# Selektory odczytane z żywej strony 19.09.2026: karta oferty to blok
# `.opis-oferty` z nazwą, miejscowością i rodzajem nieruchomości.
DEFAULTS = {
    "urls": [
        f"{BASE}/pl/sprzedaz?menu=2",
        f"{BASE}/pl/nieruchomosci-przetargi?menu=2",
    ],
    "list_selector": "a.oferty-kafelek, article, tr",
    "link_selector": "a[href*='show=']",
    "title_selector": ".nazwa, h2, h3, a",
    "location_selector": ".miejscowosc",
    "price_selector": ".cena, .price",
    "image_selector": ".zdj-oferty img, img",
    "detail": True,
    "seller_type": SellerType.INSTYTUCJA.value,
    "authority": "PKP S.A.",
}


class PKPScraper(GenericHtmlScraper):
    key = "pkp"
    name = "PKP S.A. — sprzedaż nieruchomości"
    base_url = BASE
    kind = OfferKind.PRZETARG
    coverage = "krajowy"

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, DEFAULTS | (config or {}), source_key=self.key,
                         kind=self.kind, name=self.name)

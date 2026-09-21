"""PKP S.A. — sprzedaż nieruchomości kolejowych.

Dawne dworce, budynki przydworcowe, grunty i mieszkania zakładowe. Adres
zweryfikowany 19.09.2026: `pkp.pl/pl/sprzedaz` (dawne `nieruchomosci.pkp.pl`
nie rozwiązuje się w DNS).
"""

from __future__ import annotations

import re

from ..models import OfferKind, SellerType
from ..utils.text import clean, parse_number
from .base import RawListing
from .generic_html import GenericHtmlScraper, guess_property_type

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

    async def enrich_detail(self, item: RawListing) -> None:
        """Karta oferty PKP to tabela „etykieta — wartość" i dane opiekuna.

        Ogólny odczyt brał całą stronę: menu, skrypty i listę „wyróżnionych
        ofert" z innych miast, a z tego zgadywał cenę i miejscowość. Tabela
        podaje wprost województwo, powiat, gminę, adres, cenę orientacyjną
        i powierzchnię — i te pola bierzemy.
        """
        tree = await self.html(item.url)
        box = tree.css_first(".oferta-kolumny")
        table = box.css_first("table") if box else None
        if table is None:
            return
        fields: dict[str, str] = {}
        for row in table.css("tr"):
            cells = [clean(c.text(separator=" ")) for c in row.css("td, th")]
            if len(cells) >= 2 and cells[0]:
                fields[cells[0].lower()] = cells[1]

        item.voivodeship = fields.get("województwo") or item.voivodeship
        item.county = fields.get("powiat") or item.county
        item.commune = fields.get("gmina") or item.commune
        if address := fields.get("adres"):
            # „62-800 Kalisz, Stawiszyńska 104A"
            place, _, street = address.partition(",")
            item.city = clean(re.sub(r"^\d{2}-\d{3}\s*", "", place)) or item.city
            item.street = clean(street) or item.street
        if price := parse_number(fields.get("cena orientacyjna") or fields.get("cena wywoławcza")):
            item.price = item.opening_price = price
        if plot := parse_number(fields.get("powierzchnia gruntu")):
            item.plot_area = plot
        if usable := parse_number(fields.get("powierzchnia użytkowa") or fields.get("powierzchnia")):
            item.area = usable
        kind = fields.get("typ nieruchomości")
        if kind:
            item.property_type = guess_property_type(kind, item.title)
        item.description = "; ".join(
            f"{label.capitalize()}: {value}" for label, value in fields.items() if value
        ) or None

        # opiekun oferty: telefon i e-mail z drugiej tabeli
        tables = box.css("table")
        contact = clean(tables[1].text(separator=" ")) if len(tables) > 1 else ""
        item.phones_raw = re.findall(r"(?<!\d)(\d{9})(?!\d)", contact)[:2] or item.phones_raw
        if email := re.search(r"[\w.+-]+@pkp\.pl", contact):
            item.contact_email = email.group(0)

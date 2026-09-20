"""Licytacje komornicze — serwis Krajowej Rady Komorniczej.

Portal został przebudowany: dawny `/Notice/Search` przekierowuje dziś na
wyszukiwarkę pod adresem::

    https://licytacje.komornik.pl/wyszukiwarka-licytacji?mainCategory=REAL_ESTATE&province=opolskie&offset=0

Strona wyników jest renderowana po stronie serwera (Nuxt SSR), więc karty da się
czytać bez przeglądarki. Karta zawiera komplet tego, co potrzebne: kategorię,
tryb (stacjonarna / elektroniczna), datę publikacji, adres z kodem pocztowym,
termin licytacji, cenę wywołania i sumę oszacowania.

Karta szczegółów dociągana jest natomiast po stronie klienta — sygnatura akt,
kancelaria i rękojmia nie występują w HTML-u i bez silnika JS ich nie ma.
Dlatego bierzemy je z treści karty, jeśli tam są, i nie udajemy, że mamy więcej.

e-Licytacje (dawny elicytacje.komornik.pl) zostały włączone do tego samego
serwisu — rozpoznajemy je po tagu „elektroniczna" na karcie.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from selectolax.parser import Node

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import clean, extract_area, extract_case_number, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

BASE = "https://licytacje.komornik.pl"
SEARCH = BASE + "/wyszukiwarka-licytacji"

#: ile kart zwraca jedna strona wyników
PAGE_SIZE = 20

#: kategorie serwisu -> nasze typy nieruchomości
CATEGORY_MAP = {
    "mieszkania": PropertyType.MIESZKANIE,
    "domy": PropertyType.DOM,
    "grunty": PropertyType.DZIALKA,
    "działki": PropertyType.DZIALKA,
    "lokale użytkowe": PropertyType.LOKAL,
    "lokale uzytkowe": PropertyType.LOKAL,
    "garaże": PropertyType.GARAZ,
    "hale": PropertyType.HALA,
    "obiekty przemysłowe": PropertyType.HALA,
    "gospodarstwa rolne": PropertyType.GOSPODARSTWO,
    "spółdzielcze": PropertyType.MIESZKANIE,
}

#: ligatury ikon Material Symbols sklejone z tekstem ("map_markerul. Parkowa")
ICON_PREFIX = re.compile(r"^[a-z]+_[a-z_]*")
POSTAL = re.compile(r"\b(\d{2}-\d{3})\s+(.+)$")


def _strip_icon(text: str) -> str:
    return clean(ICON_PREFIX.sub("", clean(text)))


def _label_value(text: str, label: str) -> str:
    """'Termin licytacji:21.09.2026 11:00' -> '21.09.2026 11:00'."""
    cleaned = _strip_icon(text)
    if label.lower() in cleaned.lower():
        return clean(cleaned.split(":", 1)[-1]) if ":" in cleaned else clean(
            cleaned[len(label):]
        )
    return cleaned


class LicytacjeKomornikScraper(BaseScraper):
    key = "licytacje_komornik"
    name = "Licytacje komornicze (KRK)"
    base_url = BASE
    kind = OfferKind.LICYTACJA
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        province = self.config.get("province") or ctx.voivodeship or ""
        categories = self.config.get("main_categories", ["REAL_ESTATE"])
        produced = 0
        seen: set[str] = set()

        for category in categories:
            for page in range(ctx.max_pages):
                if produced >= ctx.max_items:
                    return
                params = {
                    "mainCategory": category,
                    "province": province,
                    "offset": page * PAGE_SIZE,
                }
                try:
                    tree = await self.html(SEARCH, params=params)
                except Exception:
                    break

                cards = tree.css("a.auction")
                if not cards:
                    break
                fresh = 0
                for card in cards:
                    item = self._parse_card(card)
                    if item is None or item.external_id in seen:
                        continue
                    seen.add(item.external_id)
                    fresh += 1
                    yield item
                    produced += 1
                    if produced >= ctx.max_items:
                        return
                if fresh == 0:
                    break  # serwis oddał tę samą stronę — koniec wyników

    # ------------------------------------------------------------------ #
    def _parse_card(self, card: Node) -> RawListing | None:
        href = card.attributes.get("href") or ""
        match = re.search(r"/licytacje/(\d+)", href)
        if not match:
            return None
        auction_id = match.group(1)

        title_node = card.css_first("[class*='auction__title'], .cds-text--normal-h3")
        title = clean(title_node.text()) if title_node else ""
        if not title:
            title = clean(href.rsplit("/", 1)[-1].replace("-", " "))
        if not title:
            return None

        chips = [clean(c.text()) for c in card.css(".auction__tags .v-chip__content")]
        chips = [c for c in chips if c]
        category = chips[0].lower() if chips else ""
        mode = next((c for c in chips if "elektron" in c.lower() or "stacjon" in c.lower()), "")

        published = None
        node = card.css_first(".auction__publication-date")
        if node:
            published = parse_datetime(_label_value(node.text(), "Opublikowano"))

        province, address = "", ""
        attrs = card.css(".auction__row--location .auction__attribute")
        if attrs:
            province = _strip_icon(attrs[0].text())
            if len(attrs) > 1:
                address = _strip_icon(attrs[1].text())

        event_date = None
        node = card.css_first(".auction__row--dates")
        if node:
            event_date = parse_datetime(_label_value(node.text(), "Termin licytacji"))

        opening = estimate = None
        for price_node in card.css(".auction__price"):
            text = clean(price_node.text())
            value = parse_number(re.sub(r"^[^\d]*", "", text))
            if "oszacowan" in text.lower():
                estimate = value
            elif "wywoła" in text.lower() or "wywola" in text.lower():
                opening = value
        if opening is None and estimate is None:
            prices = [parse_number(re.sub(r"^[^\d]*", "", clean(p.text())))
                      for p in card.css(".auction__price")]
            prices = [p for p in prices if p]
            opening = prices[0] if prices else None
            estimate = prices[1] if len(prices) > 1 else None

        city, street = self._split_address(address)
        property_type = CATEGORY_MAP.get(category) or guess_property_type(title, category)

        return RawListing(
            external_id=auction_id,
            url=urljoin(BASE, href),
            source_key=self.key,
            kind=OfferKind.LICYTACJA,
            transaction=TransactionType.SPRZEDAZ,
            property_type=property_type,
            title=title[:400],
            description=None,
            price=opening or estimate,
            opening_price=opening,
            estimate_value=estimate,
            # rękojmia to ustawowo 1/10 sumy oszacowania — liczymy, zamiast zgadywać
            deposit=round(estimate / 10, 2) if estimate else None,
            event_date=event_date,
            published_at=published,
            case_number=extract_case_number(title),
            authority=None,
            seller_type=SellerType.KOMORNIK,
            location_text=address or province or None,
            city=city,
            street=street,
            area=extract_area(title),
            extra={
                "tryb": mode or None,
                "kategoria_portalu": category or None,
                "rekojmia_wyliczona": bool(estimate),
                "province": province or None,
            },
            raw={"href": href, "chips": chips, "address": address},
        )

    @staticmethod
    def _split_address(address: str) -> tuple[str | None, str | None]:
        """'ul. Parkowa 15, 47-225 Kędzierzyn-Koźle' -> ('Kędzierzyn-Koźle', 'Parkowa')."""
        if not address:
            return None, None
        city = None
        postal = POSTAL.search(address)
        if postal:
            city = clean(postal.group(2)) or None
        else:
            parts = [clean(p) for p in address.split(",") if clean(p)]
            city = parts[-1] if parts else None

        street = None
        street_match = re.search(r"\b(?:ul\.?|al\.?|os\.?|pl\.?)\s*([^,0-9]+)", address)
        if street_match:
            street = clean(street_match.group(1)) or None
        return city, street


class ELicytacjeScraper(LicytacjeKomornikScraper):
    """e-Licytacje — ten sam serwis, wyniki ograniczone do trybu elektronicznego.

    Osobny serwis `elicytacje.komornik.pl` został wygaszony i przekierowuje na
    `licytacje.komornik.pl`; zostawiamy klasę, żeby konfiguracje i zapisane
    poszukiwania sprzed zmiany nadal działały.
    """

    key = "elicytacje"
    name = "e-Licytacje komornicze (tryb elektroniczny)"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        async for item in super().run(ctx):
            if "elektron" in (item.extra.get("tryb") or "").lower():
                yield item

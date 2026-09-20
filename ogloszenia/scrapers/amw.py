"""AMW — Agencja Mienia Wojskowego. Przetargi na mienie po wojsku.

Wszystkie przetargi nieruchomości AMW leżą na jednej stronie wyników::

    /pl/nieruchomosci/przetargi-nieruchomosci/wyniki-wyszukiwania/page,0,limit,100,...

Parametry siedzą w ścieżce po przecinkach, nie w query stringu, a filtr
województwa jest formularzem POST-owym. Nie korzystamy z niego: całość to
dziś nieco ponad osiemdziesiąt ogłoszeń, więc jedno żądanie z `limit,100`
pobiera wszystko, a region odczytujemy z karty — każda podaje wprost
„Woj.: opolskie". To pewniejsze niż identyfikatory pól formularza, które
przy każdej przebudowie serwisu się zmieniają.

Wcześniej zbierał to ogólny parser HTML z selektorami `article, div.card, tr`.
Łapał nimi kafelki nawigacji, przez co do bazy trafił „Polecane nieruchomości"
z ceną 6 zł — czyli pozycja menu, nie oferta.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from selectolax.parser import Node

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import clean, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext

BASE = "https://amw.com.pl"
RESULTS = BASE + "/pl/nieruchomosci/przetargi-nieruchomosci/wyniki-wyszukiwania"

#: Adres oferty kończy się identyfikatorem działki — po nim rozpoznajemy
#: ogłoszenie i odróżniamy je od stron działów („polecane-nieruchomosci").
OFFER_HREF = re.compile(r"/przetargi-nieruchomosci/([a-z0-9][a-z0-9-]*-\d+)$", re.I)

#: „Powierzchnia1161,00 m2" albo „Powierzchnia0,5981 ha"
AREA = re.compile(r"([\d\s.,]+)\s*(ha|m)", re.I)

#: AMW opisuje nieruchomości przeznaczeniem z planu, nie rodzajem obiektu.
#: „mieszkaniowe" oznacza zarówno działkę pod zabudowę mieszkaniową, jak
#: i mieszkanie — reguły niżej rozstrzygają to metrażem.
CATEGORY_RULES = (
    (("garaż", "garaz", "miejsca postojowe"), PropertyType.GARAZ),
    (("magazynowe", "produkcyjne", "przemysłowe"), PropertyType.HALA),
    (("lokale użytkowe", "biurowe", "usługowo", "uslugowo", "handlowe"), PropertyType.LOKAL),
    (("rolne", "gospodarstwo"), PropertyType.GOSPODARSTWO),
    (("działka", "dzialka", "grunt", "rekreacyjno"), PropertyType.DZIALKA),
    (("dom", "zabudowa jednorodzinna"), PropertyType.DOM),
)

#: Powyżej tylu metrów „mieszkaniowe" to na pewno grunt, nie mieszkanie.
#: Największe wojskowe mieszkania mają ok. 100 m², najmniejsze działki AMW
#: liczy się w setkach i tysiącach metrów.
MIESZKANIE_MAX_M2 = 150.0


def _field(text: str, label: str) -> str:
    """'Cena wywoławcza1 600 000 PLN' -> '1 600 000 PLN'."""
    value = clean(text)
    if value.lower().startswith(label.lower()):
        value = value[len(label):]
    return clean(value.lstrip(":"))


def _area_m2(text: str) -> float | None:
    """Karta podaje raz metry, raz hektary — sprowadzamy do metrów."""
    match = AREA.search(text)
    if not match:
        return None
    value = parse_number(match.group(1))
    if value is None:
        return None
    return value * 10_000 if match.group(2).lower() == "ha" else value


class AMWScraper(BaseScraper):
    key = "amw"
    name = "Agencja Mienia Wojskowego"
    base_url = BASE
    kind = OfferKind.PRZETARG
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        voivodeship = (ctx.voivodeship or "opolskie").lower()
        produced = 0
        for page in range(ctx.max_pages):
            url = f"{RESULTS}/page,{page},limit,100,surface_unit,ha,sort,estate_asc"
            try:
                tree = await self.html(url)
            except Exception:
                break

            cards = tree.css("div.element")
            if not cards:
                break
            for card in cards:
                item = self._parse_card(card)
                if item is None:
                    continue
                # AMW wystawia mienie z całego kraju. Województwo mamy podane
                # wprost, więc filtrujemy tutaj, zamiast zgadywać z opisu.
                if voivodeship and (item.extra.get("wojewodztwo") or "") != voivodeship:
                    continue
                yield item
                produced += 1
                if produced >= ctx.max_items:
                    return
            if len(cards) < 100:
                break  # ostatnia strona wyników

    # ------------------------------------------------------------------ #
    def _parse_card(self, card: Node) -> RawListing | None:
        link = next(
            (a for a in card.css("a[href]") if OFFER_HREF.search(a.attributes.get("href") or "")),
            None,
        )
        if link is None:
            return None
        href = link.attributes.get("href") or ""
        slug = OFFER_HREF.search(href).group(1)

        heading = card.css_first("h2")
        title = clean(re.sub(r"\s+", " ", heading.text())) if heading else ""
        if not title:
            return None

        info = " | ".join(clean(p.text()) for p in card.css(".col-informations p"))
        price = area = event_date = None
        transaction = TransactionType.SPRZEDAZ
        for part in info.split("|"):
            part = clean(part)
            low = part.lower()
            if low.startswith("cena wywoławcza"):
                price = parse_number(_field(part, "Cena wywoławcza"))
            elif low.startswith("powierzchnia"):
                area = _area_m2(_field(part, "Powierzchnia"))
            elif low.startswith("data przetargu"):
                event_date = parse_datetime(_field(part, "Data przetargu"))
            elif low in ("najem", "dzierżawa", "wynajem"):
                transaction = TransactionType.WYNAJEM

        location = {}
        for p in card.css(".col-location p"):
            text = clean(p.text())
            for label, key in (("Woj.:", "wojewodztwo"), ("Powiat:", "powiat"), ("Gmina:", "gmina")):
                if text.startswith(label):
                    location[key] = clean(text[len(label):]).lower()

        categories = [
            clean(p.text()).lower()
            for p in card.css(".col-category p")
            if clean(p.text()) and not clean(p.text()).lower().startswith("kategoria")
        ]
        joined = " ".join(categories)
        property_type = PropertyType.INNE
        for needles, kind in CATEGORY_RULES:
            if any(n in joined for n in needles):
                property_type = kind
                break
        if property_type is PropertyType.INNE and "mieszkaniow" in joined:
            property_type = (
                PropertyType.MIESZKANIE
                if area is not None and area <= MIESZKANIE_MAX_M2
                else PropertyType.DZIALKA
            )

        images = [
            img.attributes["src"]
            for img in card.css("img[src]")
            if img.attributes.get("src", "").startswith("http")
        ]

        # Tytuł na karcie ma postać „Brzeg, ul. Małujowicka, dz. 571/62".
        city = clean(title.split(",")[0]) or None

        return RawListing(
            external_id=slug,
            url=urljoin(BASE, href),
            title=title,
            source_key=self.key,
            kind=self.kind,
            transaction=transaction,
            property_type=property_type,
            description=clean(re.sub(r"\s+", " ", card.text() or "")),
            price=price,
            opening_price=price,
            area=area,
            city=city,
            commune=(location.get("gmina") or "").title() or None,
            county=location.get("powiat") or None,
            location_text=", ".join(
                v.title() for v in (location.get("gmina"), location.get("powiat")) if v
            ) or None,
            seller_type=SellerType.INSTYTUCJA,
            seller_name="Agencja Mienia Wojskowego",
            authority="Agencja Mienia Wojskowego",
            event_date=event_date,
            images=images,
            extra={k: v for k, v in location.items() if v},
            # Województwo odczytane z karty, nie zgadnięte z treści.
            region_assured=(location.get("wojewodztwo") or "") == "opolskie",
        )

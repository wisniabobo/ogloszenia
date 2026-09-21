"""Wspólny scraper Gratki i Morizona.

Oba serwisy należą do tej samej grupy i od przebudowy dzielą jeden szablon
karty ogłoszenia — te same klasy `property-card__*`, te same pola, ta sama
treść. Różni je wyłącznie adres bazowy i kształt odnośnika do oferty
(`/ob/12345` w Gratce, `/oferta/...` w Morizonie), więc rozsądniej jest mieć
jeden parser niż dwa rozjeżdżające się.

Karta listy niesie komplet danych: tytuł, cenę, cenę za metr, powierzchnię,
liczbę pokoi, adres z ulicą i dzielnicą, zdjęcie i fragment opisu. Nie
dociągamy więc stron ofert — to oszczędza kilkadziesiąt żądań na przebieg
i nie obciąża cudzego serwera bez potrzeby.

Poprzednia wersja opierała się na selektorach sprzed przebudowy serwisu
(`article[data-testid=listing-item]`, `teaserUnified`). Żaden z nich niczego
już nie łapał, a scraper zamiast zgłosić pustkę zapisywał samą stronę wyników
jako ofertę — stąd w bazie czterdzieści osiem rekordów „Mieszkania na sprzedaż
woj. opolskie" bez ceny i metrażu.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from selectolax.parser import Node

from ..geo import known_voivodeship, lookup
from ..models import OfferKind, SellerType, TransactionType
from ..utils.text import clean, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

CARD = "div.property-card, div.card"
TITLE = ".property-card__title"
PRICE = ".property-card__price--main"
PRICE_M2 = ".property-card__price--perM2"
LOCATION = ".property-card__location, [class*='property-card__location']"
DESCRIPTION = ".property-card__property-description"
DETAILS = ".property-card__property-details"

#: „73 m²", „1 240,5 m2"
AREA = re.compile(r"([\d\s.,]+)\s*m(?:²|2)\b", re.I)
#: „3 pokoje", „1 pokój"
ROOMS = re.compile(r"(\d+)\s*pok", re.I)
#: „parter/1", „2/4"
FLOOR = re.compile(r"^(parter|\d+)\s*/\s*(\d+)$", re.I)
#: „Dodane: 2026.09.19"
ADDED = re.compile(r"Dodane:\s*([\d.\-]{8,10})")

#: Odsiewamy z opisu przycisk rozwijania, który serwis wstawia w tym samym bloku.
SHOW_MORE = re.compile(r"^\s*(?:Zobacz opis|Pokaż opis|Zwiń)\s*", re.I)


def _is_county(name: str) -> bool:
    """Czy nazwa jest powiatem, a nie miejscowością („świecki", „nyski")."""
    units = lookup(name)
    return bool(units) and all(unit.kind == "powiat" for unit in units)


def _is_place(name: str) -> bool:
    """Czy rejestr TERYT zna tę nazwę jako miasto albo gminę."""
    return any(unit.kind == "gmina" for unit in lookup(name))


class RingierScraper(BaseScraper):
    """Podklasy podają `base_url`, `offer_href` i sekcje w konfiguracji."""

    #: wzorzec adresu oferty — po nim poznajemy kartę wśród innych odnośników
    offer_href: re.Pattern[str] = re.compile(r"/oferta/")

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        sections = self.config.get("sections") or []
        produced = 0
        seen: set[str] = set()

        for section in sections:
            raw_path = section["path"] if isinstance(section, dict) else section
            transaction = TransactionType(
                section.get("transaction", "sprzedaz") if isinstance(section, dict) else "sprzedaz"
            )
            # Adres sekcji zawiera województwo, więc jedna pozycja w tablicy
            # obsługuje wszystkie szesnaście — inaczej portal oddawałby wyniki
            # tylko z tego jednego, które ktoś kiedyś wpisał w kodzie.
            for path in ctx.expand(raw_path):
                for page in range(1, ctx.max_pages + 1):
                    url = urljoin(self.base_url, path)
                    params = {"page": page} if page > 1 else None
                    try:
                        tree = await self.html(url, params=params)
                    except Exception:
                        break

                    fresh = 0
                    for card in tree.css(CARD):
                        item = self._parse_card(card, transaction)
                        if item is None or item.external_id in seen:
                            continue
                        seen.add(item.external_id)
                        fresh += 1
                        yield item
                        produced += 1
                        if produced >= ctx.max_items:
                            return
                    if fresh == 0:
                        break  # serwis oddał tę samą stronę albo koniec wyników

    # ------------------------------------------------------------------ #
    def _parse_card(self, card: Node, transaction: TransactionType) -> RawListing | None:
        link = next(
            (a for a in card.css("a[href]") if self.offer_href.search(a.attributes.get("href") or "")),
            None,
        )
        if link is None:
            return None
        href = urljoin(self.base_url, link.attributes.get("href") or "")

        title = clean(self.text(card, TITLE))
        if not title:
            return None

        price = parse_number(self.text(card, PRICE)) or None
        price_m2 = parse_number(self.text(card, PRICE_M2)) or None

        # Powierzchnię, pokoje i piętro serwis wypisuje jako nieopisane etykiety
        # w bloku szczegółów. Rozpoznajemy je po kształcie, nie po kolejności —
        # ta różni się między typami nieruchomości.
        area = rooms = floor = floors_total = None
        details = card.css_first(DETAILS)
        labels = [clean(s.text()) for s in details.css("span")] if details else []
        for label in labels:
            if area is None and (m := AREA.fullmatch(label)):
                area = parse_number(m.group(1))
            elif rooms is None and (m := ROOMS.search(label)):
                rooms = int(m.group(1))
            elif floor is None and (m := FLOOR.match(label)):
                floor = 0 if m.group(1).lower() == "parter" else int(m.group(1))
                floors_total = int(m.group(2))

        # „Jana Bytnara «Rudego», ZWM, Opole, opolskie" — cztery człony, ale
        # bywa ich dwa albo pięć, a kolejność nie zawsze jest ta sama.
        # Czytanie po pozycji dawało w polu „miasto" raz powiat („świecki"),
        # raz dzielnicę („Ponikwoda"), raz nazwę województwa — a każda z tych
        # pomyłek psuła potem odcisk oferty i porównanie z medianą okolicy.
        # Dlatego o tym, który człon jest miejscowością, rozstrzyga rejestr
        # TERYT, a nie miejsce w napisie.
        parts = [clean(p) for p in (self.text(card, LOCATION) or "").split(",") if clean(p)]
        location_text = ", ".join(parts) or None
        voivodeship = county = city = district = street = None

        if parts and known_voivodeship(parts[-1]):
            voivodeship = known_voivodeship(parts.pop())
        if parts and _is_county(parts[-1]):
            county = parts.pop()

        # Miejscowością jest ten człon, który rejestr zna jako miasto albo
        # gminę — szukamy od końca, bo tam stoją jednostki najogólniejsze.
        for index in range(len(parts) - 1, -1, -1):
            if _is_place(parts[index]):
                city = parts.pop(index)
                break
        if city is None and parts:
            city = parts.pop()          # rejestr nie zna — bierzemy ostatni człon
        if parts:
            district = parts.pop()
        if parts:
            street = ", ".join(parts)

        description = clean(SHOW_MORE.sub("", self.text(card, DESCRIPTION)))
        if description.startswith(title):
            description = clean(description[len(title):])

        images = [
            src for img in card.css("img[src]")
            if (src := img.attributes.get("src", "")).startswith("http")
        ]

        published = None
        added = ADDED.search(clean(card.text()))
        if added:
            published = parse_datetime(added.group(1).replace(".", "-"))

        return RawListing(
            external_id=href.rstrip("/").rsplit("/", 1)[-1],
            url=href,
            title=title,
            source_key=self.key,
            kind=OfferKind.NIERUCHOMOSC,
            transaction=transaction,
            property_type=guess_property_type(title, description[:200]),
            description=description or None,
            price=price,
            area=area,
            rooms=rooms,
            floor=floor,
            floors_total=floors_total,
            city=city,
            county=county,
            voivodeship=voivodeship,
            location_text=location_text,
            district=district,
            street=street,
            images=images[:1],
            published_at=published,
            seller_type=SellerType.NIEZNANY,
            extra={"cena_za_m2": price_m2} if price_m2 else {},
            # Adres sekcji zawiera województwo, więc region jest pewny nawet
            # wtedy, gdy miejscowości nie ma w naszym słowniku.
            region_assured=True,
        )

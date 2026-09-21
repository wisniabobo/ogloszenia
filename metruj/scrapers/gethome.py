"""GetHome.pl — oferty biur i deweloperów.

Serwis renderuje listę po stronie serwera i zostawia w HTML-u komplet danych
w `window.__INITIAL_STATE__`. To znacznie lepsze źródło niż karty: nazwy klas
CSS portal generuje losowo przy każdym wdrożeniu, a struktura stanu jest
stabilna i zawiera pola, których na karcie w ogóle nie widać.

Z jednego żądania dostajemy: cenę i cenę za metr, powierzchnię, liczbę pokoi
i łazienek, rok budowy, piętro, rodzaj rynku, pełną galerię zdjęć, adres
z ulicą, **dokładne współrzędne** oraz dane kontaktowe — nazwę biura i numer
telefonu agenta. Numer jest tam jawnie, bez żadnej bramki; to jedyny
z dużych portali, który go nie ukrywa.

Współrzędne z portalu są dokładniejsze niż nasze geokodowanie po adresie,
więc zapisujemy je wprost i oszczędzamy zapytania do GUGiK-u i Nominatima.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from selectolax.parser import HTMLParser

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import clean, parse_datetime, parse_int, parse_number
from .base import BaseScraper, RawListing, ScrapeContext

BASE = "https://gethome.pl"

STATE_MARKER = "__INITIAL_STATE__"

#: `property.type` portalu -> nasz typ nieruchomości
TYPES = {
    "apartment": PropertyType.MIESZKANIE,
    "flat": PropertyType.MIESZKANIE,
    "house": PropertyType.DOM,
    "terrain": PropertyType.DZIALKA,
    "plot": PropertyType.DZIALKA,
    "commercial": PropertyType.LOKAL,
    "premises": PropertyType.LOKAL,
    "office": PropertyType.BIURO,
    "warehouse": PropertyType.HALA,
    "garage": PropertyType.GARAZ,
    "room": PropertyType.POKOJ,
}

DEALS = {"sell": TransactionType.SPRZEDAZ, "rent": TransactionType.WYNAJEM}

#: Rozmiary zdjęć w kolejności od najlepszego do najmniejszego.
PICTURE_KEYS = ("o_img_1280", "o_img_800", "o_img_500", "o_img_360x171")


def _state(tree: HTMLParser) -> dict[str, Any]:
    """Wydobywa `window.__INITIAL_STATE__` z przypisania w skrypcie."""
    for script in tree.css("script"):
        body = script.text() or ""
        if STATE_MARKER not in body:
            continue
        try:
            start = body.index("{", body.index(STATE_MARKER))
            data, _ = json.JSONDecoder().raw_decode(body[start:])
        except (ValueError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}



def _local_timestamp(value):
    """GetHome oznacza czas jako UTC, ale podaje zegar polski.

    Strona pobrana o 19:20 UTC miała już „updated_at": „…T20:31:07Z", a oferty
    „wystawiane" były kwadrans po tym, jak je zobaczyliśmy. Etykietę strefy
    odrzucamy i czytamy godzinę jako czas polski.
    """
    if not isinstance(value, str):
        return parse_datetime(value)
    return parse_datetime(value.strip().removesuffix("Z"), naive_local=True)

class GetHomeScraper(BaseScraper):
    key = "gethome"
    name = "GetHome.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        sections = self.config.get("sections") or []
        produced = 0
        seen: set[str] = set()

        for section in sections:
            raw_path = section["path"] if isinstance(section, dict) else section
            for path in ctx.expand(raw_path):
                for page in range(1, ctx.max_pages + 1):
                    try:
                        tree = await self.html(
                            BASE + path, params={"page": page} if page > 1 else None
                        )
                    except Exception:
                        break
                    rows = (
                        _state(tree)
                        .get("offerList", {})
                        .get("offers", {})
                        .get("offers")
                    ) or []
                    if not rows:
                        break

                    fresh = 0
                    for row in rows:
                        item = self._parse(row)
                        if item is None or item.external_id in seen:
                            continue
                        seen.add(item.external_id)
                        fresh += 1
                        yield item
                        produced += 1
                        if produced >= ctx.max_items:
                            return
                    if fresh == 0:
                        break

    def _parse(self, row: dict) -> RawListing | None:
        slug = clean(row.get("slug"))
        title = clean(row.get("name"))
        if not slug or not title:
            return None

        prop = row.get("property") or {}
        details = prop.get("address_details") or {}
        price = (row.get("price") or {}).get("total")

        coords = row.get("coordinates") or {}
        lat, lon = coords.get("lat"), coords.get("lon")

        phones: list[str] = []
        agent = row.get("agent") or {}
        if number := clean(agent.get("phone_number")):
            phones.append(number)

        agency = row.get("agency") or {}
        seller_name = clean(agency.get("name")) or clean(
            f"{agent.get('name', '')} {agent.get('last_name', '')}"
        )
        if row.get("is_private"):
            seller_type = SellerType.PRYWATNA
        elif agency.get("type") == "developer":
            seller_type = SellerType.DEWELOPER
        elif seller_name:
            seller_type = SellerType.POSREDNIK
        else:
            seller_type = SellerType.NIEZNANY

        images: list[str] = []
        for picture in row.get("pictures") or []:
            for key in PICTURE_KEYS:
                if url := picture.get(key):
                    images.append(url)
                    break

        # „ul. Sieradzka" — przedrostek odcinamy, bo psuje geokodowanie,
        # a w interfejsie i tak dokłada go szablon.
        street = clean(details.get("street"))
        if street.lower().startswith(("ul. ", "ul ")):
            street = clean(street.split(" ", 1)[1])

        floors = prop.get("floors") or {}
        floor = floors.get("floor") if isinstance(floors, dict) else None
        floors_total = floors.get("building_floors") if isinstance(floors, dict) else None

        return RawListing(
            external_id=str(row.get("id") or slug),
            url=f"{BASE}/oferta/{slug}/",
            title=title,
            source_key=self.key,
            kind=self.kind,
            transaction=DEALS.get(row.get("deal_type"), TransactionType.SPRZEDAZ),
            property_type=TYPES.get(prop.get("type"), PropertyType.INNE),
            description=clean(row.get("description")) or None,
            # Portal podaje te pola raz liczbą, raz łańcuchem („67.31"), więc
            # każde przepuszczamy przez konwersję — bez tego walidacja metrażu
            # wywracała się na porównaniu liczby z tekstem.
            price=parse_number(price),
            area=parse_number(prop.get("size")),
            rooms=parse_int(prop.get("room_number")),
            floor=parse_int(floor),
            floors_total=parse_int(floors_total),
            year_built=parse_int(prop.get("building_year")),
            market="pierwotny" if row.get("market_type") == "primary_market" else "wtórny",
            city=clean(details.get("city")) or None,
            street=street or None,
            lat=lat,
            lon=lon,
            images=images[:12],
            phones_raw=phones,
            seller_type=seller_type,
            seller_name=seller_name or None,
            published_at=_local_timestamp(row.get("created_at")),
            # Adres sekcji zawęża wyniki do województwa.
            region_assured=True,
        )

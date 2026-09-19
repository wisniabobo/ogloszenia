"""Otodom.pl — aplikacja Next.js; dane listingu siedzą w `__NEXT_DATA__`.

Ścieżka w JSON-ie zmieniała się już kilka razy, więc zamiast jednej sztywnej
lokalizacji przeszukujemy drzewo w poszukiwaniu kolekcji z polami typowymi dla
ogłoszenia (`slug`, `totalPrice`, `areaInSquareMeters`). To przeżywa większość
przemeblowań frontu.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import clean, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

BASE = "https://www.otodom.pl"

SEARCHES = [
    ("sprzedaz", "mieszkanie", PropertyType.MIESZKANIE, TransactionType.SPRZEDAZ),
    ("sprzedaz", "dom", PropertyType.DOM, TransactionType.SPRZEDAZ),
    ("sprzedaz", "dzialka", PropertyType.DZIALKA, TransactionType.SPRZEDAZ),
    ("sprzedaz", "lokal", PropertyType.LOKAL, TransactionType.SPRZEDAZ),
    ("sprzedaz", "haleimagazyny", PropertyType.HALA, TransactionType.SPRZEDAZ),
    ("sprzedaz", "garaz", PropertyType.GARAZ, TransactionType.SPRZEDAZ),
    ("wynajem", "mieszkanie", PropertyType.MIESZKANIE, TransactionType.WYNAJEM),
    ("wynajem", "dom", PropertyType.DOM, TransactionType.WYNAJEM),
    ("wynajem", "lokal", PropertyType.LOKAL, TransactionType.WYNAJEM),
]

AGENCY_MARKERS = {"AGENCY", "DEVELOPER", "BUSINESS"}
LISTING_MARKERS = {"slug", "title"}


class OtodomScraper(BaseScraper):
    key = "otodom"
    name = "Otodom.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        produced = 0
        region = self.config.get("region_slug", "opolskie")
        searches = self.config.get("searches") or SEARCHES

        for transaction_slug, type_slug, ptype, ttype in searches:
            for page in range(1, ctx.max_pages + 1):
                if produced >= ctx.max_items:
                    return
                url = (
                    f"{BASE}/pl/wyniki/{transaction_slug}/{type_slug}/{region}"
                    f"?page={page}&limit=72&by=LATEST&direction=DESC&viewType=listing"
                )
                try:
                    tree = await self.html(url)
                except Exception:
                    break
                data = self.next_data(tree)
                rows = self._find_items(data)
                if not rows:
                    break
                for row in rows:
                    item = self._parse(row, ptype, ttype)
                    if item:
                        yield item
                        produced += 1
                        if produced >= ctx.max_items:
                            return

    # ------------------------------------------------------------------ #
    def _find_items(self, data: Any, depth: int = 0) -> list[dict]:
        """Rekurencyjnie znajduje listę ofert w `__NEXT_DATA__`."""
        if depth > 8 or data is None:
            return []
        if isinstance(data, list):
            hits = [
                row for row in data
                if isinstance(row, dict) and LISTING_MARKERS <= set(row) and
                ("totalPrice" in row or "areaInSquareMeters" in row or "price" in row)
            ]
            if len(hits) >= 3:
                return hits
            for element in data:
                found = self._find_items(element, depth + 1)
                if found:
                    return found
            return []
        if isinstance(data, dict):
            for key in ("items", "searchAds", "ads", "organic", "data", "pageProps", "props"):
                if key in data:
                    found = self._find_items(data[key], depth + 1)
                    if found:
                        return found
            for value in data.values():
                if isinstance(value, (dict, list)):
                    found = self._find_items(value, depth + 1)
                    if found:
                        return found
        return []

    def _parse(self, row: dict, ptype: PropertyType, ttype: TransactionType) -> RawListing | None:
        slug = row.get("slug")
        if not slug:
            return None
        url = f"{BASE}/pl/oferta/{slug}"
        title = clean(row.get("title") or "")
        if not title:
            return None

        price = parse_number(self.dig(row, "totalPrice", "value") or row.get("price"))
        area = parse_number(row.get("areaInSquareMeters"))
        rooms = row.get("roomsNumber")
        if isinstance(rooms, str):
            rooms = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
                     "SIX": 6, "SEVEN": 7, "EIGHT": 8}.get(rooms.upper())
        location = row.get("location") or {}
        address = self.dig(location, "address", default={}) or {}
        city = clean(self.dig(address, "city", "name", default="") or "")
        district = clean(self.dig(address, "district", "name", default="") or "") or None
        street = clean(self.dig(address, "street", "name", default="") or "") or None
        county = clean(self.dig(address, "county", "name", default="") or "") or None

        owner_type = str(row.get("agency") and "AGENCY" or row.get("ownerType") or "").upper()
        seller_type = SellerType.POSREDNIK if owner_type in AGENCY_MARKERS else (
            SellerType.PRYWATNA if owner_type == "PRIVATE" else SellerType.NIEZNANY
        )
        if str(row.get("developmentId") or "") not in ("", "None"):
            seller_type = SellerType.DEWELOPER

        images = [
            img.get("large") or img.get("medium") or img.get("small")
            for img in (row.get("images") or [])
            if isinstance(img, dict)
        ]

        return RawListing(
            external_id=str(row.get("id") or slug),
            url=url,
            source_key=self.key,
            title=title,
            description=clean(row.get("shortDescription") or row.get("description") or "") or None,
            price=price,
            area=area,
            rooms=int(rooms) if isinstance(rooms, (int, float)) else None,
            floor=None,
            city=city or None,
            district=district,
            street=street,
            county=county,
            lat=self.dig(location, "coordinates", "latitude"),
            lon=self.dig(location, "coordinates", "longitude"),
            seller_type=seller_type,
            seller_name=clean(self.dig(row, "agency", "name", default="") or "") or None,
            images=[i for i in images if i][:12],
            published_at=parse_datetime(row.get("dateCreated") or row.get("createdAt")),
            source_updated_at=parse_datetime(row.get("pushedUpAt") or row.get("modifiedAt")),
            property_type=ptype if ptype != PropertyType.INNE else guess_property_type(title),
            transaction=ttype,
            market="pierwotny" if row.get("market") == "PRIMARY" else
                   ("wtorny" if row.get("market") == "SECONDARY" else None),
            extra={"isPromoted": bool(row.get("isPromoted"))},
            raw=row,
        )

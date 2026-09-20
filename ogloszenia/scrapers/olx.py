"""OLX.pl — publiczne API v1, z którego korzysta sama strona.

Endpoint, z którego korzystamy:

    GET /api/v1/offers/?region_id=&category_id=&offset=&limit=&sort_by=

To jedyna ścieżka API, którą OLX **sam dopuszcza** w robots.txt::

    Disallow: /api/
    Allow: /api/v1/offers/

Dlatego nie sięgamy po `/api/v1/geo-encoder/regions/` ani po dawny
`/api/v1/friendly-links/...` (ten zresztą zwraca już 404) — są objęte zakazem.
Identyfikatory województw i kategorii mamy więc w tablicach poniżej; zostały
ustalone empirycznie i można je nadpisać w `config/sources.yaml`, gdyby OLX je
przenumerował.

Telefony: `/api/v1/offers/{id}/limited-phones/` wymaga tokenu konta OLX.
Bot nie obchodzi tego zabezpieczenia — jeśli token jest w konfiguracji
(`config.auth_token`), używa go; w przeciwnym razie numery bierzemy z treści
ogłoszenia, o ile wystawiający je tam podał.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from typing import Any

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import clean, deaccent, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

API = "https://www.olx.pl/api/v1"

#: kategoria -> (typ nieruchomości, rodzaj transakcji)
CATEGORIES: dict[int, tuple[PropertyType, TransactionType]] = {
    14: (PropertyType.MIESZKANIE, TransactionType.SPRZEDAZ),
    15: (PropertyType.MIESZKANIE, TransactionType.WYNAJEM),
    16: (PropertyType.MIESZKANIE, TransactionType.ZAMIANA),
    18: (PropertyType.DOM, TransactionType.SPRZEDAZ),
    20: (PropertyType.DOM, TransactionType.WYNAJEM),
    22: (PropertyType.DOM, TransactionType.ZAMIANA),
    24: (PropertyType.DZIALKA, TransactionType.SPRZEDAZ),
    25: (PropertyType.DZIALKA, TransactionType.DZIERZAWA),
    32: (PropertyType.LOKAL, TransactionType.NIEZNANY),
    11: (PropertyType.POKOJ, TransactionType.WYNAJEM),
}

#: kategoria nadrzędna „Nieruchomości" — awaryjne źródło, gdy id-ki się zmienią
PARENT_CATEGORY = 3

#: Ile ofert OLX oddaje na jedno zapytanie (maksimum przyjmowane przez API).
PAGE_SIZE = 50

#: Najdalszy offset, jaki API obsługuje dla jednego zapytania. Dalej odpowiada
#: pustą listą, więc pełne pokrycie bierze się z podziału na województwa
#: i kategorie, a nie z jednego głębokiego przejścia.
MAX_OFFSET = 1000

ROOMS_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "kawalerka": 1}
FLOOR_WORDS = {"floor_cellar": -1, "floor_ground": 0, "parter": 0, "floor_garret": 99}


class OLXScraper(BaseScraper):
    key = "olx"
    name = "OLX.pl"
    base_url = "https://www.olx.pl"
    kind = OfferKind.NIERUCHOMOSC

    @functools.cached_property
    def categories(self) -> dict[int, tuple[PropertyType, TransactionType]]:
        configured = self.config.get("categories")
        if not configured:
            return CATEGORIES
        return {
            int(cid): (PropertyType(v[0]), TransactionType(v[1])) for cid, v in configured.items()
        }

    def _regions(self, ctx: ScrapeContext) -> list[tuple[str, int]]:
        """(nazwa województwa, identyfikator regionu OLX) dla tego przebiegu.

        Pełne pokrycie kraju bierze się właśnie stąd: API oddaje najwyżej
        tysiąc ofert na jedno zapytanie, więc zamiast jednego zapytania
        „cała Polska" robimy szesnaście — po jednym na województwo — razy
        dziesięć kategorii. To 160 zapytań, które razem obejmują cały zasób.
        """
        if self.config.get("region_id"):
            return [("", int(self.config["region_id"]))]
        override = self.config.get("regions") or {}
        out: list[tuple[str, int]] = []
        for entry in ctx.regions:
            name = str(entry.get("nazwa", ""))
            region_id = override.get(deaccent(name).lower()) or entry.get("olx_region_id")
            if region_id:
                out.append((name, int(region_id)))
        return out

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        produced = 0
        seen: set[str] = set()
        exhausted: set[tuple[int, int]] = set()
        max_pages = ctx.max_pages if not ctx.deep else MAX_OFFSET // PAGE_SIZE
        regions = self._regions(ctx)

        # Pętla po stronach jest **na zewnątrz**, a po województwach w środku.
        # Odwrotna kolejność wyczerpywała limit ofert na pierwszym regionie
        # z brzegu i do pozostałych piętnastu skan nigdy nie docierał — przy
        # zawężeniu limitem w bazie lądowało samo Dolnośląskie.
        for page in range(max_pages):
            offset = page * PAGE_SIZE
            if offset >= MAX_OFFSET:
                return
            for voivodeship, region_id in regions:
                for category_id, (ptype, ttype) in self.categories.items():
                    if produced >= ctx.max_items:
                        return
                    if (region_id, category_id) in exhausted:
                        continue
                    params = {
                        "offset": offset,
                        "limit": PAGE_SIZE,
                        "region_id": region_id,
                        "category_id": category_id,
                        "sort_by": "created_at:desc",
                    }
                    try:
                        payload = await self.client.get_json(f"{API}/offers/", params=params)
                    except Exception:
                        exhausted.add((region_id, category_id))
                        continue
                    rows = self.dig(payload, "data", default=[]) or []
                    if len(rows) < PAGE_SIZE:
                        exhausted.add((region_id, category_id))
                    for row in rows:
                        item = self._parse_offer(row, ptype, ttype)
                        if item is None or item.external_id in seen:
                            continue
                        item.voivodeship = item.voivodeship or voivodeship or None
                        seen.add(item.external_id)
                        yield item
                        produced += 1
            if len(exhausted) >= len(regions) * len(self.categories):
                return

    # ------------------------------------------------------------------ #
    def _parse_offer(
        self, row: dict, ptype: PropertyType, ttype: TransactionType
    ) -> RawListing | None:
        offer_id = row.get("id")
        url = row.get("url")
        title = clean(row.get("title") or "")
        if not offer_id or not url or not title:
            return None
        if str(row.get("status", "active")) not in ("active", "", "new"):
            return None

        description = clean(row.get("description") or "")
        location = row.get("location") or {}
        city = clean(self.dig(location, "city", "name", default="") or "")
        district = clean(self.dig(location, "district", "name", default="") or "") or None
        region = clean(self.dig(location, "region", "name", default="") or "")

        params = {p.get("key"): p for p in row.get("params", []) if isinstance(p, dict)}

        def value_of(key: str) -> Any:
            entry = (params.get(key) or {}).get("value")
            if isinstance(entry, dict):
                return entry.get("key") or entry.get("label") or entry.get("value")
            return entry

        price = currency = None
        price_entry = (params.get("price") or {}).get("value")
        if isinstance(price_entry, dict):
            price = parse_number(price_entry.get("value"))
            currency = price_entry.get("currency")

        rent = None
        rent_entry = (params.get("rent") or {}).get("value")
        if isinstance(rent_entry, dict):
            rent = parse_number(rent_entry.get("value"))

        area = parse_number(value_of("m"))
        rooms_raw = value_of("rooms")
        rooms = ROOMS_WORDS.get(str(rooms_raw).lower()) if rooms_raw else None
        if rooms is None and rooms_raw is not None:
            parsed = parse_number(str(rooms_raw))
            rooms = int(parsed) if parsed is not None else None

        floor = None
        floor_raw = value_of("floor_select")
        if floor_raw is not None:
            key = str(floor_raw).lower()
            floor = FLOOR_WORDS.get(key)
            if floor is None:
                parsed = parse_number(key.replace("floor_", ""))
                floor = int(parsed) if parsed is not None else None

        business = bool(row.get("business")) or bool(self.dig(row, "user", "is_business", default=False))
        seller_name = clean(self.dig(row, "user", "name", default="") or "") or None
        shop_name = clean(self.dig(row, "shop", "name", default="") or "") or None

        photos = [
            (p.get("link") or "").replace("{width}", "1000").replace("{height}", "750")
            for p in row.get("photos", [])
            if isinstance(p, dict) and p.get("link")
        ]
        # OLX wystawia w `contact.phone` flagę bool ("czy jest numer"), a nie numer
        raw_phone = self.dig(row, "contact", "phone", default=None)
        contact_phone = clean(raw_phone) if isinstance(raw_phone, str) else ""

        transaction = ttype
        if transaction == TransactionType.NIEZNANY:
            transaction = (
                TransactionType.WYNAJEM
                if rent is not None or "wynaj" in f"{title} {description[:200]}".lower()
                else TransactionType.SPRZEDAZ
            )

        return RawListing(
            external_id=str(offer_id),
            url=url,
            source_key=self.key,
            kind=OfferKind.NIERUCHOMOSC,
            property_type=ptype if ptype != PropertyType.INNE else guess_property_type(title, description),
            transaction=transaction,
            title=title,
            description=description or None,
            price=price,
            currency=currency or "PLN",
            area=area,
            rooms=rooms,
            floor=floor,
            building_type=clean(str(value_of("builttype") or "")) or None,
            market=clean(str(value_of("market") or "")) or None,
            city=city or None,
            district=district,
            # OLX podaje województwo w osobnym polu — to ono rozstrzyga, które
            # z trzech polskich „Opoli" mamy na myśli.
            voivodeship=region or None,
            location_text=", ".join(x for x in (city, district, region) if x) or None,
            lat=self.dig(location, "lat"),
            lon=self.dig(location, "lon"),
            seller_type=SellerType.POSREDNIK if business else SellerType.PRYWATNA,
            seller_name=shop_name or seller_name,
            phones_raw=[contact_phone] if contact_phone else [],
            images=photos[:12],
            published_at=parse_datetime(row.get("created_time")),
            source_updated_at=parse_datetime(row.get("last_refresh_time")),
            extra={
                "czynsz": rent,
                "promowana": bool(self.dig(row, "promotion", "highlighted", default=False)),
                "kategoria_olx": self.dig(row, "category", "id"),
            },
            raw=row,
        )

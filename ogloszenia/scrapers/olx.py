"""OLX.pl — publiczne API v1 używane przez stronę.

Zamiast zgadywać identyfikatory kategorii i regionów (potrafią się zmieniać),
najpierw pytamy OLX o tłumaczenie „przyjaznego” adresu na parametry zapytania:

    GET /api/v1/friendly-links/query-params/nieruchomosci/mieszkania/sprzedaz/opolskie/

a dopiero potem odpytujemy listę ofert:

    GET /api/v1/offers/?offset=0&limit=50&category_id=...&region_id=...&sort_by=created_at:desc

Gdy API zwróci błąd, schodzimy na parsowanie HTML (`__NEXT_DATA__` listingu).

Telefony: endpoint `/api/v1/offers/{id}/limited-phones/` wymaga tokenu konta OLX.
Bot go nie obchodzi — jeśli token jest skonfigurowany (`config.auth_token`),
korzysta z niego; w przeciwnym razie numery wyciągane są z treści ogłoszenia.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..models import OfferKind, SellerType
from ..utils.text import clean, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type, guess_transaction

API = "https://www.olx.pl/api/v1"

DEFAULT_PATHS = [
    "nieruchomosci/mieszkania/sprzedaz/opolskie/",
    "nieruchomosci/mieszkania/wynajem/opolskie/",
    "nieruchomosci/domy/sprzedaz/opolskie/",
    "nieruchomosci/domy/wynajem/opolskie/",
    "nieruchomosci/dzialki/sprzedaz/opolskie/",
    "nieruchomosci/lokale/sprzedaz/opolskie/",
    "nieruchomosci/garaze-parkingi/opolskie/",
    "nieruchomosci/pokoje/opolskie/",
]

# Mapowanie parametrów OLX -> pola RawListing
PARAM_MAP = {
    "m": "area",           # powierzchnia
    "price": "price",
    "rooms": "rooms",
    "floor_select": "floor",
    "builttype": "building_type",
    "market": "market",
    "furniture": None,
}

ROOMS_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "kawalerka": 1}


class OLXScraper(BaseScraper):
    key = "olx"
    name = "OLX.pl"
    base_url = "https://www.olx.pl"
    kind = OfferKind.NIERUCHOMOSC

    async def _resolve_params(self, path: str) -> dict[str, Any]:
        """Zamienia ścieżkę kategorii na parametry API (category_id, region_id, ...)."""
        try:
            data = await self.client.get_json(f"{API}/friendly-links/query-params/{path.strip('/')}/")
        except Exception:
            return {}
        params = self.dig(data, "data", "params", default=None)
        if isinstance(params, dict):
            return {k: v for k, v in params.items() if v not in (None, "")}
        return {k: v for k, v in (self.dig(data, "data", default={}) or {}).items()
                if isinstance(v, (str, int))}

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        paths = self.config.get("paths") or DEFAULT_PATHS
        limit = min(50, ctx.max_items)
        produced = 0

        for path in paths:
            params = await self._resolve_params(path)
            if not params:
                params = dict(self.config.get("fallback_params", {}))
            if not params:
                continue
            params.setdefault("sort_by", "created_at:desc")

            for page in range(ctx.max_pages):
                if produced >= ctx.max_items:
                    return
                query = params | {"offset": page * limit, "limit": limit}
                try:
                    payload = await self.client.get_json(f"{API}/offers/", params=query)
                except Exception:
                    break
                rows = self.dig(payload, "data", default=[]) or []
                if not rows:
                    break
                for row in rows:
                    item = self._parse_offer(row)
                    if item:
                        yield item
                        produced += 1
                        if produced >= ctx.max_items:
                            return
                if len(rows) < limit:
                    break

    # ------------------------------------------------------------------ #
    def _parse_offer(self, row: dict) -> RawListing | None:
        offer_id = row.get("id")
        url = row.get("url")
        title = clean(row.get("title") or "")
        if not offer_id or not url or not title:
            return None

        description = clean(row.get("description") or "")
        location = row.get("location") or {}
        city = clean(self.dig(location, "city", "name", default="") or "")
        district = clean(self.dig(location, "district", "name", default="") or "") or None
        region = clean(self.dig(location, "region", "name", default="") or "")

        params = {p.get("key"): p for p in row.get("params", []) if isinstance(p, dict)}

        def param_value(key: str) -> Any:
            entry = params.get(key) or {}
            value = entry.get("value")
            if isinstance(value, dict):
                return value.get("key") or value.get("label") or value.get("value")
            return value

        price = None
        price_entry = (params.get("price") or {}).get("value") or {}
        if isinstance(price_entry, dict):
            price = parse_number(price_entry.get("value"))
            currency = price_entry.get("currency") or "PLN"
        else:
            currency = "PLN"

        area = parse_number(param_value("m"))
        rooms_raw = param_value("rooms")
        rooms = ROOMS_WORDS.get(str(rooms_raw).lower()) if rooms_raw else None
        if rooms is None:
            rooms = int(parse_number(rooms_raw)) if parse_number(rooms_raw) else None

        floor_raw = param_value("floor_select")
        floor = None
        if floor_raw is not None:
            text = str(floor_raw).lower()
            floor = 0 if "parter" in text or text in {"floor_0", "0"} else None
            if floor is None:
                num = parse_number(text.replace("floor_", ""))
                floor = int(num) if num is not None else None

        business = bool(row.get("business")) or self.dig(row, "user", "is_business", default=False)
        seller_type = SellerType.POSREDNIK if business else SellerType.PRYWATNA
        seller_name = clean(self.dig(row, "user", "name", default="") or "") or None

        photos = [
            (p.get("link") or "").replace("{width}", "800").replace("{height}", "600")
            for p in row.get("photos", [])
            if isinstance(p, dict) and p.get("link")
        ]
        contact_phone = clean(self.dig(row, "contact", "phone", default="") or "")

        return RawListing(
            external_id=str(offer_id),
            url=url,
            source_key=self.key,
            kind=OfferKind.NIERUCHOMOSC,
            title=title,
            description=description or None,
            price=price,
            currency=currency,
            area=area,
            rooms=rooms,
            floor=floor,
            building_type=clean(str(param_value("builttype") or "")) or None,
            market=clean(str(param_value("market") or "")) or None,
            city=city or None,
            district=district,
            location_text=", ".join(x for x in (city, district, region) if x) or None,
            lat=self.dig(location, "lat"),
            lon=self.dig(location, "lon"),
            seller_type=seller_type,
            seller_name=seller_name,
            phones_raw=[contact_phone] if contact_phone else [],
            images=photos[:12],
            published_at=parse_datetime(row.get("created_time")),
            source_updated_at=parse_datetime(row.get("last_refresh_time") or row.get("valid_to_time")),
            property_type=guess_property_type(title, description, url),
            transaction=guess_transaction(title, url),
            extra={"promoted": bool(row.get("promotion", {}).get("highlighted"))},
            raw=row,
        )

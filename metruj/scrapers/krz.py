"""Krajowy Rejestr Zadłużonych — obwieszczenia syndyków o sprzedaży majątku.

KRZ udostępnia publiczne wyszukiwanie obwieszczeń (POST z filtrem). Interesują
nas obwieszczenia o sprzedaży z masy upadłości — to najczęściej nieruchomości
sprzedawane poniżej wartości rynkowej, więc dla bota są bardzo wartościowe.

Endpoint i kształt zapytania trzymamy w konfiguracji, bo KRZ potrafi zmieniać
API między wersjami; brak odpowiedzi nie wywraca przebiegu, tylko zapisuje błąd
przy źródle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..models import OfferKind, SellerType
from ..utils.text import (
    clean,
    extract_area,
    extract_case_number,
    parse_local_datetime,
    parse_number,
)
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

BASE = "https://krz.ms.gov.pl"
DEFAULT_API = "https://krz-brs.ms.gov.pl/api/krz-brs-public/obwieszczenia/search"

SALE_KEYWORDS = ("sprzedaż", "sprzedazy", "przetarg", "licytacj", "konkurs ofert", "obwieszczenie o sprzedaży")


class KRZScraper(BaseScraper):
    key = "krz"
    name = "Krajowy Rejestr Zadłużonych (syndycy)"
    base_url = BASE
    kind = OfferKind.LICYTACJA
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        endpoint = self.config.get("api_url", DEFAULT_API)
        produced = 0
        for page in range(ctx.max_pages):
            body = {
                "page": page,
                "size": 50,
                "sort": "dataObwieszczenia,desc",
                "wojewodztwo": self.config.get("voivodeship", "OPOLSKIE"),
            } | dict(self.config.get("payload", {}))
            try:
                payload = await self.client.post_json(endpoint, body)
            except Exception:
                return
            rows = (
                self.dig(payload, "content", default=None)
                or self.dig(payload, "data", default=None)
                or (payload if isinstance(payload, list) else [])
            )
            if not rows:
                return
            for row in rows:
                item = self._parse(row)
                if item:
                    yield item
                    produced += 1
                    if produced >= ctx.max_items:
                        return

    def _parse(self, row: dict) -> RawListing | None:
        if not isinstance(row, dict):
            return None
        title = clean(
            row.get("tytul") or row.get("rodzajObwieszczenia") or row.get("typ") or ""
        )
        content = clean(row.get("tresc") or row.get("opis") or "")
        haystack = f"{title} {content}".lower()
        if not any(word in haystack for word in SALE_KEYWORDS):
            return None
        obw_id = row.get("id") or row.get("numerObwieszczenia")
        if not obw_id:
            return None
        return RawListing(
            external_id=str(obw_id),
            url=row.get("url") or f"{BASE}/#/obwieszczenie/{obw_id}",
            source_key=self.key,
            kind=OfferKind.LICYTACJA,
            title=(title or content[:160])[:400],
            description=content or None,
            price=parse_number(row.get("cenaWywolawcza")),
            opening_price=parse_number(row.get("cenaWywolawcza")),
            estimate_value=parse_number(row.get("wartoscOszacowania")),
            deposit=parse_number(row.get("wadium")),
            deadline=parse_local_datetime(row.get("terminSkladaniaOfert")),
            event_date=parse_local_datetime(row.get("dataObwieszczenia") or row.get("terminOtwarcia")),
            case_number=clean(row.get("sygnatura") or "") or extract_case_number(content),
            authority=clean(row.get("syndyk") or row.get("organ") or "") or None,
            seller_type=SellerType.SYNDYK,
            location_text=clean(row.get("miejscowosc") or row.get("wojewodztwo") or "") or None,
            city=clean(row.get("miejscowosc") or "") or None,
            area=extract_area(content),
            property_type=guess_property_type(title, content),
            raw=row,
        )

"""Katalogi biur nieruchomości — kompletna lista pośredników w regionie.

Portale prowadzą własne katalogi biur i to jest **najlepsze źródło listy
pośredników**, jakie istnieje: zamiast zgadywać nazwy, bierzemy je stamtąd,
gdzie biura same się rejestrują. Otodom oddaje przy okazji pełny numer
telefonu, adres z kodem pocztowym i **liczbę aktywnych ofert** — dzięki temu
od razu wiadomo, ile ofert powinniśmy u danego biura znaleźć, i widać, gdy
czegoś brakuje.

Zweryfikowane 19.09.2026: dla woj. opolskiego katalog Otodom zwraca
**110 biur** z łącznie ~1950 aktywnymi ofertami.

Ten scraper nie produkuje ogłoszeń — zasila rejestr biur (`agencies`).
Dlatego zwraca `RawListing` w rodzaju `INNE` z kompletem danych w `extra`,
a pipeline zamienia je na wpisy w rejestrze zamiast w ofertach.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..models import OfferKind, SellerType
from ..utils.text import clean
from .base import BaseScraper, RawListing, ScrapeContext

OTODOM = "https://www.otodom.pl"
DIRECTORY = f"{OTODOM}/pl/firmy/biura-nieruchomosci/lista"

#: znaczniki rekordu biura w __NEXT_DATA__
AGENCY_MARKERS = {"name", "contacts"}


class AgencyDirectoryScraper(BaseScraper):
    """Pobiera katalog biur nieruchomości dla województwa."""

    key = "katalog_biur"
    name = "Katalog biur nieruchomości (Otodom)"
    base_url = OTODOM
    kind = OfferKind.INNE
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        region = self.config.get("region_slug", ctx.voivodeship or "opolskie")
        seen: set[str] = set()
        # katalog ma 20 pozycji na stronę; idziemy aż przestaną przybywać nowe
        max_pages = max(ctx.max_pages, int(self.config.get("max_pages", 40)))

        for page in range(1, max_pages + 1):
            url = f"{DIRECTORY}/{region}"
            try:
                tree = await self.html(url, params={"page": page})
            except Exception:
                break
            rows = self._extract(self.next_data(tree))
            if not rows:
                break
            fresh = 0
            for row in rows:
                item = self._parse(row, region)
                if item is None or item.external_id in seen:
                    continue
                seen.add(item.external_id)
                fresh += 1
                yield item
            if fresh == 0:
                break  # katalog zaczął powtarzać stronę — koniec

    # ------------------------------------------------------------------ #
    def _extract(self, data: Any, depth: int = 0) -> list[dict]:
        if depth > 8 or data is None:
            return []
        if isinstance(data, list):
            hits = [r for r in data if isinstance(r, dict) and AGENCY_MARKERS <= set(r)]
            if hits:
                return hits
            for element in data:
                found = self._extract(element, depth + 1)
                if found:
                    return found
            return []
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, (dict, list)):
                    found = self._extract(value, depth + 1)
                    if found:
                        return found
        return []

    def _parse(self, row: dict, region: str) -> RawListing | None:
        name = clean(row.get("name") or "")
        if not name:
            return None
        slug = self.dig(row, "attributes", "slug", default="") or ""
        agency_id = row.get("id") or slug or name

        location = row.get("location") or {}
        stats = row.get("statistics") or {}
        sell = (stats.get("adsSell") or {}).get("activeAdsCount") or 0
        rent = (stats.get("adsRent") or {}).get("activeAdsCount") or 0

        phone = clean(self.dig(row, "contacts", "phone", default="") or "")

        return RawListing(
            external_id=str(agency_id),
            url=f"{OTODOM}/pl/firmy/biura-nieruchomosci/{slug}" if slug else OTODOM,
            source_key=self.key,
            kind=OfferKind.INNE,
            title=name[:300],
            seller_type=SellerType.POSREDNIK,
            seller_name=name[:300],
            phones_raw=[phone] if phone else [],
            city=clean(location.get("name") or "") or None,
            location_text=clean(location.get("fullName") or "") or None,
            images=[row["photo"]] if row.get("photo") else [],
            extra={
                "katalog": True,
                "slug": slug or None,
                "adres": clean(location.get("address") or "") or None,
                "kod_pocztowy": clean(location.get("postalCode") or "") or None,
                "oferty_sprzedaz": sell,
                "oferty_wynajem": rent,
                "oferty_razem": sell + rent,
                "zarejestrowane": row.get("createdAt"),
                "region": region,
            },
            raw=row,
        )


def agencies_from_raw(items: list[RawListing]) -> list[dict]:
    """Zamienia wynik katalogu na rekordy do rejestru biur."""
    out: list[dict] = []
    for item in items:
        extra = item.extra or {}
        out.append(
            {
                "name": item.seller_name or item.title,
                "city": item.city,
                "phones": list(item.phones_raw),
                "address": extra.get("adres"),
                "postal_code": extra.get("kod_pocztowy"),
                "listings_expected": extra.get("oferty_razem") or 0,
                "profile_url": item.url,
                "source": item.source_key,
                "raw": json.dumps(extra, ensure_ascii=False)[:2000],
            }
        )
    return out

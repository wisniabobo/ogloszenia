"""Katalog biur nieruchomości i deweloperów — cała Polska.

Portale prowadzą własne katalogi firm i to jest **najlepsze źródło listy
pośredników**, jakie istnieje: zamiast zgadywać nazwy z ogłoszeń, bierzemy je
stamtąd, gdzie biura same się rejestrują. Katalog Otodom oddaje pełny numer
telefonu, adres z kodem pocztowym, województwo i **liczbę aktywnych ofert** —
dzięki temu od razu wiadomo, ile ofert powinniśmy u danego biura znaleźć,
i widać czarno na białym, gdy czegoś brakuje.

Sprawdzone na żywo 20.09.2026: katalog bez zawężania regionem zwraca
**13 047 biur** (20 na stronę, stronicowanie przez `offset`/`hasNext`), a
sitemapa portalu wymienia 6 368 profili firm. Wcześniej czytaliśmy tylko pięć
województw, bo serwis obejmował jeden region — teraz bierzemy całą listę.

Ten scraper nie produkuje ogłoszeń: zwraca `RawListing` w rodzaju `INNE`
z kompletem danych w `extra`, a pipeline zamienia je na wpisy w rejestrze biur.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..models import OfferKind, SellerType
from ..utils.text import clean
from .base import BaseScraper, RawListing, ScrapeContext

OTODOM = "https://www.otodom.pl"

#: Dwa katalogi: pośrednicy i deweloperzy. Oba mają ten sam kształt danych.
CATALOGUES: list[tuple[str, SellerType]] = [
    ("biura-nieruchomosci", SellerType.POSREDNIK),
    ("deweloperzy", SellerType.DEWELOPER),
]

#: Ile pozycji katalog oddaje na stronę.
PAGE_SIZE = 20

#: znaczniki rekordu biura w __NEXT_DATA__
AGENCY_MARKERS = {"name", "contacts"}


class AgencyDirectoryScraper(BaseScraper):
    """Pobiera katalog firm — domyślnie z całej Polski."""

    key = "katalog_biur"
    name = "Katalog biur nieruchomości (Otodom)"
    base_url = OTODOM
    kind = OfferKind.INNE
    coverage = "krajowy"

    def _regions(self, ctx: ScrapeContext) -> list[str]:
        """Fragmenty adresu katalogu. Pusty = cała Polska (jedna lista)."""
        configured = self.config.get("region_slugs")
        if configured:
            return [str(r) for r in configured]
        if ctx.voivodeships:
            return [str(e["otodom_slug"]) for e in ctx.regions if e.get("otodom_slug")]
        return [""]

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        seen: set[str] = set()
        # Katalog zmienia się wolno (biura powstają i znikają w skali miesięcy),
        # więc pełne przejście robimy w trybie głębokim, a zwykły skan bierze
        # kilka pierwszych stron, żeby wyłapać nowe firmy.
        max_pages = int(self.config.get("max_pages", 800)) if ctx.deep else max(ctx.max_pages, 5)

        for catalogue, seller_type in CATALOGUES:
            for region in self._regions(ctx):
                async for item in self._pages(catalogue, seller_type, region, max_pages, seen):
                    yield item

    async def _pages(
        self, catalogue: str, seller_type: SellerType, region: str,
        max_pages: int, seen: set[str],
    ) -> AsyncIterator[RawListing]:
        url = f"{OTODOM}/pl/firmy/{catalogue}/lista" + (f"/{region}" if region else "")
        for page in range(1, max_pages + 1):
            try:
                tree = await self.html(url, params={"page": page})
            except Exception:
                return
            payload = self._accounts(self.next_data(tree))
            rows = payload.get("accounts") or self._extract(self.next_data(tree))
            if not rows:
                return
            fresh = 0
            for row in rows:
                item = self._parse(row, catalogue, seller_type)
                if item is None or item.external_id in seen:
                    continue
                seen.add(item.external_id)
                fresh += 1
                yield item
            # Portal sam mówi, czy jest następna strona — nie trzeba zgadywać
            # po liczbie wyników.
            if not (payload.get("pagination") or {}).get("hasNext", fresh > 0):
                return
            if fresh == 0:
                return  # katalog zaczął powtarzać stronę

    # ------------------------------------------------------------------ #
    @staticmethod
    def _accounts(data: Any, depth: int = 0) -> dict:
        """Znajduje blok `accounts` z listą firm i informacją o paginacji."""
        if depth > 8 or not isinstance(data, (dict, list)):
            return {}
        if isinstance(data, dict):
            if isinstance(data.get("accounts"), list) and "pagination" in data:
                return data
            for value in data.values():
                found = AgencyDirectoryScraper._accounts(value, depth + 1)
                if found:
                    return found
            return {}
        for element in data:
            found = AgencyDirectoryScraper._accounts(element, depth + 1)
            if found:
                return found
        return {}

    def _extract(self, data: Any, depth: int = 0) -> list[dict]:
        """Zapas, gdyby portal przemeblował kształt odpowiedzi."""
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

    def _parse(
        self, row: dict, catalogue: str, seller_type: SellerType
    ) -> RawListing | None:
        name = clean(row.get("name") or "")
        if not name or str(row.get("status") or "ACTIVE").upper() != "ACTIVE":
            return None
        slug = self.dig(row, "attributes", "slug", default="") or ""
        agency_id = row.get("id") or slug or name

        location = row.get("location") or {}
        stats = row.get("statistics") or {}
        sell = (stats.get("adsSell") or {}).get("activeAdsCount") or 0
        rent = (stats.get("adsRent") or {}).get("activeAdsCount") or 0

        phone = clean(self.dig(row, "contacts", "phone", default="") or "")

        # `fullName` ma postać „Kraków, małopolskie" — stąd bierzemy województwo
        full_name = clean(location.get("fullName") or "")
        voivodeship = full_name.rsplit(",", 1)[-1].strip() if "," in full_name else None

        return RawListing(
            external_id=str(agency_id),
            url=f"{OTODOM}/pl/firmy/{catalogue}/{slug}" if slug else OTODOM,
            source_key=self.key,
            kind=OfferKind.INNE,
            title=name[:300],
            seller_type=seller_type,
            seller_name=name[:300],
            phones_raw=[phone] if phone else [],
            city=clean(location.get("name") or "") or None,
            voivodeship=voivodeship,
            location_text=full_name or None,
            images=[row["photo"]] if row.get("photo") else [],
            extra={
                "katalog": True,
                "slug": slug or None,
                "rodzaj": catalogue,
                "adres": clean(location.get("address") or "") or None,
                "kod_pocztowy": clean(location.get("postalCode") or "") or None,
                "oferty_sprzedaz": sell,
                "oferty_wynajem": rent,
                "oferty_razem": sell + rent,
                "zarejestrowane": row.get("createdAt"),
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
                "voivodeship": item.voivodeship,
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

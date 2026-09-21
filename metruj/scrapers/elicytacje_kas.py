"""eLicytacje KAS — licytacje Krajowej Administracji Skarbowej.

Portal ruszył 1 lipca 2026 i zebrał w jednym miejscu to, co wcześniej wisiało
w ogłoszeniach poszczególnych urzędów skarbowych: licytacje, przetargi ofert
i sprzedaż z wolnej ręki majątku zajętego w egzekucji.

Publiczne API (zweryfikowane 19.09.2026, działa bez konta):

    POST /auction/api/v1/announcement/external?page=&size=&sort=creationTimestamp,desc
         body: {"sectionId": "<uuid sekcji>", "categoryId": "<uuid kategorii>"}
    GET  /auction/api/v1/dict/sections
    GET  /auction/api/v1/dict/sections/{uuid}/categories

Filtra po województwie API nie ma, więc pobieramy całą sekcję „Nieruchomości"
(rzędu kilkuset pozycji w skali kraju) i odsiewamy po nazwie miejscowości
słownikiem regionu — tym samym, który rozpoznaje lokalizacje w ogłoszeniach.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..geo import detect_location
from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import clean, extract_area, parse_datetime, parse_local_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext

BASE = "https://elicytacje.mf.gov.pl"
API = f"{BASE}/auction/api/v1"

#: identyfikatory ze słownika portalu (GET /dict/sections, /dict/sections/{id}/categories)
SECTION_NIERUCHOMOSCI = "8777b999-fbac-4696-a11e-5323a52760f1"
CATEGORIES: dict[str, PropertyType] = {
    "6c8acd79-fbb0-425f-a4ad-d556851ca532": PropertyType.DOM,
    "7a59957f-9ff0-477f-b807-8c7c1fd84e7e": PropertyType.MIESZKANIE,
    "4c98a773-6ddf-4320-a0aa-896fc20b6ef2": PropertyType.LOKAL,
    "11d01e63-801e-487b-a16f-f9b8a22d0b94": PropertyType.DZIALKA,
    "f72dc9a8-c522-486a-a662-03252051e6ad": PropertyType.GARAZ,
}

#: kody rodzaju ogłoszenia — etykiety wzięte wprost ze słownika portalu
#: (/auction/assets/i18n/search/pl.json), żeby niczego nie zgadywać
TYPE_LABELS = {
    "LICELENIER": "elektroniczna licytacja nieruchomości",
    "LICELEPRWM": "elektroniczna licytacja praw majątkowych",
    "LICELERUCH": "elektroniczna licytacja ruchomości",
    "OSZNELNIER": "termin opisu i oszacowania nieruchomości",
    "SPRNELPRWM": "nieelektroniczna sprzedaż praw majątkowych z wolnej ręki",
    "SPRNELRUCH": "nieelektroniczna sprzedaż ruchomości z wolnej ręki",
}

#: „opis i oszacowanie" to jeszcze nie sprzedaż — to etap *przed* licytacją.
#: Dla kupującego to najcenniejszy sygnał: nieruchomość dopiero trafi na rynek,
#: więc zostawiamy takie pozycje, ale wyraźnie je oznaczamy.
PRE_AUCTION_CODES = {"OSZNELNIER"}


class ELicytacjeKASScraper(BaseScraper):
    key = "elicytacje_kas"
    name = "eLicytacje KAS (urzędy skarbowe)"
    base_url = BASE
    kind = OfferKind.LICYTACJA
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        section = self.config.get("section_id", SECTION_NIERUCHOMOSCI)
        categories = self.config.get("categories") or list(CATEGORIES)
        page_size = 100
        produced = 0
        seen: set[str] = set()

        for category_id in categories:
            for page in range(ctx.max_pages):
                if produced >= ctx.max_items:
                    return
                try:
                    payload = await self.client.post_json(
                        f"{API}/announcement/external",
                        {"sectionId": section, "categoryId": category_id},
                        params={
                            "page": page,
                            "size": page_size,
                            "sort": "creationTimestamp,desc",
                        },
                        headers={"Origin": BASE, "Referer": f"{BASE}/"},
                    )
                except Exception:
                    break
                rows = (payload or {}).get("content") or []
                if not rows:
                    break
                for row in rows:
                    item = self._parse(row, CATEGORIES.get(category_id, PropertyType.INNE))
                    if item is None or item.external_id in seen:
                        continue
                    seen.add(item.external_id)
                    yield item
                    produced += 1
                    if produced >= ctx.max_items:
                        return
                if len(rows) < page_size:
                    break

    # ------------------------------------------------------------------ #
    def _parse(self, row: dict, property_type: PropertyType) -> RawListing | None:
        uuid = row.get("uuid")
        title = clean(row.get("title") or "")
        if not uuid or not title:
            return None
        if row.get("cancellationDateTime") or row.get("voidDateTime"):
            return None  # odwołana albo unieważniona

        location = clean(row.get("localization") or "")
        # API oddaje licytacje z całego kraju; lokalizację czytamy z pola
        # `localization` tym samym mechanizmem, co w zwykłych ogłoszeniach
        place = detect_location(location)

        codes = row.get("announcementTypeCodes") or []
        labels = [TYPE_LABELS.get(code, code) for code in codes]
        pre_auction = any(code in PRE_AUCTION_CODES for code in codes)

        starting = parse_number(row.get("startingPrice"))
        return RawListing(
            external_id=str(uuid),
            url=f"{BASE}/auction/announcements/{uuid}",
            source_key=self.key,
            kind=OfferKind.LICYTACJA,
            transaction=TransactionType.SPRZEDAZ,
            property_type=property_type,
            title=title[:400],
            price=starting,
            opening_price=starting,
            event_date=parse_local_datetime(row.get("saleBeginDateTime")),
            deadline=parse_local_datetime(row.get("depositDueDate") or row.get("saleEndDateTime")),
            published_at=parse_datetime(row.get("publicationDateTime")),
            seller_type=SellerType.URZAD,
            authority="Krajowa Administracja Skarbowa",
            location_text=location or None,
            city=place.city or (location or None),
            county=place.county,
            commune=place.commune,
            voivodeship=place.voivodeship,
            area=extract_area(title),
            images=(
                [f"{API}/items/pictures/{row['pictureId']}/thumbnail"]
                if row.get("pictureId")
                else []
            ),
            extra={
                "rodzaj_sprzedazy": ", ".join(labels) or None,
                "etap": "przed licytacją (opis i oszacowanie)" if pre_auction else "sprzedaż",
                "przed_licytacja": pre_auction,
                "stan": row.get("electronicStateCode"),
                "najwyzsza_oferta": row.get("highestBid"),
                "cena_koncowa": row.get("finalPrice"),
                "kody": codes,
            },
            raw=row,
        )

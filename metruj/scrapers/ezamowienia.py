"""Platforma e-Zamówienia (ezamowienia.gov.pl) — przetargi publiczne.

Publiczne API, zweryfikowane 19.09.2026 — **metodą GET**, nie POST (POST zwraca
405, na czym wykładała się pierwsza wersja tego scrapera)::

    GET /mo-board/api/v1/Board/Search?SortingColumnName=PublicationDate
        &SortingDirection=DESC&PageNumber=1&PageSize=50

Województwo siedzi w polu `organizationProvince` jako kod `PL` + numer TERYT
(opolskie = **PL16**). Parametr filtrujący po stronie serwera nie działa —
sprawdzone: przekazanie `Province` nie zmienia wyniku — więc odsiewamy sami.

Z całego BZP interesują nas zamówienia okołonieruchomościowe, czyli CPV
45 (roboty budowlane), 70 (usługi w zakresie nieruchomości) i 71 (usługi
architektoniczne). Reszta to tonery i catering.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..models import OfferKind, SellerType, TransactionType
from ..utils.text import clean, parse_datetime, parse_local_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext

BASE = "https://ezamowienia.gov.pl"
SEARCH = f"{BASE}/mo-board/api/v1/Board/Search"

#: kod województwa w polu organizationProvince (PL + numer TERYT)
PROVINCE_CODES = {
    "dolnoslaskie": "PL02", "kujawsko-pomorskie": "PL04", "lubelskie": "PL06",
    "lubuskie": "PL08", "lodzkie": "PL10", "malopolskie": "PL12", "mazowieckie": "PL14",
    "opolskie": "PL16", "podkarpackie": "PL18", "podlaskie": "PL20", "pomorskie": "PL22",
    "slaskie": "PL24", "swietokrzyskie": "PL26", "warminsko-mazurskie": "PL28",
    "wielkopolskie": "PL30", "zachodniopomorskie": "PL32",
}

#: CPV, które mają cokolwiek wspólnego z nieruchomościami
CPV_PREFIXES = ("45", "70", "71")

#: rodzaje ogłoszeń — domyślnie pomijamy wyniki postępowań (już rozstrzygnięte)
SKIP_NOTICE_TYPES = {"TenderResultNotice"}


class EZamowieniaScraper(BaseScraper):
    key = "ezamowienia"
    name = "e-Zamówienia (przetargi publiczne)"
    base_url = BASE
    kind = OfferKind.PRZETARG
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        from ..utils.text import deaccent

        wanted = PROVINCE_CODES.get(
            deaccent(ctx.voivodeship or "").lower(), ""
        )
        cpv = tuple(self.config.get("cpv_prefixes", CPV_PREFIXES))
        skip_types = set(self.config.get("skip_notice_types", SKIP_NOTICE_TYPES))
        page_size = 100
        produced = 0

        for page in range(1, ctx.max_pages + 1):
            if produced >= ctx.max_items:
                return
            params = {
                "SortingColumnName": "PublicationDate",
                "SortingDirection": "DESC",
                "PageNumber": page,
                "PageSize": page_size,
            }
            try:
                payload = await self.client.get_json(SEARCH, params=params)
            except Exception:
                return
            rows = payload if isinstance(payload, list) else (payload or {}).get("items") or []
            if not rows:
                return
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("organizationProvince") != wanted:
                    continue
                if row.get("noticeType") in skip_types:
                    continue
                item = self._parse(row, cpv)
                if item is None:
                    continue
                yield item
                produced += 1
                if produced >= ctx.max_items:
                    return

    # ------------------------------------------------------------------ #
    def _parse(self, row: dict, cpv_prefixes: tuple[str, ...]) -> RawListing | None:
        notice_number = row.get("noticeNumber") or row.get("bzpNumber")
        title = clean(row.get("orderObject") or "")
        if not notice_number or not title:
            return None

        cpv = str(row.get("cpvCode") or "")
        if cpv_prefixes and cpv and not cpv.startswith(cpv_prefixes):
            return None

        object_id = row.get("objectId") or row.get("moIdentifier")
        contractors = row.get("contractors") or []
        return RawListing(
            external_id=str(notice_number),
            url=f"{BASE}/mo-client-board/bzp/notice-details/id/{object_id}"
            if object_id
            else f"{BASE}/mo-client-board/bzp/list",
            source_key=self.key,
            kind=OfferKind.PRZETARG,
            transaction=TransactionType.NIEZNANY,
            title=title[:400],
            deadline=parse_local_datetime(row.get("submittingOffersDate")),
            published_at=parse_datetime(row.get("publicationDate")),
            price=parse_number(row.get("orderValue")),
            authority=clean(row.get("organizationName") or "") or None,
            seller_type=SellerType.URZAD,
            city=clean(row.get("organizationCity") or "") or None,
            location_text=clean(row.get("organizationCity") or "") or None,
            extra={
                "cpv": cpv or None,
                "rodzaj_ogloszenia": row.get("noticeType"),
                "rodzaj_zamowienia": row.get("orderType"),
                "nr_bzp": row.get("bzpNumber"),
                "nip_zamawiajacego": row.get("organizationNationalId"),
                "ponizej_progu_ue": row.get("isTenderAmountBelowEU"),
                "wykonawcy": [c.get("contractorName") for c in contractors if isinstance(c, dict)],
                "pdf": row.get("pdfUrl"),
            },
            raw=row,
        )

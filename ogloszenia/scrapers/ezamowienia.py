"""Platforma e-Zamówienia (ezamowienia.gov.pl) — przetargi publiczne.

Filtrujemy po kodach CPV związanych z nieruchomościami i robotami budowlanymi
oraz po województwie, żeby nie zasypywać bazy zamówieniami na tonery.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..models import OfferKind, SellerType, TransactionType
from ..utils.text import clean, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext

BASE = "https://ezamowienia.gov.pl"
DEFAULT_API = f"{BASE}/mo-board/api/v1/Board/Search"

# CPV: 70* usługi w zakresie nieruchomości, 45* roboty budowlane,
# 71* usługi architektoniczne/budowlane
CPV_PREFIXES = ("70", "45", "71")


class EZamowieniaScraper(BaseScraper):
    key = "ezamowienia"
    name = "e-Zamówienia (przetargi publiczne)"
    base_url = BASE
    kind = OfferKind.PRZETARG
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        endpoint = self.config.get("api_url", DEFAULT_API)
        cpv = tuple(self.config.get("cpv_prefixes", CPV_PREFIXES))
        produced = 0
        for page in range(ctx.max_pages):
            body = {
                "SortingColumnName": "PublicationDate",
                "SortingDirection": "DESC",
                "PageNumber": page + 1,
                "PageSize": 50,
                "NoticeType": "ContractNotice",
                "Voivodeship": self.config.get("voivodeship", "OPOLSKIE"),
            } | dict(self.config.get("payload", {}))
            try:
                payload = await self.client.post_json(endpoint, body)
            except Exception:
                return
            rows = (
                self.dig(payload, "items", default=None)
                or self.dig(payload, "data", default=None)
                or (payload if isinstance(payload, list) else [])
            )
            if not rows:
                return
            for row in rows:
                item = self._parse(row, cpv)
                if item:
                    yield item
                    produced += 1
                    if produced >= ctx.max_items:
                        return

    def _parse(self, row: dict, cpv_prefixes: tuple[str, ...]) -> RawListing | None:
        if not isinstance(row, dict):
            return None
        notice_id = row.get("noticeNumber") or row.get("id") or row.get("bzpNumber")
        title = clean(row.get("orderObject") or row.get("title") or row.get("name") or "")
        if not notice_id or not title:
            return None
        cpv = str(row.get("cpvCode") or row.get("mainCpv") or "")
        if cpv_prefixes and cpv and not cpv.startswith(cpv_prefixes):
            return None
        authority = clean(row.get("organizationName") or row.get("contractingAuthority") or "")
        return RawListing(
            external_id=str(notice_id),
            url=row.get("htmlUrl") or f"{BASE}/mp-client/search/list/{notice_id}",
            source_key=self.key,
            kind=OfferKind.PRZETARG,
            transaction=TransactionType.NIEZNANY,
            title=title[:400],
            description=clean(row.get("shortDescription") or "") or None,
            price=parse_number(row.get("orderValue")),
            deadline=parse_datetime(row.get("submittingOffersDate") or row.get("tenderSubmissionDeadline")),
            event_date=parse_datetime(row.get("openingOffersDate")),
            published_at=parse_datetime(row.get("publicationDate")),
            authority=authority or None,
            seller_type=SellerType.URZAD,
            location_text=clean(row.get("organizationCity") or row.get("voivodeship") or "") or None,
            city=clean(row.get("organizationCity") or "") or None,
            extra={"cpv": cpv, "noticeType": row.get("noticeType"), "procedure": row.get("procedureType")},
            raw=row,
        )

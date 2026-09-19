"""Licytacje komornicze.

Dwa źródła:
  * `licytacje.komornik.pl` — obwieszczenia o licytacjach prowadzone przez
    Krajową Radę Komorniczą (licytacje „tradycyjne”, w sądzie),
  * `elicytacje.komornik.pl` — elektroniczne licytacje ruchomości i nieruchomości.

Oba mają publiczne wyszukiwarki z filtrem po województwie. Parsujemy tabelę
wyników, a szczegóły (suma oszacowania, cena wywoławcza, rękojmia, sygnatura,
kancelaria) dociągamy z karty obwieszczenia.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from ..models import OfferKind, SellerType, TransactionType
from ..utils.text import clean, extract_area, extract_case_number, parse_datetime, parse_number, sha1
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

KRK_BASE = "https://licytacje.komornik.pl"
ELIC_BASE = "https://elicytacje.komornik.pl"

# Etykiety spotykane na kartach obwieszczeń
LABELS = {
    "suma oszacowania": "estimate_value",
    "cena wywoławcza": "opening_price",
    "cena wywolawcza": "opening_price",
    "kwota oszacowania": "estimate_value",
    "rękojmia": "deposit",
    "rekojmia": "deposit",
    "wadium": "deposit",
    "sygnatura": "case_number",
    "sygn. akt": "case_number",
    "data licytacji": "event_date",
    "termin licytacji": "event_date",
    "udział": "share",
}


def _apply_labels(item: RawListing, text: str) -> None:
    """Wyciąga z tekstu karty pary 'etykieta: wartość'."""
    for raw_line in re.split(r"[\n\r]+|(?<=\.)\s{2,}", text):
        line = clean(raw_line)
        if not line or ":" not in line:
            continue
        label, _, value = line.partition(":")
        field = LABELS.get(clean(label).lower())
        if not field or not clean(value):
            continue
        if field in {"estimate_value", "opening_price", "deposit"}:
            setattr(item, field, parse_number(value))
        elif field == "event_date":
            item.event_date = parse_datetime(value)
        else:
            setattr(item, field, clean(value)[:120])


class LicytacjeKomornikScraper(BaseScraper):
    key = "licytacje_komornik"
    name = "Licytacje komornicze (KRK)"
    base_url = KRK_BASE
    kind = OfferKind.LICYTACJA
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        # Wyszukiwarka KRK przyjmuje województwo jako parametr GET.
        voivodeship_id = self.config.get("voivodeship_id", 16)  # 16 = opolskie (TERYT)
        produced = 0
        for page in range(1, ctx.max_pages + 1):
            url = self.config.get(
                "search_url",
                f"{KRK_BASE}/Notice/Search?VoivodeshipId={voivodeship_id}&Page={page}",
            ).format(page=page, voivodeship_id=voivodeship_id)
            try:
                tree = await self.html(url)
            except Exception:
                break
            rows = tree.css("table tr") or tree.css("div.notice, li.notice")
            found = 0
            for row in rows:
                link = row.css_first("a[href*='/Notice/Details'], a[href*='/Notice/']")
                if not link:
                    continue
                href = link.attributes.get("href")
                if not href:
                    continue
                detail_url = urljoin(KRK_BASE, href)
                body = clean(row.text())
                title = clean(link.text()) or body[:160]
                item = RawListing(
                    external_id=self._id_from_url(detail_url),
                    url=detail_url,
                    source_key=self.key,
                    kind=OfferKind.LICYTACJA,
                    transaction=TransactionType.SPRZEDAZ,
                    title=title[:400],
                    seller_type=SellerType.KOMORNIK,
                    authority=None,
                    case_number=extract_case_number(body),
                    event_date=parse_datetime(body),
                    property_type=guess_property_type(title, body),
                    location_text=body,
                    area=extract_area(body),
                    raw={"row": body},
                )
                if ctx.fetch_details:
                    try:
                        await self._detail(item)
                    except Exception:
                        pass
                item.price = item.opening_price or item.estimate_value
                yield item
                produced += 1
                found += 1
                if produced >= ctx.max_items:
                    return
            if not found:
                break

    async def _detail(self, item: RawListing) -> None:
        tree = await self.html(item.url)
        main = tree.css_first("main") or tree.css_first("#content") or tree.css_first("body")
        text = clean(main.text()) if main else ""
        item.description = text[:20000] or None
        _apply_labels(item, (main.html or "") if main else "")
        _apply_labels(item, text.replace(". ", ".\n"))
        if not item.case_number:
            item.case_number = extract_case_number(text)
        if not item.event_date:
            item.event_date = parse_datetime(text)
        for heading in tree.css("h1, h2"):
            head = clean(heading.text())
            if head and len(head) > 10:
                item.title = head[:400]
                break
        office = re.search(r"(Komornik\s+S[ąa]dowy[^.\n]{0,160})", text)
        if office:
            item.authority = clean(office.group(1))[:300]

    @staticmethod
    def _id_from_url(url: str) -> str:
        m = re.search(r"(?:id=|/)(\d{4,})", url, re.I)
        return m.group(1) if m else sha1(url)[:20]


class ELicytacjeScraper(BaseScraper):
    """e-Licytacje — aplikacja z API JSON; przy zmianie API schodzimy na HTML."""

    key = "elicytacje"
    name = "e-Licytacje komornicze"
    base_url = ELIC_BASE
    kind = OfferKind.LICYTACJA

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        endpoint = self.config.get("api_url", f"{ELIC_BASE}/api/auctions")
        produced = 0
        for page in range(1, ctx.max_pages + 1):
            params = {
                "page": page,
                "perPage": 50,
                "voivodeship": self.config.get("voivodeship", "opolskie"),
                "sort": "-publishedAt",
            } | dict(self.config.get("params", {}))
            try:
                payload = await self.client.get_json(endpoint, params=params)
            except Exception:
                break
            rows = payload if isinstance(payload, list) else (
                self.dig(payload, "data", default=[]) or self.dig(payload, "items", default=[]) or []
            )
            if not rows:
                break
            for row in rows:
                item = self._parse(row)
                if not item:
                    continue
                yield item
                produced += 1
                if produced >= ctx.max_items:
                    return

    def _parse(self, row: dict) -> RawListing | None:
        if not isinstance(row, dict):
            return None
        auction_id = row.get("id") or row.get("auctionId") or row.get("number")
        title = clean(row.get("title") or row.get("name") or row.get("subject") or "")
        if not auction_id or not title:
            return None
        url = row.get("url") or f"{ELIC_BASE}/items/{auction_id}"
        description = clean(row.get("description") or "")
        return RawListing(
            external_id=str(auction_id),
            url=url,
            source_key=self.key,
            kind=OfferKind.LICYTACJA,
            title=title[:400],
            description=description or None,
            price=parse_number(row.get("startPrice") or row.get("openingPrice")),
            opening_price=parse_number(row.get("startPrice") or row.get("openingPrice")),
            estimate_value=parse_number(row.get("estimateValue") or row.get("valuation")),
            deposit=parse_number(row.get("deposit") or row.get("bailment")),
            event_date=parse_datetime(row.get("startAt") or row.get("auctionStart")),
            deadline=parse_datetime(row.get("endAt") or row.get("auctionEnd")),
            case_number=clean(row.get("caseNumber") or row.get("signature") or "") or
            extract_case_number(description),
            authority=clean(self.dig(row, "bailiff", "name", default="") or row.get("office") or "") or None,
            seller_type=SellerType.KOMORNIK,
            location_text=clean(row.get("location") or self.dig(row, "address", "city", default="") or "")
            or None,
            city=clean(self.dig(row, "address", "city", default="") or "") or None,
            area=extract_area(f"{title} {description}"),
            property_type=guess_property_type(title, description),
            raw=row,
        )

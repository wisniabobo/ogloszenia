"""Monitor Sądowy i Gospodarczy — obwieszczenia syndyków o sprzedaży.

Wyszukiwarka MSiG ma publiczne API (zweryfikowane 19.09.2026):

    GET /api/Monitor/Search?textInBody=&from=YYYY-M-D&to=YYYY-M-D&page=1&…
    GET /api/Monitor/SearchCount?…   (te same parametry)

Odpowiedź: {"countPages": N, "page": 1, "list": [{id, monitorNumber,
dateOfPublication, entityName, …}]}.

Pełna treść obwieszczenia jest w numerze Monitora (PDF), nie w API — dlatego
zapisujemy to, co jest w wyniku wyszukiwania, i link do numeru. Filtrujemy po
frazach, które faktycznie zapowiadają sprzedaż nieruchomości z masy upadłości;
to najbardziej „okazyjne" źródło, bo syndyk sprzedaje pod presją czasu.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

from ..geo import detect_location
from ..models import OfferKind, SellerType, TransactionType
from ..utils.text import clean, extract_case_number, parse_datetime
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

BASE = "https://wyszukiwarka-msig.ms.gov.pl"

#: frazy, po których szukamy obwieszczeń o sprzedaży nieruchomości
PHRASES = [
    "sprzedaż nieruchomości",
    "sprzedaży nieruchomości",
    "przetarg na sprzedaż nieruchomości",
    "konkurs ofert nieruchomość",
]

#: jak daleko wstecz sięgamy przy pełnym przebiegu
DEFAULT_DAYS_BACK = 120


class MSiGScraper(BaseScraper):
    key = "msig"
    name = "Monitor Sądowy i Gospodarczy (syndycy)"
    base_url = BASE
    kind = OfferKind.LICYTACJA
    coverage = "krajowy"

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        from ..models import utcnow

        days_back = int(self.config.get("days_back", DEFAULT_DAYS_BACK))
        end = utcnow()
        start = end - timedelta(days=days_back)
        phrases = self.config.get("phrases") or PHRASES

        produced = 0
        seen: set[str] = set()

        for phrase in phrases:
            for page in range(1, ctx.max_pages + 1):
                if produced >= ctx.max_items:
                    return
                params = {
                    "entityName": "", "krs": "", "nip": "",
                    "textInPosition": "", "textInBody": phrase,
                    "signatureType": "A", "signatureOfCase": "", "signatureKRS": "",
                    "court": "",
                    "from": f"{start.year}-{start.month}-{start.day}",
                    "to": f"{end.year}-{end.month}-{end.day}",
                    "page": page,
                }
                try:
                    payload = await self.client.get_json(f"{BASE}/api/Monitor/Search", params=params)
                except Exception:
                    break
                rows = (payload or {}).get("list") or []
                if not rows:
                    break
                for row in rows:
                    item = self._parse(row, phrase)
                    if item is None or item.external_id in seen:
                        continue
                    seen.add(item.external_id)
                    yield item
                    produced += 1
                    if produced >= ctx.max_items:
                        return

    def _parse(self, row: dict, phrase: str) -> RawListing | None:
        entry_id = row.get("id")
        entity = clean(row.get("entityName") or "")
        if not entry_id or not entity:
            return None

        monitor = clean(row.get("monitorNumber") or "")
        published = parse_datetime(row.get("dateOfPublication"))
        title = f"Obwieszczenie MSiG {monitor}: {entity}"
        place = detect_location(entity)

        return RawListing(
            external_id=str(entry_id),
            url=f"{BASE}/search/ogloszenie/{entry_id}",
            source_key=self.key,
            kind=OfferKind.LICYTACJA,
            transaction=TransactionType.SPRZEDAZ,
            property_type=guess_property_type(entity, phrase),
            title=title[:400],
            description=(
                f"Obwieszczenie znalezione po frazie: {phrase}. "
                f"Podmiot: {entity}. Pełna treść w numerze {monitor} Monitora."
            ),
            published_at=published,
            seller_type=SellerType.SYNDYK,
            authority="Syndyk / sąd upadłościowy",
            case_number=clean(row.get("signatureOfCase") or "") or extract_case_number(entity),
            city=place.city,
            county=place.county,
            commune=place.commune,
            voivodeship=place.voivodeship,
            location_text=entity,
            extra={
                "monitor": monitor,
                "fraza": phrase,
                "krs": row.get("signatureKRS"),
                "pozycja": row.get("sequenceNumber"),
            },
            raw=row,
        )

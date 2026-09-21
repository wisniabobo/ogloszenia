"""Morizon.pl — portal ogłoszeniowy grupy Ringier.

Ten sam szablon kart co Gratka, więc i ten sam parser (`ringier.py`).
Różni się adresami sekcji i tym, że odnośnik do oferty prowadzi przez
„/oferta/".
"""

from __future__ import annotations

import re

from .ringier import RingierScraper

BASE = "https://www.morizon.pl"

OFFER_HREF = re.compile(r"/oferta/")

#: Sprawdzone 20.09.2026. „/nieruchomosci/{region}/" to zbiorcza lista
#: wszystkich typów — trzymamy ją jako siatkę bezpieczeństwa, gdyby serwis
#: przemianował którąś z sekcji szczegółowych.
SECTIONS = [
    {"path": "/mieszkania/{region}/", "transaction": "sprzedaz"},
    {"path": "/domy/{region}/", "transaction": "sprzedaz"},
    {"path": "/dzialki/{region}/", "transaction": "sprzedaz"},
    {"path": "/nieruchomosci/{region}/", "transaction": "sprzedaz"},
    {"path": "/do-wynajecia/mieszkania/{region}/", "transaction": "wynajem"},
]


class MorizonScraper(RingierScraper):
    key = "morizon"
    name = "Morizon.pl"
    base_url = BASE
    coverage = "krajowy"
    offer_href = OFFER_HREF

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, {"sections": SECTIONS} | (config or {}))

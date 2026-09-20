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

#: Sprawdzone 20.09.2026. „/nieruchomosci/opolskie/" to zbiorcza lista
#: wszystkich typów — trzymamy ją jako siatkę bezpieczeństwa, gdyby serwis
#: przemianował którąś z sekcji szczegółowych.
SECTIONS = [
    {"path": "/mieszkania/opolskie/", "transaction": "sprzedaz"},
    {"path": "/domy/opolskie/", "transaction": "sprzedaz"},
    {"path": "/dzialki/opolskie/", "transaction": "sprzedaz"},
    {"path": "/nieruchomosci/opolskie/", "transaction": "sprzedaz"},
    {"path": "/do-wynajecia/mieszkania/opolskie/", "transaction": "wynajem"},
]


class MorizonScraper(RingierScraper):
    key = "morizon"
    name = "Morizon.pl"
    base_url = BASE
    coverage = "krajowy"
    offer_href = OFFER_HREF

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, {"sections": SECTIONS} | (config or {}))

"""Gratka.pl — portal ogłoszeniowy grupy Ringier.

Parser kart siedzi w `ringier.py`, bo Morizon używa dokładnie tego samego
szablonu. Tutaj zostaje tylko to, co Gratkę wyróżnia: adres bazowy, kształt
odnośnika do oferty i nazwy sekcji.
"""

from __future__ import annotations

import re

from .ringier import RingierScraper

BASE = "https://gratka.pl"

#: Adres oferty w Gratce kończy się identyfikatorem po „/ob/".
OFFER_HREF = re.compile(r"/ob/\d+")

#: Sprawdzone 20.09.2026. Sekcji „garaże" serwis nie prowadzi dla regionu
#: (strona wstaje, ale jest pusta), a hal w ogóle nie ma w tym drzewie adresów.
SECTIONS = [
    {"path": "/nieruchomosci/mieszkania/{region}", "transaction": "sprzedaz"},
    {"path": "/nieruchomosci/domy/{region}", "transaction": "sprzedaz"},
    {"path": "/nieruchomosci/dzialki-grunty/{region}", "transaction": "sprzedaz"},
    {"path": "/nieruchomosci/lokale-uzytkowe/{region}", "transaction": "sprzedaz"},
    {"path": "/nieruchomosci/mieszkania/{region}/wynajem", "transaction": "wynajem"},
    {"path": "/nieruchomosci/domy/{region}/wynajem", "transaction": "wynajem"},
    {"path": "/nieruchomosci/lokale-uzytkowe/{region}/wynajem", "transaction": "wynajem"},
]


class GratkaScraper(RingierScraper):
    key = "gratka"
    name = "Gratka.pl"
    base_url = BASE
    coverage = "krajowy"
    offer_href = OFFER_HREF

    def __init__(self, client, config: dict | None = None, **kw) -> None:
        super().__init__(client, {"sections": SECTIONS} | (config or {}))

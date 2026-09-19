"""Klienci darmowych, publicznych API.

Wszystkie usługi w tym pakiecie działają **bez klucza i bez opłat** (poza
opcjonalnym Apify, które klucza wymaga). Każdy endpoint został sprawdzony na
żywo — daty weryfikacji są w docstringach poszczególnych modułów.
"""

from .gugik import GugikClient
from .nominatim import NominatimClient
from .overpass import OverpassClient

__all__ = ["GugikClient", "NominatimClient", "OverpassClient"]

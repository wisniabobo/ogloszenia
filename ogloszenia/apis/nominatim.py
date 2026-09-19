"""Nominatim (OpenStreetMap) — zapasowy geokoder. Darmowy, bez klucza.

Używamy go dopiero wtedy, gdy GUGiK nie zna adresu: dla polskich adresów
ewidencja jest dokładniejsza, ale OSM bywa lepszy przy nazwach potocznych
i obiektach ("dworzec PKP Opole Wschodnie").

Zasady użycia Nominatim wymagają identyfikującego się User-Agenta i **maks.
1 zapytania na sekundę** — klient sam pilnuje tego odstępu. Przy większych
wolumenach należy postawić własną instancję (ustawienie `OGL_NOMINATIM_URL`).

Sprawdzone 19.09.2026.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ..utils.http import HttpClient

DEFAULT_URL = "https://nominatim.openstreetmap.org/search"
#: publiczna instancja dopuszcza 1 zapytanie na sekundę
MIN_INTERVAL_S = 1.05


@dataclass(slots=True)
class OsmPlace:
    lat: float
    lon: float
    display_name: str
    osm_type: str | None = None
    osm_id: int | None = None
    category: str | None = None
    source: str = "nominatim"


class NominatimClient:
    def __init__(self, client: HttpClient, base_url: str = DEFAULT_URL) -> None:
        self.client = client
        self.base_url = base_url
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def search(self, query: str, *, limit: int = 1) -> OsmPlace | None:
        if not query or len(query) < 3:
            return None
        async with self._lock:
            wait = self._last_call + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            try:
                payload = await self.client.get_json(
                    self.base_url,
                    params={
                        "q": query,
                        "format": "json",
                        "limit": limit,
                        "countrycodes": "pl",
                        "addressdetails": 0,
                    },
                )
            except Exception:
                return None

        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        try:
            return OsmPlace(
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                display_name=row.get("display_name", ""),
                osm_type=row.get("osm_type"),
                osm_id=row.get("osm_id"),
                category=row.get("class"),
            )
        except (KeyError, TypeError, ValueError):
            return None

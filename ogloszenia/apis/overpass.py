"""Overpass API (OpenStreetMap) — co jest w okolicy nieruchomości. Darmowe.

Dla kupującego „500 m od szkoły i przystanku" bywa ważniejsze niż metraż.
Pobieramy najbliższe obiekty użytkowe wokół punktu i zapisujemy je przy ofercie,
dzięki czemu można filtrować po dostępności infrastruktury.

Publiczny serwer ma ostry limit (2 równoległe zapytania na IP), więc odpytujemy
go tylko dla ofert, które faktycznie oglądamy, i zawsze cache'ujemy wynik.

Sprawdzone 19.09.2026 (`/api/status` odpowiada).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..utils.http import HttpClient

DEFAULT_URL = "https://overpass-api.de/api/interpreter"

#: co uznajemy za istotne sąsiedztwo — klucz OSM -> etykieta po polsku
AMENITIES: dict[str, str] = {
    "amenity=school": "szkoła",
    "amenity=kindergarten": "przedszkole",
    "amenity=pharmacy": "apteka",
    "amenity=doctors": "przychodnia",
    "amenity=hospital": "szpital",
    "amenity=restaurant": "restauracja",
    "amenity=bank": "bank",
    "shop=supermarket": "supermarket",
    "shop=convenience": "sklep",
    "leisure=park": "park",
    "leisure=playground": "plac zabaw",
    "highway=bus_stop": "przystanek autobusowy",
    "railway=station": "stacja kolejowa",
    "railway=tram_stop": "przystanek tramwajowy",
}


@dataclass(slots=True)
class Poi:
    label: str
    name: str | None
    distance_m: int
    lat: float
    lon: float


@dataclass(slots=True)
class Surroundings:
    pois: list[Poi] = field(default_factory=list)

    def nearest(self, label: str) -> Poi | None:
        matches = [p for p in self.pois if p.label == label]
        return min(matches, key=lambda p: p.distance_m) if matches else None

    def summary(self) -> dict[str, int]:
        """{'szkoła': 320, 'przystanek autobusowy': 150, …} — metry do najbliższego."""
        out: dict[str, int] = {}
        for poi in self.pois:
            if poi.label not in out or poi.distance_m < out[poi.label]:
                out[poi.label] = poi.distance_m
        return dict(sorted(out.items(), key=lambda kv: kv[1]))


class OverpassClient:
    def __init__(self, client: HttpClient, base_url: str = DEFAULT_URL) -> None:
        self.client = client
        self.base_url = base_url
        self._gate = asyncio.Semaphore(1)  # publiczny serwer nie lubi równoległości

    async def around(self, lat: float, lon: float, radius_m: int = 1000) -> Surroundings:
        parts = []
        for selector in AMENITIES:
            key, value = selector.split("=")
            parts.append(f'node["{key}"="{value}"](around:{radius_m},{lat},{lon});')
            parts.append(f'way["{key}"="{value}"](around:{radius_m},{lat},{lon});')
        query = f"[out:json][timeout:25];({''.join(parts)});out center 60;"

        async with self._gate:
            try:
                payload = await self.client.request(
                    self.base_url, method="POST", data={"data": query}, retries=1
                )
                data = payload.json()
            except Exception:
                return Surroundings()

        pois: list[Poi] = []
        for element in data.get("elements", []):
            center = element.get("center") or element
            plat, plon = center.get("lat"), center.get("lon")
            if plat is None or plon is None:
                continue
            tags = element.get("tags") or {}
            label = next(
                (
                    name
                    for selector, name in AMENITIES.items()
                    for key, value in [selector.split("=")]
                    if tags.get(key) == value
                ),
                None,
            )
            if not label:
                continue
            pois.append(
                Poi(
                    label=label,
                    name=tags.get("name"),
                    distance_m=int(_haversine_m(lat, lon, plat, plon)),
                    lat=plat,
                    lon=plon,
                )
            )
        pois.sort(key=lambda p: p.distance_m)
        return Surroundings(pois=pois[:120])


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    r = 6371000.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))

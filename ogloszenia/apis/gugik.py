"""GUGiK — Główny Urząd Geodezji i Kartografii. Darmowo, bez klucza.

Dwie usługi, obie sprawdzone 19.09.2026:

**UUG (Usługa Uniwersalnego Geokodowania)** — `services.gugik.gov.pl/uug/`
Najlepszy geokoder dla polskich adresów: zna punkty adresowe z ewidencji, więc
trafia tam, gdzie OpenStreetMap ma dziurę. Zwraca przy okazji kody TERYT, SIMC
i ULIC, czyli twarde identyfikatory gminy, miejscowości i ulicy.

**ULDK (Usługa Lokalizacji Działek Katastralnych)** — `uldk.gugik.gov.pl`
Zwraca działkę ewidencyjną po współrzędnych albo po identyfikatorze, razem
z geometrią. To jest rzecz, której nie ma żaden portal ogłoszeniowy: dla
licytacji komorniczej z podanym numerem działki dostajemy jej rzeczywisty
kształt i powierzchnię.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..utils.http import HttpClient

UUG = "https://services.gugik.gov.pl/uug/"
ULDK = "https://uldk.gugik.gov.pl/"


@dataclass(slots=True)
class GeocodeResult:
    lat: float
    lon: float
    city: str | None = None
    street: str | None = None
    number: str | None = None
    postal_code: str | None = None
    teryt: str | None = None          # identyfikator gminy
    simc: str | None = None           # identyfikator miejscowości
    ulic: str | None = None           # identyfikator ulicy
    precision: str = "city"           # address | street | city
    source: str = "gugik"

    @property
    def is_exact(self) -> bool:
        return self.precision == "address"


@dataclass(slots=True)
class Parcel:
    identifier: str
    teryt: str | None
    geometry_wkt: str | None
    region: str | None = None


class GugikClient:
    """Cienka warstwa nad usługami GUGiK."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    # ------------------------------------------------------------------ #
    async def geocode(
        self,
        *,
        city: str | None,
        street: str | None = None,
        number: str | None = None,
        district: str | None = None,
        voivodeship: str | None = None,
        teryt_prefix: str | None = None,
    ) -> GeocodeResult | None:
        """Geokoduje adres, schodząc kaskadowo na mniej dokładny poziom.

        Numer domu bywa w ogłoszeniu wymyślony albo nieistniejący w ewidencji,
        dlatego przy pustej odpowiedzi próbujemy samej ulicy, a potem samej
        miejscowości — lepiej mieć punkt z dokładnością do ulicy niż nic.

        `teryt_prefix` to zabezpieczenie przed nazwami, które w Polsce
        powtarzają się w kilku województwach. Bez niego „Opole" potrafi
        wylądować w **Opolu Lubelskim** — a to 300 km od celu. Wynik spoza
        oczekiwanego województwa odrzucamy i próbujemy dalej.
        """
        if not city:
            return None

        # UWAGA: nie dopisujemy tu województwa. Sprawdzone na żywym API —
        # „Opole, Wrocławska" zwraca trafienie, a „Opole, Wrocławska, opolskie"
        # zwraca ZERO wyników. Dopisek powodował, że pierwsza próba zawsze
        # przepadała i każdy adres kosztował dwa zapytania zamiast jednego.
        # Region pilnuje `teryt_prefix`, sprawdzany na wszystkich trafieniach.
        attempts: list[tuple[str, str]] = []
        if street and number:
            attempts.append((f"{city}, {street} {number}", "address"))
        if street:
            attempts.append((f"{city}, {street}", "street"))
        if district and district.lower() != (city or "").lower():
            # dzielnica jest dokładniejsza niż środek miasta — Zaodrze to nie centrum
            attempts.append((f"{city}, {district}", "district"))
        attempts.append((city, "city"))

        for address, precision in attempts:
            results = await self._query_uug(address, precision)
            if not results:
                continue
            if teryt_prefix:
                # GUGiK oddaje do 15 dopasowań — bierzemy pierwsze z właściwego
                # województwa, zamiast ślepo ufać pierwszemu z listy
                match = next(
                    (r for r in results if r.teryt and r.teryt.startswith(teryt_prefix)), None
                )
                if match is not None:
                    return match
                continue
            return results[0]
        return None

    async def _query_uug(self, address: str, precision: str) -> list[GeocodeResult]:
        """Zwraca **wszystkie** dopasowania — wybór właściwego robi `geocode()`."""
        try:
            payload = await self.client.get_json(
                UUG, params={"request": "GetAddress", "address": address, "srid": "4326"}
            )
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []
        results = payload.get("results") or {}
        if not isinstance(results, dict):
            return []

        found: list[GeocodeResult] = []
        for row in results.values():
            if not isinstance(row, dict):
                continue
            lat, lon = _to_float(row.get("y")), _to_float(row.get("x"))
            if lat is None or lon is None:
                continue
            found.append(
                GeocodeResult(
                    lat=lat,
                    lon=lon,
                    city=row.get("city"),
                    street=row.get("street"),
                    number=row.get("number"),
                    postal_code=row.get("code"),
                    teryt=row.get("teryt"),
                    simc=row.get("simc"),
                    ulic=row.get("ulic"),
                    precision=str(payload.get("type") or precision),
                    source="gugik",
                )
            )
        return found

    async def reverse(self, lat: float, lon: float, radius: int = 200) -> GeocodeResult | None:
        """Adres najbliższy podanemu punktowi (WGS84)."""
        try:
            payload = await self.client.get_json(
                UUG,
                params={
                    "request": "GetAddressReverse",
                    "location": f"POINT({lon} {lat})",
                    "srid": "4326",
                    "radius": radius,
                },
            )
        except Exception:
            return None
        results = (payload or {}).get("results") or {}
        first = results.get("1") if isinstance(results, dict) else None
        if not isinstance(first, dict):
            return None
        return GeocodeResult(
            lat=lat,
            lon=lon,
            city=first.get("city"),
            street=first.get("street"),
            number=first.get("number"),
            postal_code=first.get("code"),
            teryt=first.get("teryt"),
            simc=first.get("simc"),
            precision="address",
            source="gugik-reverse",
        )

    # ------------------------------------------------------------------ #
    async def parcel_by_id(self, identifier: str) -> Parcel | None:
        """Działka ewidencyjna po identyfikatorze, np. `160105_2.0012.345/6`."""
        return await self._parcel({"request": "GetParcelById", "id": identifier})

    async def parcel_by_point(self, lat: float, lon: float) -> Parcel | None:
        """Działka, w której leży punkt (WGS84)."""
        return await self._parcel(
            {"request": "GetParcelByXY", "xy": f"{lon},{lat},4326"}
        )

    async def _parcel(self, params: dict[str, Any]) -> Parcel | None:
        params = params | {"result": "id,teryt,geom_wkt,region"}
        try:
            raw = await self.client.get_text(ULDK, params=params)
        except Exception:
            return None
        lines = [ln for ln in (raw or "").splitlines() if ln.strip()]
        if not lines or not lines[0].startswith("0"):
            return None  # pierwsza linia to status: 0 = ok, -1 = brak wyniku
        payload = lines[0][1:].strip() if len(lines) == 1 else lines[1]
        fields = payload.split("|")
        wkt = next((f for f in fields if "POLYGON" in f or "POINT" in f), None)
        identifier = next((f for f in fields if re.match(r"^\d{6}_", f)), fields[0] if fields else "")
        return Parcel(
            identifier=identifier,
            teryt=next((f for f in fields if re.fullmatch(r"\d{6,7}", f)), None),
            geometry_wkt=wkt,
            region=fields[-1] if len(fields) > 2 else None,
        )


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None

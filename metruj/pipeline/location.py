"""Ustalenie, gdzie właściwie leży nieruchomość.

Najczęstsze i najkosztowniejsze błędy tego serwisu były błędami lokalizacji:
garaż „w Krakowie" lądował we wsi Miejsce pod Namysłowem, a działka spod
Starogardu Gdańskiego w gminie Dąbrowa. Obie pomyłki miały tę samą przyczynę —
nazwa miejscowości była *zgadywana z tekstu* i wygrywała ta, która akurat
wypadła lepiej w punktacji.

Teraz obowiązuje porządek źródeł, od najmocniejszego:

1. **Pole portalu.** OLX i Otodom podają województwo, miasto i dzielnicę
   w osobnych polach — nie ma powodu czytać tego z tytułu.
2. **Rejestr TERYT.** Nazwę z portalu dopasowujemy do rejestru i dobieramy do
   niej powiat oraz gminę. Przy nazwie powtarzającej się w kraju („Opole",
   „Świerczów") rozstrzyga województwo podane przez portal.
3. **Tekst ogłoszenia** — dopiero gdy portal nie podał nic. Tu obowiązują
   ostre zasady z `geo.detect`: nazwa niebędąca miastem musi mieć przy sobie
   wskazówkę („w miejscowości X", „gm. X") albo zgadzać się z rozpoznanym
   powiatem.
4. **Geokoder GUGiK** — poprawia resztę już po zapisie (patrz `pipeline.geocode`):
   odpowiada kodem TERYT z ewidencji, więc jego zdanie jest ostateczne.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..geo import (
    detect_location,
    extract_street,
    known_voivodeship,
    lookup,
    normalize_street,
    resolve_place,
)
from ..scrapers.base import RawListing
from ..utils.text import clean


@dataclass(slots=True)
class Location:
    voivodeship: str | None = None
    county: str | None = None
    commune: str | None = None
    city: str | None = None
    district: str | None = None
    street: str | None = None
    teryt: str | None = None
    #: skąd pochodzi miejscowość: portal | rejestr | tekst | brak
    origin: str = "brak"

    @property
    def known(self) -> bool:
        return bool(self.city or self.county or self.voivodeship)

    def as_dict(self) -> dict:
        return {
            "voivodeship": self.voivodeship,
            "county": self.county,
            "commune": self.commune,
            "city": self.city,
            "district": self.district,
            "street": self.street,
            "teryt": self.teryt,
        }


def _first(*values: str | None) -> str | None:
    for value in values:
        text = clean(value or "")
        if text:
            return text
    return None


def _voivodeship_from_county(county: str | None) -> str | None:
    """Powiat jednoznacznie wskazuje województwo, o ile jego nazwa jest unikalna."""
    if not county:
        return None
    hits = [u for u in lookup(county) if u.kind == "powiat"]
    regions = {u.voivodeship for u in hits}
    return regions.pop() if len(regions) == 1 else None


def resolve(raw: RawListing) -> Location:
    """Ustala lokalizację jednej surowej oferty."""
    result = Location()

    # --- 1. województwo: portal wie najlepiej -------------------------- #
    result.voivodeship = known_voivodeship(raw.voivodeship) or _voivodeship_from_county(
        clean(raw.county or "")
    )

    # --- 2. miejscowość z pola portalu --------------------------------- #
    portal_city = clean(raw.city or "")
    # Portal potrafi wstawić w pole miejscowości nazwę województwa — dzieje się
    # to zawsze, gdy wyszukiwanie obejmuje cały region, a karta nie podaje nic
    # dokładniejszego. Taki wpis nie jest miejscowością i nie może nią zostać:
    # wszystkie oferty z regionu dostawały wtedy ten sam odcisk parametrów
    # i zlewały się w jedną ofertę.
    as_region = known_voivodeship(portal_city)
    if as_region:
        result.voivodeship = result.voivodeship or as_region
        portal_city = ""
    if portal_city:
        matched = resolve_place(portal_city, voivodeship_hint=result.voivodeship)
        result.city = matched.get("city") or portal_city
        result.commune = matched.get("commune")
        result.county = matched.get("county")
        result.voivodeship = result.voivodeship or matched.get("voivodeship")
        result.teryt = matched.get("teryt")
        result.origin = "portal" if not matched else "rejestr"

    # --- 3. z tekstu, ale tylko gdy portal milczy ---------------------- #
    if not result.city:
        for text in (raw.title, raw.location_text, (raw.description or "")[:1500]):
            found = detect_location(text, voivodeship_hint=result.voivodeship)
            if found.city:
                result.city = found.city
                result.commune = result.commune or found.commune
                result.county = result.county or found.county
                result.voivodeship = result.voivodeship or found.voivodeship
                result.teryt = result.teryt or found.teryt
                result.origin = "tekst"
                break
            # sama wskazówka administracyjna też jest coś warta
            result.voivodeship = result.voivodeship or found.voivodeship
            result.county = result.county or found.county

    # --- 4. pola, które portal podaje wprost --------------------------- #
    result.county = _first(raw.county, result.county)
    result.commune = _first(raw.commune, result.commune)
    result.district = _first(raw.district)
    result.street = normalize_street(
        _first(
            raw.street,
            extract_street(raw.title),
            extract_street(raw.location_text or ""),
            extract_street((raw.description or "")[:600]) if raw.street_from_body else None,
        )
    )
    return result

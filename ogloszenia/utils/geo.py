"""Słownik geograficzny województwa opolskiego + rozpoznawanie lokalizacji w tekście."""

from __future__ import annotations

import functools
import re

from ..settings import regions_config
from .text import clean, norm_key

VOIVODESHIP = "opolskie"

# Dzielnice i osiedla Opola (oraz sołectwa w granicach miasta po 2017 r.)
OPOLE_DISTRICTS = [
    "Śródmieście", "Stare Miasto", "Pasieka", "Zaodrze", "Chabry", "Armii Krajowej",
    "Malinka", "Nadodrze", "Kolonia Gosławicka", "Gosławice", "Grudzice", "Groszowice",
    "Nowa Wieś Królewska", "Bierkowice", "Szczepanowice", "Wójtowa Wieś", "Półwieś",
    "Zakrzów", "Grotowice", "Wróblin", "Brzezie", "Świerkle", "Czarnowąsy", "Borki",
    "Winów", "Chmielowice", "Żerkowice", "Sławice", "Wrzoski", "Krzanowice", "Malina",
    "Zawada", "Luboszyce", "Wrzoski",
]


@functools.lru_cache(maxsize=1)
def region_index() -> dict[str, dict]:
    """Mapa: znormalizowana nazwa miejscowości -> {city, commune, county}."""
    cfg = regions_config()
    index: dict[str, dict] = {}
    for county in cfg.get("powiaty", []):
        county_name = county.get("nazwa", "")
        for commune in county.get("gminy", []):
            commune_name = commune if isinstance(commune, str) else commune.get("nazwa", "")
            places = [commune_name] if isinstance(commune, str) else commune.get("miejscowosci", [])
            for place in {commune_name, *places}:
                if not place:
                    continue
                index[norm_key(place)] = {
                    "city": place,
                    "commune": commune_name,
                    "county": county_name,
                    "voivodeship": VOIVODESHIP,
                }
    return index


@functools.lru_cache(maxsize=1)
def counties() -> list[str]:
    return [c.get("nazwa", "") for c in regions_config().get("powiaty", [])]


@functools.lru_cache(maxsize=1)
def all_cities() -> list[str]:
    return sorted({v["city"] for v in region_index().values()})


def resolve_place(name: str | None) -> dict | None:
    """Dopasowuje nazwę miejscowości do słownika woj. opolskiego."""
    if not name:
        return None
    key = norm_key(name)
    if not key:
        return None
    hit = region_index().get(key)
    if hit:
        return dict(hit)
    # dopasowanie po pierwszym członie ("Kędzierzyn-Koźle, Śródmieście")
    head = norm_key(re.split(r"[,/(]", name)[0])
    return dict(region_index().get(head)) if region_index().get(head) else None


def detect_location(text: str | None) -> dict:
    """Wyszukuje w dowolnym tekście miejscowość z woj. opolskiego.

    Zwraca {} gdy nic nie znaleziono (oferta prawdopodobnie spoza regionu).
    """
    result: dict = {}
    if not text:
        return result
    key = f" {norm_key(text)} "
    for place_key, meta in region_index().items():
        if len(place_key) < 4:
            continue
        if f" {place_key} " in key:
            result = dict(meta)
            if meta["city"].lower() == "opole":
                district = detect_opole_district(text)
                if district:
                    result["district"] = district
            break
    return result


def detect_opole_district(text: str | None) -> str | None:
    if not text:
        return None
    key = f" {norm_key(text)} "
    for district in OPOLE_DISTRICTS:
        if f" {norm_key(district)} " in key:
            return district
    return None


def is_in_opolskie(*texts: str | None) -> bool:
    joined = " ".join(clean(t) for t in texts if t)
    if not joined:
        return False
    if "opolsk" in norm_key(joined):
        return True
    return bool(detect_location(joined))


STREET_RE = re.compile(
    r"\b(?:ul\.?|ulica|al\.?|aleja|aleje|os\.?|osiedle|pl\.?|plac|rynek)\s+"
    r"([A-ZŻŹĆĄŚĘŁÓŃ][\w.\-]*(?:\s+(?:[A-ZŻŹĆĄŚĘŁÓŃ][\w.\-]*|i|de|von|im))*)",
    re.UNICODE,
)


def extract_street(text: str | None) -> str | None:
    if not text:
        return None
    m = STREET_RE.search(text)
    if not m:
        return None
    street = clean(m.group(1))
    street = re.split(r"\s+(?:w|we|k/|koło|obok)\s+", street)[0]
    return street[:120] or None

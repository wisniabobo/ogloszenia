"""Geografia: krajowy rejestr TERYT, słownik miejscowości, adresy.

Cały moduł działa dla **całej Polski**. Wcześniej lokalizacja opierała się na
ręcznie spisanym słowniku jednego województwa, więc każde ogłoszenie spoza
niego albo odpadało, albo lądowało w przypadkowej wsi o podobnej nazwie.
"""

from .detect import (
    Detected,
    detect_location,
    detect_voivodeship,
    known_voivodeship,
    resolve_place,
)
from .gazetteer import find_places, lookup, town_names
from .streets import extract_street, normalize_street, split_house_number
from .teryt import (
    POLAND_BBOX,
    Unit,
    bboxes,
    by_teryt,
    counties,
    counties_of,
    in_poland,
    in_voivodeship,
    parse_jednostka,
    towns,
    units,
    voivodeship_of,
    voivodeships,
    voivodeships_at,
)

__all__ = [
    "POLAND_BBOX",
    "Detected",
    "Unit",
    "bboxes",
    "by_teryt",
    "counties",
    "counties_of",
    "detect_location",
    "detect_voivodeship",
    "extract_street",
    "find_places",
    "in_poland",
    "in_voivodeship",
    "voivodeships_at",
    "known_voivodeship",
    "lookup",
    "normalize_street",
    "parse_jednostka",
    "resolve_place",
    "split_house_number",
    "town_names",
    "towns",
    "units",
    "voivodeship_of",
    "voivodeships",
]

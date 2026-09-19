"""Słownik geograficzny województwa opolskiego + rozpoznawanie lokalizacji w tekście."""

from __future__ import annotations

import functools
import re

from ..settings import regions_config
from .text import clean, deaccent, norm_key

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


#: maksymalna długość polskiej końcówki fleksyjnej dopuszczanej przy dopasowaniu
MAX_INFLECTION = 3


@functools.lru_cache(maxsize=1)
def ambiguous_names() -> set[str]:
    """Nazwy miejscowości, które są jednocześnie zwykłymi polskimi słowami.

    W woj. opolskim są wsie o nazwach „Pokój", „Dzielnica", „Dobra", „Sucha"
    czy „Rogi". Bez ostrożności ogłoszenie „mieszkanie 3 pokoje" trafiłoby do
    gminy Pokój. Takie nazwy uznajemy za lokalizację tylko wtedy, gdy w tekście
    występują z wielkiej litery — tak jak nazwy własne.
    """
    from ..settings import regions_config

    configured = regions_config().get("nazwy_wieloznaczne", [])
    return {norm_key(name) for name in configured}


@functools.lru_cache(maxsize=2048)
def _pattern_for(name: str) -> re.Pattern[str] | None:
    r"""Wzorzec dopasowujący nazwę w dowolnym przypadku gramatycznym.

    Każdy człon nazwy jest skracany o końcową literę i może przyjąć dowolną
    końcówkę do MAX_INFLECTION znaków, bo w polskim odmieniają się wszystkie
    człony naraz: „Strzelce Opolskie" -> „w Strzelcach Opolskich",
    „Kędzierzyn-Koźle" -> „w Kędzierzynie-Koźlu".
    """
    tokens = [t for t in re.split(r"[\s\-]+", deaccent(name)) if t]
    if not tokens:
        return None
    parts = []
    for token in tokens:
        stem = token[:-1] if len(token) > 4 else token
        ending = rf"(\w{{0,{MAX_INFLECTION}}})" if len(token) > 4 else "()"
        parts.append(re.escape(stem) + ending)
    if len(tokens) == 1 and len(tokens[0]) < 4:
        return None
    return re.compile(r"\b" + r"[\s\-]+".join(parts) + r"\b", re.IGNORECASE)


def _score_match(name: str, text_deacc: str, *, is_seat: bool) -> int | None:
    """Ocenia trafienie nazwy w tekście. `None` = brak wiarygodnego trafienia.

    Punktujemy dokładność (bez końcówki), pisownię wielką literą i to, czy
    miejscowość jest siedzibą gminy — dzięki temu „Opola" wygrywa z przypadkową
    wioską o krótkiej nazwie.
    """
    pattern = _pattern_for(name)
    if pattern is None:
        return None
    ambiguous = norm_key(name) in ambiguous_names()
    best: int | None = None
    for match in pattern.finditer(text_deacc):
        capitalized = match.group(0)[0].isupper()
        if ambiguous and not capitalized:
            continue
        score = len(norm_key(name))
        if not any(match.groups()):
            score += 20                      # trafienie dokładne, bez odmiany
        if capitalized:
            score += 15
        if is_seat:
            score += 8
        best = score if best is None else max(best, score)
    return best


def detect_location(text: str | None) -> dict:
    """Wyszukuje w tekście miejscowość z woj. opolskiego.

    Zamiast brać pierwsze trafienie, zbiera wszystkich kandydatów i wybiera
    najlepiej punktowanego. Zwraca {}, gdy nic nie pasuje (oferta spoza regionu).
    """
    if not text:
        return {}
    text_deacc = deaccent(text)
    best_score = 0
    best_meta: dict | None = None

    for _, meta in region_index().items():
        city = meta["city"]
        score = _score_match(city, text_deacc, is_seat=(city == meta.get("commune")))
        if score is not None and score > best_score:
            best_score, best_meta = score, meta

    if best_meta is None:
        return {}
    result = dict(best_meta)
    if result["city"].lower() == "opole":
        district = detect_opole_district(text)
        if district:
            result["district"] = district
    return result


def detect_opole_district(text: str | None) -> str | None:
    if not text:
        return None
    text_deacc = deaccent(text)
    best_score, best_name = 0, None
    for district in OPOLE_DISTRICTS:
        score = _score_match(district, text_deacc, is_seat=False)
        if score is not None and score > best_score:
            best_score, best_name = score, district
    return best_name


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


#: słowa, po których nazwa ulicy na pewno się skończyła
_STREET_STOP = re.compile(
    r"\s+(?:w|we|k/|koło|obok|tel|telefon|kontakt|cena|pow|powierzchnia|nr|oferta)\b",
    re.I,
)


def extract_street(text: str | None) -> str | None:
    """Wyciąga nazwę ulicy z tekstu typu „ul. Leona Powolnego. Kontakt 500…".

    Nazwa kończy się na kropce zdania, przecinku, myślniku albo słowie, które
    otwiera kolejną informację — bez tego do nazwy wchodziło pół opisu.
    """
    if not text:
        return None
    m = STREET_RE.search(text)
    if not m:
        return None
    street = clean(m.group(1))
    street = re.split(r"\.\s+|\s+[–—]\s+|\s+-\s+|[,;)]", street)[0]
    street = _STREET_STOP.split(street)[0]
    street = street.strip(" .,-–—")
    if len(street) < 3 or street.isdigit():
        return None
    return street[:120]

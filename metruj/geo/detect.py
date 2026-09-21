"""Rozpoznawanie lokalizacji w tekście ogłoszenia — dla całej Polski.

Poprzednia wersja brała z tekstu *najlepiej punktowaną* nazwę ze słownika
jednego województwa. Przy słowniku krajowym taka metoda rozsypuje się od razu:
ogłoszenie „**Miejsce** postojowe w garażu podziemnym w **Krakowie**" trafiało
do wsi Miejsce w powiecie namysłowskim, bo „Miejsce" jest nazwą wsi i stało
w tytule wcześniej niż Kraków.

Stąd dwie zasady, które tu obowiązują:

1. **Nazwa bez wskazówki liczy się tylko wtedy, gdy jest miastem.** Wieś albo
   siedziba małej gminy musi mieć przy sobie wskazówkę („w miejscowości X",
   „gmina X", „pow. X") albo zgadzać się z rozpoznanym powiatem.
2. **Wskazówka administracyjna bije wszystko.** „woj. opolskie", „powiat nyski",
   „gmina Świerczów" to informacja wpisana wprost przez człowieka — nie ma
   powodu jej przegłosowywać statystyką.

Tekstu używamy dopiero wtedy, gdy portal nie podał lokalizacji w osobnym polu.
Dla ogłoszeń z portali (95% zasobu) ta ścieżka w ogóle się nie uruchamia.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from ..settings import load_yaml
from ..utils.text import clean, deaccent
from . import gazetteer
from .teryt import Unit, units, voivodeships

#: Wskazówki, po których w polskim ogłoszeniu stoi nazwa miejscowości.
#: „w miejscowości Dąbrówka", „gm. Świerczów", „obręb Grudzice".
PLACE_CUES = re.compile(
    r"(?:w\s+miejscowo\w+|miejscowo\w+|we?\s+wsi|w\s+mie[śs]cie|sołectw\w+|obr[ęe]b\w*"
    r"|po[łl]o[żz]on\w*\s+w|zlokalizowan\w*\s+w|adres\w*[:\s])\s*[:\-]?\s*",
    re.I,
)
COMMUNE_CUE = re.compile(r"\bgm(?:\.|ina|iny|inie|inę)?\s+", re.I)
COUNTY_CUE = re.compile(r"\bpow(?:\.|iat|iatu|iecie)?\s+", re.I)
VOIVODESHIP_CUE = re.compile(r"\bwoj(?:\.|ew[óo]dztw\w*)\s+", re.I)

#: Nazwy województw w odmianie: „opolskiego", „mazowieckim", „śląskie".
_VOIV_STEMS = {v[:-2]: v for v in voivodeships()} if voivodeships() else {}


@functools.lru_cache(maxsize=1)
def ambiguous_names() -> set[str]:
    """Nazwy miejscowości będące zwykłymi polskimi słowami (config/…yaml)."""
    configured = load_yaml("nazwy_wieloznaczne.yaml").get("nazwy", [])
    return {deaccent(str(name)).lower() for name in configured}


def is_ambiguous(name: str) -> bool:
    return deaccent(name).lower() in ambiguous_names()


#: Ile dokłada każde kolejne wystąpienie tej samej nazwy w tekście.
#: Nazwa powtórzona to przesłanka, nie zbieg okoliczności.
REPEAT_BONUS = 12


@dataclass
class Detected:
    """Co udało się odczytać z tekstu."""

    voivodeship: str | None = None
    county: str | None = None
    commune: str | None = None
    city: str | None = None
    teryt: str | None = None
    #: skąd wzięła się miejscowość — do oceny wiarygodności
    basis: str = ""
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            k: v
            for k, v in {
                "voivodeship": self.voivodeship,
                "county": self.county,
                "commune": self.commune,
                "city": self.city,
                "teryt": self.teryt,
            }.items()
            if v
        }

    def __bool__(self) -> bool:
        return bool(self.city or self.county or self.commune or self.voivodeship)


def detect_voivodeship(text: str | None) -> str | None:
    """Nazwa województwa podana wprost: „woj. opolskie", „w województwie śląskim".

    Sama nazwa gdziekolwiek w treści to za mało — stopki portali ogólnopolskich
    wypisują wszystkie szesnaście naraz, przez co każde ogłoszenie „było"
    z każdego województwa. Wymagamy więc skrótu albo słowa „województwo" tuż
    przed nazwą.
    """
    if not text:
        return None
    for match in VOIVODESHIP_CUE.finditer(text):
        tail = deaccent(text[match.end() : match.end() + 24]).lower()
        for stem, name in _VOIV_STEMS.items():
            if tail.startswith(deaccent(stem).lower()):
                return name
    return None


def _cue_positions(text: str, pattern: re.Pattern[str]) -> list[int]:
    return [m.end() for m in pattern.finditer(text)]


def _near(position: int, cues: list[int], window: int = 3) -> bool:
    """Czy nazwa zaczyna się tuż za wskazówką (dopuszczamy spację i „ ")."""
    return any(0 <= position - cue <= window for cue in cues)


def detect_location(
    text: str | None,
    *,
    voivodeship_hint: str | None = None,
) -> Detected:
    """Odczytuje z tekstu województwo, powiat, gminę i miejscowość."""
    result = Detected()
    if not text:
        return result
    text = clean(text)

    # --- 1. wskazówki administracyjne: wpisane wprost, więc rozstrzygają ---
    result.voivodeship = detect_voivodeship(text) or voivodeship_hint

    place_cues = _cue_positions(text, PLACE_CUES)
    commune_cues = _cue_positions(text, COMMUNE_CUE)
    county_cues = _cue_positions(text, COUNTY_CUE)

    matches = gazetteer.find_places(text)
    if not matches:
        return result

    # --- 2. powiat i gmina podane wprost ---
    for match in matches:
        if _near(match.start, county_cues) and match.unit.county:
            result.county = match.unit.county
            result.voivodeship = result.voivodeship or match.unit.voivodeship
        if _near(match.start, commune_cues) and match.unit.commune:
            result.commune = match.unit.commune
            result.county = result.county or match.unit.county
            result.voivodeship = result.voivodeship or match.unit.voivodeship

    # --- 3. miejscowość ---
    best: tuple[int, Unit] | None = None
    # Punktujemy **jednostki**, nie pojedyncze trafienia: nazwa powtórzona
    # w ogłoszeniu kilka razy jest mocniejszą przesłanką niż wspomniana raz.
    # Bez tego tytuł „mieszkanie w Starogardzie Gdańskim: Starogard Gdański:
    # Gdańska" trafiał do Gdańska — bo nazwa ulicy „Gdańska" dopasowywała się
    # do miasta o wyższej randze, a ranga ważyła więcej niż dwa wystąpienia
    # nazwy właściwej.
    scores: dict[str, tuple[int, Unit, str]] = {}
    for match in matches:
        unit = match.unit
        if unit.kind == "powiat":
            continue
        cued = _near(match.start, place_cues) or _near(match.start, commune_cues)
        agrees_county = bool(result.county and unit.county == result.county)
        agrees_voiv = bool(result.voivodeship and unit.voivodeship == result.voivodeship)

        # Nazwa, która nie jest miastem i nie ma przy sobie żadnej wskazówki,
        # to najczęściej zwykłe polskie słowo — „Miejsce", „Pokój", „Dobra".
        # Miasto o takiej nazwie (gmina Dobra, gmina Nowe) też musi mieć
        # wskazówkę: „Dobra oferta" zaczyna się wielką literą tak samo jak
        # nazwa własna i po samej pisowni nie da się ich rozróżnić.
        town = unit.town and not is_ambiguous(unit.name)
        if not (town or cued or agrees_county or agrees_voiv):
            result.candidates.append(unit.name)
            continue
        if not match.capitalized and not cued:
            continue

        # Ranga ma rozstrzygać remisy między miejscami o tej samej nazwie,
        # a nie przebijać to, co w tekście stoi wprost — stąd dzielona.
        score = (unit.rank if town else min(unit.rank, 30)) // 5
        if cued:
            score += 60
        if agrees_county:
            score += 40
        elif agrees_voiv:
            score += 25
        if match.exact:
            score += 10
        if match.start < 60:
            score += 5          # nazwa w tytule, a nie w połowie opisu
        basis = "wskazówka" if cued else ("miasto" if town else "zgodność z regionem")

        key = unit.teryt
        previous = scores.get(key)
        if previous is None:
            scores[key] = (score, unit, basis)
        else:
            # kolejne wystąpienie tej samej nazwy dokłada pewności
            scores[key] = (max(previous[0], score) + REPEAT_BONUS, unit, previous[2])

    if scores:
        score, unit, basis = max(scores.values(), key=lambda entry: entry[0])
        best = (score, unit)
        result.basis = basis

    if best is not None:
        unit = best[1]
        result.city = unit.name
        result.commune = result.commune or unit.commune
        result.county = result.county or unit.county
        result.voivodeship = result.voivodeship or unit.voivodeship
        result.teryt = unit.gmina_teryt
    return result


def resolve_place(name: str | None, *, voivodeship_hint: str | None = None) -> dict:
    """Dopasowuje gotową nazwę miejscowości (z pola portalu) do rejestru TERYT.

    Portal podaje samą nazwę — „Nysa", „Opole" — a my chcemy do niej powiat
    i województwo. Przy nazwie powtarzającej się w kraju rozstrzyga podpowiedź
    z portalu, a gdy jej nie ma — jednostka o wyższej randze (miasto przed wsią).
    """
    hits = gazetteer.lookup(name)
    if not hits:
        return {}
    if voivodeship_hint:
        matching = [u for u in hits if u.voivodeship == voivodeship_hint]
        if matching:
            hits = matching
    unit = hits[0]
    if len(hits) > 1 and not voivodeship_hint and unit.rank < 70:
        # kilka wsi o tej samej nazwie i nic, co by je rozróżniało —
        # lepiej oddać samą nazwę niż wylosować powiat
        return {"city": unit.name}
    return {
        "city": unit.name,
        "commune": unit.commune,
        "county": unit.county,
        "voivodeship": unit.voivodeship,
        "teryt": unit.gmina_teryt,
    }


def known_voivodeship(name: str | None) -> str | None:
    """Normalizuje nazwę województwa („Opolskie", „OPOLSKIE") do postaci z rejestru."""
    if not name:
        return None
    key = deaccent(name).strip().lower()
    for value in voivodeships():
        if deaccent(value).lower() == key:
            return value
    return None


__all__ = [
    "Detected",
    "ambiguous_names",
    "is_ambiguous",
    "detect_location",
    "detect_voivodeship",
    "known_voivodeship",
    "resolve_place",
    "units",
]

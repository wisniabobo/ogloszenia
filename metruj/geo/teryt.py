"""Krajowy rejestr podziału administracyjnego (TERYT).

Dane siedzą w `config/teryt.json` i pochodzą z GUS BDL — 16 województw,
382 powiaty, 2477 gmin. Plik jest w repozytorium, bo podział administracyjny
zmienia się raz na rok; odświeża go `scripts/build_teryt.py`.

Kod TERYT gminy ma 7 znaków: **WW PP GG R** — województwo, powiat, gmina,
rodzaj. Sześć pierwszych znaków (bez rodzaju) zwraca geokoder GUGiK przy
każdym adresie i to jest najmocniejsze, co mamy: identyfikator z ewidencji,
a nie nazwa odczytana z tekstu ogłoszenia.

Rodzaje gmin w TERYT::

    1  gmina miejska                    4  miasto w gminie miejsko-wiejskiej
    2  gmina wiejska                    5  obszar wiejski w takiej gminie
    3  gmina miejsko-wiejska            8  dzielnica m.st. Warszawy
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "teryt.json"

#: rodzaje jednostek, które są miastem (a nie obszarem wiejskim)
TOWN_KINDS = {"1", "4"}

#: Powiaty o kodzie 61–67 to miasta na prawach powiatu — Warszawa, Kraków,
#: Łódź i 63 pozostałe. Rozróżnienie jest praktyczne: „Opole" jako miasto
#: na prawach powiatu bije wieś „Opole" w powiecie parczewskim.
CITY_COUNTY = re.compile(r"^6[1-7]$")

#: Sufiksy, którymi GUS rozdziela część miejską i wiejską gminy miejsko-wiejskiej.
#: W adresie nikt tak nie pisze, więc do dopasowywania nazw ich nie chcemy.
SPLIT_SUFFIX = re.compile(r"\s+-\s+(?:miasto|obszar wiejski)$", re.I)


@dataclass(frozen=True, slots=True)
class Unit:
    """Jednostka podziału administracyjnego."""

    teryt: str              # 2 znaki (woj.), 4 (powiat) albo 7 (gmina)
    name: str
    kind: str               # wojewodztwo | powiat | gmina
    voivodeship: str
    county: str | None = None
    commune: str | None = None
    town: bool = False      # czy jednostka jest miastem
    city_county: bool = False   # miasto na prawach powiatu
    #: dzielnica m.st. Warszawy (rodzaj 8) — w TERYT jest gminą, ale w adresie
    #: zachowuje się jak dzielnica i jako nazwa miejscowości tylko przeszkadza
    #: („Śródmieście" jest w kilkudziesięciu polskich miastach)
    district_unit: bool = False

    @property
    def gmina_teryt(self) -> str | None:
        return self.teryt if self.kind == "gmina" else None

    @property
    def rank(self) -> int:
        """Waga przy wyborze między miejscami o tej samej nazwie.

        Nie jest to liczba mieszkańców, tylko kolejność, w jakiej człowiek
        rozumie nazwę: „Opole" to najpierw miasto wojewódzkie, a dopiero potem
        wieś pod Parczewem. Bez takiego porządku każda dwuznaczna nazwa
        rozstrzygała się przypadkiem — tym, co akurat było pierwsze w pliku.
        """
        if self.city_county:
            return 100
        if self.kind == "powiat":
            return 60
        if self.town:
            return 70
        return 30


@functools.lru_cache(maxsize=1)
def _raw() -> dict:
    if not CONFIG.exists():  # pragma: no cover - plik jest w repozytorium
        return {"wojewodztwa": []}
    return json.loads(CONFIG.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def units() -> list[Unit]:
    """Wszystkie jednostki: województwa, powiaty i gminy, spłaszczone."""
    out: list[Unit] = []
    for woj in _raw().get("wojewodztwa", []):
        wname = woj["nazwa"]
        out.append(Unit(teryt=woj["kod"], name=wname, kind="wojewodztwo", voivodeship=wname))
        for powiat in woj.get("powiaty", []):
            is_city = bool(CITY_COUNTY.match(powiat["kod"][2:4]))
            out.append(
                Unit(
                    teryt=powiat["kod"],
                    name=powiat["nazwa"],
                    kind="powiat",
                    voivodeship=wname,
                    county=powiat["nazwa"],
                    town=is_city,
                    city_county=is_city,
                )
            )
            for gmina in powiat.get("gminy", []):
                name = SPLIT_SUFFIX.sub("", gmina["nazwa"]).strip()
                out.append(
                    Unit(
                        teryt=gmina["kod"],
                        name=name,
                        kind="gmina",
                        voivodeship=wname,
                        county=powiat["nazwa"],
                        commune=name,
                        town=gmina.get("rodzaj") in TOWN_KINDS,
                        city_county=is_city,
                        district_unit=gmina.get("rodzaj") == "8",
                    )
                )
    return out


@functools.lru_cache(maxsize=1)
def voivodeships() -> list[str]:
    return [w["nazwa"] for w in _raw().get("wojewodztwa", [])]


@functools.lru_cache(maxsize=1)
def _by_code() -> dict[str, Unit]:
    """Kod TERYT -> jednostka. Gminy trzymamy też pod kodem bez rodzaju."""
    index: dict[str, Unit] = {}
    for unit in units():
        index.setdefault(unit.teryt, unit)
        if unit.kind == "gmina":
            index.setdefault(unit.teryt[:6], unit)
    return index


def by_teryt(code: str | None) -> Unit | None:
    """Jednostka po kodzie TERYT — przyjmuje 2, 4, 6 albo 7 znaków."""
    if not code:
        return None
    code = str(code).strip()
    for length in (7, 6, 4, 2):
        hit = _by_code().get(code[:length])
        if hit is not None and len(code) >= length:
            return hit
    return None


def voivodeship_of(code: str | None) -> str | None:
    """Nazwa województwa po dowolnym kodzie TERYT (pierwsze dwie cyfry)."""
    if not code or len(str(code)) < 2:
        return None
    unit = _by_code().get(str(code)[:2])
    return unit.voivodeship if unit else None


@functools.lru_cache(maxsize=1)
def counties() -> list[str]:
    return sorted({u.name for u in units() if u.kind == "powiat"})


@functools.lru_cache(maxsize=32)
def counties_of(voivodeship: str | None = None) -> list[str]:
    if not voivodeship:
        return counties()
    return sorted(
        {u.name for u in units() if u.kind == "powiat" and u.voivodeship == voivodeship}
    )


@functools.lru_cache(maxsize=1)
def towns() -> list[str]:
    """Wszystkie miasta w Polsce (1067) — do podpowiedzi i walidacji."""
    return sorted({u.name for u in units() if u.kind == "gmina" and u.town})


@functools.lru_cache(maxsize=1)
def bboxes() -> dict[str, tuple[float, float, float, float]]:
    """Ramka każdego województwa: (lat_min, lat_max, lon_min, lon_max)."""
    out: dict[str, tuple[float, float, float, float]] = {}
    for woj in _raw().get("wojewodztwa", []):
        box = woj.get("bbox")
        if box and len(box) == 4:
            out[woj["nazwa"]] = tuple(float(x) for x in box)  # type: ignore[assignment]
    return out


#: Ramka całej Polski — ostatnia linia obrony przed punktem w innym kraju.
POLAND_BBOX = (48.9, 55.05, 13.9, 24.25)


def in_poland(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    lat_min, lat_max, lon_min, lon_max = POLAND_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def in_voivodeship(lat: float | None, lon: float | None, voivodeship: str | None) -> bool:
    """Czy punkt mieści się w ramce województwa (z zapasem na kształt granicy)."""
    if lat is None or lon is None:
        return False
    box = bboxes().get((voivodeship or "").lower())
    if not box:
        return in_poland(lat, lon)
    lat_min, lat_max, lon_min, lon_max = box
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def voivodeships_at(lat: float | None, lon: float | None) -> list[str]:
    """Województwa, w których ramce leży punkt — przy granicy bywa ich kilka."""
    if lat is None or lon is None:
        return []
    return [
        name for name, (lat_min, lat_max, lon_min, lon_max) in bboxes().items()
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
    ]


#: GUGiK oddaje hierarchię w polu `jednostka`: „{Polska,małopolskie,Kraków,Kraków}".
#: Przy trafieniu w ulicę skraca ją do „{Nysa,160705}", więc trzeba oba warianty.
_JEDNOSTKA = re.compile(r"[{}]")


def parse_jednostka(value: str | None) -> dict[str, str]:
    """Rozkłada pole `jednostka` z GUGiK na województwo / powiat / gminę.

    To jest najtańsze źródło prawdy o przynależności administracyjnej, jakie
    mamy: geokoder odpowiada wprost, w jakim województwie i powiecie leży
    adres, więc nie trzeba tego zgadywać z treści ogłoszenia.
    """
    if not value:
        return {}
    # Tablica w zapisie PostgreSQL: nazwy ze spacją idą w cudzysłowie —
    # „{Polska,podkarpackie,ropczycko-sędziszowski,"Sędziszów Małopolski"}".
    parts = [p.strip().strip('"') for p in _JEDNOSTKA.sub("", value).split(",") if p.strip()]
    if len(parts) >= 4 and parts[0].lower() == "polska":
        return {"voivodeship": parts[1].lower(), "county": parts[2], "commune": parts[3]}
    if len(parts) == 2 and parts[1].isdigit():
        unit = by_teryt(parts[1])
        if unit:
            return {
                "voivodeship": unit.voivodeship,
                "county": unit.county or "",
                "commune": unit.commune or parts[0],
            }
    return {}

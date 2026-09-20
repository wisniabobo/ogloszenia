"""Słownik nazw miejscowości całej Polski — wyszukiwanie nazwy w tekście.

Zawiera 1038 miast i 2477 siedzib gmin z rejestru TERYT. Wsi tu nie ma i nie
powinno być: jest ich ponad sto tysięcy, a ich nazwy to w większości zwykłe
polskie słowa („Miejsce", „Pokój", „Dobra", „Zamek"). Trzymanie ich w słowniku
do dopasowywania tekstu daje więcej szkody niż pożytku — wieś rozpoznajemy
przez geokoder GUGiK, który odpowiada z ewidencji, a nie zgaduje.

Dopasowanie musi przeżyć polską odmianę: „w **Krakowie**", „w **Kędzierzynie-Koźlu**",
„w **Strzelcach Opolskich**". Zamiast kompilować 3,5 tysiąca wyrażeń i puszczać
każde po tekście, tekst jest raz tokenizowany, a nazwy siedzą w indeksie pod
skróconym rdzeniem pierwszego członu. Dzięki temu koszt to jedno przejście
po tekście, a nie tysiące przejść.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from ..utils.text import deaccent
from .teryt import Unit, units

#: Ile znaków pierwszego członu tworzy klucz indeksu.
#:
#: Trzy, bo polska odmiana zmienia nazwę już od czwartej litery: „Opole" ->
#: „w **Opolu**", „Nysa" -> „w **Nysie**". Przy dłuższym kluczu obie formy
#: trafiały do różnych kubełków i miasto w odmienionej formie przepadało —
#: właśnie dlatego „w Opolu" nie było rozpoznawane. Kubełki robią się przez to
#: większe (średnio kilka nazw), ale każde trafienie i tak jest sprawdzane
#: człon po członie, więc kosztuje to ułamek milisekundy.
STEM = 3

#: Najdłuższa końcówka fleksyjna, jaką dopuszczamy przy dopasowaniu członu.
MAX_INFLECTION = 3

TOKEN = re.compile(r"[0-9A-Za-zÀ-ž]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Match:
    """Trafienie nazwy w tekście."""

    unit: Unit
    name: str
    start: int
    end: int
    exact: bool         # trafienie bez odmiany
    capitalized: bool   # pisane wielką literą w oryginale


def _tokens(text: str) -> list[tuple[str, int, int, bool]]:
    """(token bez znaków diakrytycznych i małymi literami, start, koniec, wielka litera)."""
    out = []
    for m in TOKEN.finditer(text):
        raw = m.group(0)
        out.append((deaccent(raw).lower(), m.start(), m.end(), raw[:1].isupper()))
    return out


def _name_tokens(name: str) -> list[str]:
    return [deaccent(t).lower() for t in TOKEN.findall(name)]


@functools.lru_cache(maxsize=1)
def _index() -> dict[str, list[tuple[tuple[str, ...], Unit]]]:
    """Rdzeń pierwszego członu -> lista (człony nazwy, jednostka)."""
    index: dict[str, list[tuple[tuple[str, ...], Unit]]] = {}
    for unit in units():
        if unit.kind == "wojewodztwo" or unit.district_unit:
            continue
        parts = _name_tokens(unit.name)
        if not parts or len(parts[0]) < 3:
            continue
        index.setdefault(parts[0][:STEM], []).append((tuple(parts), unit))
    return index


def _token_matches(pattern: str, token: str) -> tuple[bool, bool]:
    """Czy `token` to `pattern` w dowolnym przypadku. Zwraca (pasuje, dokładnie)."""
    if token == pattern:
        return True, True
    if len(pattern) <= 3:
        return False, False       # krótkie człony („Bór") tylko dokładnie
    stem = pattern[:-1]
    if not token.startswith(stem):
        return False, False
    return len(token) - len(stem) <= MAX_INFLECTION, False


def find_places(text: str | None) -> list[Match]:
    """Wszystkie nazwy ze słownika znalezione w tekście, z pozycjami.

    Nazwy wielowyrazowe mają pierwszeństwo przed jednowyrazowymi: dla „Strzelce
    Opolskie" chcemy tej miejscowości, a nie przypadkowych „Strzelec".
    """
    if not text:
        return []
    tokens = _tokens(text)
    index = _index()
    found: list[Match] = []

    for position, (token, start, _end, capitalized) in enumerate(tokens):
        for parts, unit in index.get(token[:STEM], ()):
            if position + len(parts) > len(tokens):
                continue
            exact = True
            for offset, part in enumerate(parts):
                ok, is_exact = _token_matches(part, tokens[position + offset][0])
                if not ok:
                    break
                exact = exact and is_exact
            else:
                found.append(
                    Match(
                        unit=unit,
                        name=unit.name,
                        start=start,
                        end=tokens[position + len(parts) - 1][2],
                        exact=exact,
                        capitalized=capitalized,
                    )
                )

    # Rozstrzyganie trafień, po kolei:
    #   * dłuższa nazwa bije krótszą („Strzelce Opolskie", nie „Strzelce"),
    #   * trafienie bez odmiany bije odmienione („Świerczów", nie „Świercze"),
    #   * gmina bije powiat o tej samej nazwie — niesie komplet: miasto,
    #     powiat i województwo naraz,
    #   * przy dalszym remisie wygrywa jednostka o wyższej randze (miasto
    #     przed wsią), bo tak tę nazwę rozumie czytający.
    found.sort(
        key=lambda m: (
            m.start,
            -(m.end - m.start),
            not m.exact,
            m.unit.kind != "gmina",
            -m.unit.rank,
            m.unit.name,
        )
    )
    best: list[Match] = []
    for match in found:
        if best and match.start < best[-1].end:
            continue
        best.append(match)
    return best


@functools.lru_cache(maxsize=1)
def _by_name() -> dict[str, list[Unit]]:
    index: dict[str, list[Unit]] = {}
    for unit in units():
        if unit.kind == "wojewodztwo" or unit.district_unit:
            continue
        index.setdefault(deaccent(unit.name).lower(), []).append(unit)
    return index


def lookup(name: str | None) -> list[Unit]:
    """Jednostki o dokładnie tej nazwie — posortowane od najważniejszej."""
    if not name:
        return []
    key = deaccent(re.split(r"[,/(]", name)[0]).lower().strip()
    hits = _by_name().get(key, [])
    return sorted(hits, key=lambda u: (-u.rank, u.kind != "gmina", u.name))


@functools.lru_cache(maxsize=1)
def town_names() -> list[str]:
    """Nazwy miast — do podpowiedzi w wyszukiwarce."""
    return sorted({u.name for u in units() if u.kind == "gmina" and u.town})

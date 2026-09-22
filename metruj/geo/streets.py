"""Wyciąganie nazwy ulicy z tekstu ogłoszenia."""

from __future__ import annotations

import re

from ..utils.text import clean

STREET_RE = re.compile(
    r"\b(?:ul\.?|ulica|ulicy|al\.?|aleja|aleje|alei|os\.?|osiedle|osiedlu|"
    r"pl\.?|plac|placu|rynek|rynku)\s+"
    r"([A-ZŻŹĆĄŚĘŁÓŃ][\w.\-]*(?:\s+(?:[A-ZŻŹĆĄŚĘŁÓŃ][\w.\-]*|i|de|von|im|na|pod))*)",
    re.UNICODE,
)

#: Słowa, po których nazwa ulicy na pewno się skończyła. Bez nich do bazy
#: wchodziły nazwy w rodzaju „Ozimska Opole Sprzedam", „Ozimskiej i" czy
#: „Krakowska Bezpośrednio" — a taka „ulica" nie łączy się z niczym: ani
#: z geokoderem, ani z inną ofertą przy tej samej ulicy.
_STOP = re.compile(
    r"\s+(?:w|we|i|oraz|k/|koło|obok|blisko|przy|tel|telefon|kontakt|cena|pow|"
    r"powierzchnia|metra[żz]|nr|numer|oferta|oferuje\w*|polecam\w*|sprzedam\w*|"
    r"sprzeda[żz]\w*|kupi\w*|wynajm\w*|wynaj\w*|do\s+wynaj\w*|bezpo[śs]rednio|"
    r"bez\s+po[śs]rednik\w*|mieszkanie|mieszkania|kawalerka|apartament\w*|dom|domy|"
    r"lokal|dzia[łl]k\w*|gara[żz]\w*|piętro|pi[ęe]tro|pok[oó]j|pokoje|"
    r"nowe|nowa|now\w*\s+inwestycj\w*|inwestycj\w*|stan|rynek\s+\w+)\b",
    re.I,
)

#: Miasta i województwa doklejone do nazwy ulicy („Ozimska Opole") — nazwa
#: ulicy kończy się przed nazwą miejscowości z rejestru.
def _cut_place(street: str) -> str:
    from .gazetteer import lookup

    words = street.split()
    for index in range(1, len(words)):
        if lookup(words[index]) and len(words[index]) > 3:
            return " ".join(words[:index])
    return street

#: Przedrostek typu ulicy w bazie psuje geokodowanie (GUGiK na „ul. Telesfora"
#: zwraca zero wyników), a w interfejsie dokłada go szablon.
PREFIX = re.compile(r"^\s*(?:ul\.?|ulica|ulicy)\s+", re.I)

#: Numer domu doklejony do nazwy: „Wrocławska 12A", „Piastowska 3/5".
HOUSE_NUMBER = re.compile(r"\s+(\d+[A-Za-z]?(?:\s*[/\\]\s*\d+[A-Za-z]?)?)\s*$")


def extract_street(text: str | None) -> str | None:
    """„ul. Leona Powolnego. Kontakt 500…" -> „Leona Powolnego".

    Nazwa kończy się na kropce zdania, przecinku, myślniku albo słowie, które
    otwiera kolejną informację — bez tego do nazwy wchodziło pół opisu.
    """
    if not text:
        return None
    match = STREET_RE.search(text)
    if not match:
        return None
    street = clean(match.group(1))
    street = re.split(r"\.\s+|\s+[–—]\s+|\s+-\s+|[,;)]", street)[0]
    street = _STOP.split(street)[0].strip(" .,-–—")
    street = _cut_place(street).strip(" .,-–—")
    if len(street) < 3 or street.isdigit():
        return None
    return street[:120]


def split_house_number(street: str | None) -> tuple[str | None, str | None]:
    """Rozdziela „Wrocławska 12A" na nazwę i numer — geokoder chce ich osobno."""
    if not street:
        return None, None
    match = HOUSE_NUMBER.search(street)
    if not match:
        return street, None
    name = street[: match.start()].strip(" .,")
    return (name or None), match.group(1).replace(" ", "")


def normalize_street(street: str | None) -> str | None:
    """Zdejmuje „ul." — aleję, osiedle i plac zostawia, bo zmieniają adres."""
    if not street:
        return None
    return PREFIX.sub("", street).strip(" .,") or None

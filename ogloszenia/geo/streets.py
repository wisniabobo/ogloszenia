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

#: słowa, po których nazwa ulicy na pewno się skończyła
_STOP = re.compile(
    r"\s+(?:w|we|k/|koło|obok|tel|telefon|kontakt|cena|pow|powierzchnia|nr|numer|"
    r"oferta|mieszkanie|dom|lokal|dzia[łl]ka|gara[żz]|sprzeda[żz]|wynaj\w*)\b",
    re.I,
)

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

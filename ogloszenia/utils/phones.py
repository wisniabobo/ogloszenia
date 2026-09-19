"""Wykrywanie, normalizacja, maskowanie i haszowanie numerów telefonu (PL).

Numery to dane osobowe — moduł domyślnie zwraca postać zamaskowaną
(`537 *** ***`), a pełny numer udostępnia dopiero na jawne żądanie
(API `/api/listings/{id}/phone`). Tryb `store_phone_hash_only=true`
zapisuje wyłącznie skrót (HMAC-owy hash z solą), co pozwala nadal
deduplikować oferty bez trzymania numerów w bazie.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import phonenumbers

from ..settings import get_settings

# Sekwencja cyfr z dopuszczalnymi separatorami / zaciemnieniem
_CANDIDATE = re.compile(
    r"(?:(?:\+?\s*48|0\s*0\s*48)[\s.\-()]*)?(?:\d[\s.\-()]*){8,11}"
)
_WORD_DIGITS = {
    "zero": "0", "jeden": "1", "dwa": "2", "trzy": "3", "cztery": "4",
    "pięć": "5", "piec": "5", "sześć": "6", "szesc": "6", "siedem": "7",
    "osiem": "8", "dziewięć": "9", "dziewiec": "9",
}
# Ciągi, które wyglądają jak telefon, a nim nie są
_BLOCKLIST_PREFIX = ("0000", "1111", "1234567", "9999")


@dataclass(slots=True)
class PhoneNumber:
    e164: str
    national: str
    masked: str
    hashed: str
    origin: str = "opis"

    def as_dict(self, reveal: bool = False) -> dict:
        data = {"masked": self.masked, "hash": self.hashed, "origin": self.origin}
        if reveal:
            data |= {"e164": self.e164, "national": self.national}
        return data


def _words_to_digits(text: str) -> str:
    out = text
    for word, digit in _WORD_DIGITS.items():
        out = re.sub(rf"\b{word}\b", digit, out, flags=re.I)
    return out


def hash_phone(e164: str) -> str:
    salt = get_settings().phone_hash_salt.encode()
    return hashlib.blake2b(e164.encode(), key=salt[:64], digest_size=16).hexdigest()


def mask(national: str) -> str:
    """'537123123' -> '537 *** ***' (format jak w podglądzie listy)."""
    digits = re.sub(r"\D", "", national)
    if len(digits) == 9:
        return f"{digits[:3]} *** ***"
    if len(digits) > 4:
        return f"{digits[:3]}{'*' * (len(digits) - 3)}"
    return "***"


def _build(raw_e164: str, origin: str) -> PhoneNumber | None:
    try:
        parsed = phonenumbers.parse(raw_e164, "PL")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    national = re.sub(r"\D", "", str(parsed.national_number))
    if national.startswith(_BLOCKLIST_PREFIX):
        return None
    return PhoneNumber(e164=e164, national=national, masked=mask(national),
                       hashed=hash_phone(e164), origin=origin)


def parse_phone(value: str, origin: str = "api") -> PhoneNumber | None:
    """Normalizuje pojedynczy numer (np. z API portalu)."""
    if not value:
        return None
    digits = re.sub(r"[^\d+]", "", value)
    if not digits:
        return None
    if not digits.startswith("+"):
        digits = digits.lstrip("0")
        if len(digits) == 11 and digits.startswith("48"):
            digits = "+" + digits
        elif len(digits) == 9:
            digits = "+48" + digits
        else:
            digits = "+" + digits if len(digits) > 9 else "+48" + digits
    return _build(digits, origin)


def extract_phones(text: str, origin: str = "opis", limit: int = 5) -> list[PhoneNumber]:
    """Wyciąga numery z dowolnego tekstu (opis ogłoszenia, stopka strony).

    Radzi sobie z zaciemnianiem: spacje, kropki, myślniki, nawiasy oraz
    liczebnikami zapisanymi słownie.
    """
    if not text:
        return []
    normalized = _words_to_digits(text)
    found: dict[str, PhoneNumber] = {}
    for match in _CANDIDATE.finditer(normalized):
        chunk = match.group(0)
        digits = re.sub(r"\D", "", chunk)
        if len(digits) < 9:
            continue
        # spróbuj kilku wariantów odcięcia (numer może być sklejony z innymi cyframi)
        variants = []
        if digits.startswith("48") and len(digits) >= 11:
            variants.append("+" + digits[:11])
        variants.append("+48" + digits[-9:])
        variants.append("+48" + digits[:9])
        for variant in variants:
            phone = _build(variant, origin)
            if phone and phone.e164 not in found:
                found[phone.e164] = phone
                break
        if len(found) >= limit:
            break
    return list(found.values())


def phones_fingerprint(phones: list[PhoneNumber]) -> str | None:
    """Wspólny odcisk zbioru numerów — dwie oferty z tym samym telefonem
    to prawie na pewno ta sama nieruchomość (albo ten sam oferent)."""
    if not phones:
        return None
    return sorted(p.hashed for p in phones)[0]

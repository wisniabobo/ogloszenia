"""Normalizacja tekstu, liczb, dat i wyciąganie parametrów z opisów."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from dateutil import parser as dateparser

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]{1,400}>")
_ENTITY = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}
_NUM = re.compile(r"(\d[\d\s .,]*)")

MONTHS_PL = {
    "stycznia": 1, "styczeń": 1, "stycznia.": 1,
    "lutego": 2, "luty": 2,
    "marca": 3, "marzec": 3,
    "kwietnia": 4, "kwiecień": 4,
    "maja": 5, "maj": 5,
    "czerwca": 6, "czerwiec": 6,
    "lipca": 7, "lipiec": 7,
    "sierpnia": 8, "sierpień": 8,
    "września": 9, "wrzesień": 9,
    "października": 10, "październik": 10,
    "listopada": 11, "listopad": 11,
    "grudnia": 12, "grudzień": 12,
}


def clean(text: object | None) -> str:
    """Normalizuje białe znaki. Przyjmuje cokolwiek — API portali potrafią
    wstawić w pole tekstowe liczbę albo `true`."""
    if text is None or text is False or text == "":
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace(" ", " ").replace("​", "")
    return _WS.sub(" ", text).strip()


def strip_html(text: str | None) -> str:
    """Usuwa znaczniki HTML z opisu.

    OLX zwraca opis w postaci `<p>…</p><br>`; bez tego znaczniki trafiałyby
    do bazy i psuły porównywanie opisów między portalami.
    """
    if not text:
        return ""
    out = _TAG.sub(" ", text)
    for entity, char in _ENTITY.items():
        out = out.replace(entity, char)
    return clean(out)


def deaccent(text: str) -> str:
    text = text.replace("ł", "l").replace("Ł", "L")
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def slugify(text: str, max_len: int = 150) -> str:
    s = deaccent(clean(text)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len] or "brak"


def norm_key(text: str | None) -> str:
    """Klucz porównawczy: bez ogonków, bez interpunkcji, lowercase."""
    return re.sub(r"[^a-z0-9]+", " ", deaccent(clean(text or "")).lower()).strip()


def sha1(*parts: object) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "ignore"))
        h.update(b"|")
    return h.hexdigest()


def parse_number(value: str | float | int | None) -> float | None:
    """'629 000 zł' -> 629000.0 ; '12 837 zł/m²' -> 12837.0 ; '75,28 m²' -> 75.28"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM.search(value.replace(" ", " "))
    if not m:
        return None
    raw = m.group(1).strip().replace(" ", "")
    # Rozróżnienie separatora dziesiętnego od tysięcznego
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        # 75,28 -> dziesiętny; 1,250,000 -> tysięczny
        raw = raw.replace(",", ".") if raw.count(",") == 1 and len(raw.split(",")[-1]) <= 2 else raw.replace(",", "")
    elif raw.count(".") == 1 and len(raw.split(".")[-1]) == 3 and len(raw.split(".")[0]) <= 3:
        raw = raw.replace(".", "")  # 629.000
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_int(value: str | int | None) -> int | None:
    n = parse_number(value)
    return int(n) if n is not None else None


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Obsługuje ISO, polskie daty słowne oraz zwroty względne ('dzisiaj 14:30')."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value

    s = clean(value).lower()
    if not s:
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    time_m = re.search(r"(\d{1,2}):(\d{2})", s)
    hh, mm = (int(time_m.group(1)), int(time_m.group(2))) if time_m else (0, 0)

    if "dzisiaj" in s or "dziś" in s or s.startswith("today"):
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if "wczoraj" in s:
        return (now - timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)

    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", s)
    if m and m.group(2) in MONTHS_PL:
        return datetime(int(m.group(3)), MONTHS_PL[m.group(2)], int(m.group(1)), hh, mm)

    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)$", s)
    if m and m.group(2) in MONTHS_PL:
        return datetime(now.year, MONTHS_PL[m.group(2)], int(m.group(1)), hh, mm)

    try:
        dt = dateparser.parse(s, dayfirst=True, fuzzy=True)
        return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt
    except (ValueError, OverflowError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Wyciąganie parametrów z tekstu
# --------------------------------------------------------------------------- #
# Powierzchnia: dopuszczamy spację jako separator tysięcy, ale tylko w pełnych
# trójkach cyfr — inaczej "3 pokoje, 49 m2" dałoby 349 m².
AREA_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*"
    r"(?:m2|m²|mkw|m\.kw|metrów kwadratowych|metrów|metry)",
    re.I,
)
ROOMS_RE = re.compile(r"(\d+)[\s-]*(?:pokoj|pokoi|pok\.|pokoje|pokój|pokojow)", re.I)
FLOOR_RE = re.compile(r"(?:piętro|pietro)[:\s]*(\d+|parter\w*)", re.I)
FLOOR_OF_RE = re.compile(r"(\d+|parter\w*)\s*piętro\s*z\s*(\d+)", re.I)
YEAR_RE = re.compile(r"(?:rok budowy|wybudowan\w*|z roku)[:\s]*((?:1[89]|20)\d{2})", re.I)
PLOT_RE = re.compile(r"(?:działk\w+|powierzchnia działki)[^\d]{0,20}(\d+[\s.,]?\d*)\s*(?:m2|m²|ar|ha)?", re.I)
CASE_RE = re.compile(r"\b((?:K[Mm]|GKm|Kmp|Kms|GKM|KM)\s?\d+/\d+)\b")
SIGN_RE = re.compile(r"\b([IVX]+\s?[A-Za-z]{1,4}\s?\d+/\d+)\b")


def extract_area(text: str) -> float | None:
    m = AREA_RE.search(text or "")
    return parse_number(m.group(1)) if m else None


def extract_rooms(text: str) -> int | None:
    m = ROOMS_RE.search(text or "")
    return parse_int(m.group(1)) if m else None


def extract_floor(text: str) -> tuple[int | None, int | None]:
    m = FLOOR_OF_RE.search(text or "")
    if m:
        fl = 0 if m.group(1).lower().startswith("parter") else parse_int(m.group(1))
        return fl, parse_int(m.group(2))
    m = FLOOR_RE.search(text or "")
    if m:
        return (0 if m.group(1).lower().startswith("parter") else parse_int(m.group(1))), None
    # „parter", „na parterze", „parterowy" — polska odmiana
    if re.search(r"\bparter\w*", text or "", re.I):
        return 0, None
    return None, None


def extract_year(text: str) -> int | None:
    m = YEAR_RE.search(text or "")
    return parse_int(m.group(1)) if m else None


def extract_case_number(text: str) -> str | None:
    for rx in (CASE_RE, SIGN_RE):
        m = rx.search(text or "")
        if m:
            return clean(m.group(1))
    return None


#: poniżej tylu słów tekst nie identyfikuje oferty ("lokal mieszkalny")
MIN_SHINGLE_WORDS = 12


def shingle_hash(text: str, k: int = 5) -> str | None:
    """Skrót odporny na drobne przeróbki opisu (do wykrywania kopii).

    Zwraca None dla krótkich, ogólnych tekstów — obwieszczenia komornicze
    potrafią mieć tytuł „nieruchomość gruntowa zabudowana" i bez tego progu
    dwie różne działki z dwóch powiatów wyglądałyby na tę samą ofertę.
    """
    words = norm_key(text).split()
    if len(words) < MIN_SHINGLE_WORDS:
        return None
    grams = {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}
    top = sorted(grams)[:64]
    return sha1("|".join(top))


def truncate(text: str, limit: int = 220) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"

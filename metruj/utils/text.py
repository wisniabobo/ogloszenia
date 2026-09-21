"""Normalizacja tekstu, liczb, dat i wyciąganie parametrów z opisów."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
    elif raw.count(",") > 1:
        raw = raw.replace(",", "")  # 1,250,000 — zapis angielski
    elif "," in raw:
        # Polskie ogłoszenia oddzielają tysiące spacją, a przecinek jest
        # zawsze dziesiętny. Wcześniej uznawaliśmy go za tysięczny, gdy po
        # przecinku stały więcej niż dwie cyfry, przez co areał „0,0436 ha"
        # (436 m²) czytaliśmy jako 436 hektarów.
        raw = raw.replace(",", ".")
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


#: Data w zapisie ISO: rok na początku („2026-09-12", „2026-09-12T10:41:34+02:00").
ISO_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}")


def _to_utc_naive(dt: datetime) -> datetime:
    """Czas ze strefą sprowadzony do UTC, bez znacznika strefy.

    Wszystkie nasze znaczniki czasu (`first_seen_at`, `utcnow()`) są w UTC.
    Samo zdjęcie strefy z „10:41+02:00" dawało 10:41 zamiast 08:41 — i oferta
    wystawiona tuż po północy lądowała w „dzisiaj" albo „wczoraj" nie tego dnia.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


#: Strefa, w której podają godziny sądy, komornicy i urzędy.
WARSAW = ZoneInfo("Europe/Warsaw")


def _to_local_naive(dt: datetime) -> datetime:
    """Czas ze strefą przeliczony na czas polski, bez znacznika strefy."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(WARSAW).replace(tzinfo=None)


def to_polish_time(moment: datetime | None) -> datetime | None:
    """Znacznik z bazy (UTC) na godzinę, którą widzi człowiek w Polsce."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(WARSAW).replace(tzinfo=None)


def polish_midnight_utc(now: datetime | None = None) -> datetime:
    """Początek dzisiejszego dnia w Polsce, jako chwila w UTC.

    „Dodane dziś" to od północy polskiego czasu, a nie ostatnie 24 godziny
    ani doba liczona od północy UTC (która w Polsce wypada o 1:00 albo 2:00).
    """
    local = to_polish_time(now or datetime.now(timezone.utc).replace(tzinfo=None))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return _to_utc_naive(midnight.replace(tzinfo=WARSAW))


def parse_datetime(
    value: str | datetime | int | float | None, *, naive_local: bool = False
) -> datetime | None:
    """Chwila w UTC — dla dat wystawienia, odświeżenia i wszystkiego, co
    porównujemy z `utcnow()`. Szczegóły odczytu opisuje `_parse_moment`.

    `naive_local` mówi, że zapis bez strefy to czas polski. Tak podaje daty
    Otodom („2026-09-21 19:13:17" obok „2026-09-21T19:13:18+02:00" w tym samym
    ogłoszeniu) — wzięty za UTC, lądował dwie godziny w przyszłości.
    """
    moment = _parse_moment(value)
    if moment is None:
        return None
    if naive_local and moment.tzinfo is None:
        moment = moment.replace(tzinfo=WARSAW)
    return _to_utc_naive(moment)


def parse_local_datetime(value: str | datetime | int | float | None) -> datetime | None:
    """Godzina zegarowa w Polsce — dla terminów licytacji, przetargów i wadium.

    Licytację o 11:00 pokazujemy jako 11:00, a nie jako 09:00 UTC, bo tak ją
    ogłosił komornik i o tej godzinie trzeba być na sali albo przy komputerze.
    e-Licytacje KAS podają terminy w UTC („2026-11-26T09:00:00Z"), strony
    sądów i urzędów — w czasie polskim bez strefy; po przeliczeniu jedno
    i drugie znaczy to samo.
    """
    moment = _parse_moment(value)
    return _to_local_naive(moment) if moment else None


def _parse_moment(value: str | datetime | int | float | None) -> datetime | None:
    """Obsługuje ISO, polskie daty słowne, zapis dzień.miesiąc.rok, znaczniki
    czasu uniksowego oraz zwroty względne („dzisiaj 14:30").

    **Zapis ISO czytamy ściśle, osobno od reszty.** Ogólny parser z ustawieniem
    „dzień pierwszy" — potrzebnym dla polskiego „12.09.2026" — czytał także
    „2026-09-12" jako rok-dzień-miesiąc i robił z niego 9 grudnia. Dotyczyło to
    każdej daty z dniem od 1 do 12, czyli mniej więcej 40% ofert: tysiące
    ogłoszeń „dodanych" w przyszłości, sortowanie od najnowszych stawiające na
    górze ogłoszenia sprzed miesięcy i przestawione terminy licytacji. Dni od 13
    wzwyż wychodziły dobrze, bo nie mogą być miesiącem — stąd błąd wyglądał
    na przypadkowy.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # znacznik uniksowy — w sekundach albo w milisekundach
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    raw = clean(value)
    if not raw:
        return None

    if ISO_DATE.match(raw):
        try:
            return dateparser.isoparse(raw.replace(" ", "T", 1))
        except (ValueError, OverflowError):
            pass
        try:
            return dateparser.parse(raw, dayfirst=False, yearfirst=True)
        except (ValueError, OverflowError, TypeError):
            return None

    s = raw.lower()
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
    except (ValueError, OverflowError, TypeError):
        return None
    return dt


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
#: Powierzchnia działki bywa podana w arach i hektarach, a ogłoszeniodawcy
#: mieszają jednostki w jednym zdaniu („dom 180 m², działka 12 arów").
#: Bez przeliczenia na metry działka 0,45 ha wchodziła do bazy jako 0,45 m².
AREA_UNITS: dict[str, float] = {
    "m2": 1.0, "m²": 1.0, "mkw": 1.0, "m.kw": 1.0, "metrow": 1.0, "metry": 1.0,
    "a": 100.0, "ar": 100.0, "ary": 100.0, "arow": 100.0, "arów": 100.0,
    "ha": 10_000.0, "hektar": 10_000.0, "hektary": 10_000.0, "hektarow": 10_000.0,
    "hektarów": 10_000.0,
}

_NUMBER = r"(\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
#: „ar" odmienia się jak rzeczownik: 8,5 **ara**, 12 **arów**, 3 **ary**.
_UNIT = r"(m2|m²|mkw|m\.kw|ha|hektar\w*|ar(?:ach|ami|owi|[oó]w|y|a|ze|em)?|a)\b"

PLOT_RE = re.compile(
    r"(?:powierzchni\w*\s+(?:dzia[łl]ki|gruntu|terenu|nieruchomo[śs]ci)|"
    r"dzia[łl]k\w*|grunt\w*|teren\w*|parcel\w*|area[łl])"
    r"[^\d\n]{0,28}?" + _NUMBER + r"\s*" + _UNIT,
    re.I,
)
AREA_WITH_UNIT = re.compile(_NUMBER + r"\s*" + _UNIT, re.I)
CASE_RE = re.compile(r"\b((?:K[Mm]|GKm|Kmp|Kms|GKM|KM)\s?\d+/\d+)\b")
SIGN_RE = re.compile(r"\b([IVX]+\s?[A-Za-z]{1,4}\s?\d+/\d+)\b")


def extract_area(text: str) -> float | None:
    m = AREA_RE.search(text or "")
    return parse_number(m.group(1)) if m else None


def to_square_meters(value: float | None, unit: str | None) -> float | None:
    """Przelicza powierzchnię na metry kwadratowe. „0,45 ha" -> 4500.0"""
    if value is None:
        return None
    key = deaccent(clean(unit or "m2")).lower().rstrip(".")
    factor = AREA_UNITS.get(key)
    if factor is None:
        factor = next((v for k, v in AREA_UNITS.items() if key.startswith(k)), 1.0)
    return round(value * factor, 2)


def extract_plot_area(text: str) -> float | None:
    """Powierzchnia działki w metrach, z dowolnej jednostki użytej w ogłoszeniu.

    „działka 12 arów" -> 1200.0 ; „grunt o pow. 0,45 ha" -> 4500.0.
    Jednostka jest tu obowiązkowa: sama liczba po słowie „działka" równie
    często oznacza numer ewidencyjny co powierzchnię.
    """
    m = PLOT_RE.search(text or "")
    if not m:
        return None
    return to_square_meters(parse_number(m.group(1)), m.group(2))


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

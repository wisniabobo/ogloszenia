"""Normalizacja surowych ofert do kształtu tabeli `listings`.

Tu dzieje się cała brudna robota: uzupełnianie brakujących parametrów z opisu,
ustalenie lokalizacji, przeliczenie ceny za m², odsianie śmieci i wyciągnięcie
telefonów.

Lokalizacja wyprowadziła się stąd do `pipeline/location.py` — była najczęstszym
źródłem błędów w całym serwisie i zasługuje na osobny moduł z własnymi testami.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..scrapers.base import RawListing
from ..utils.phones import PhoneNumber, extract_phones, parse_phone
from ..utils.text import (
    clean,
    extract_area,
    extract_case_number,
    extract_floor,
    extract_plot_area,
    extract_rooms,
    extract_year,
    strip_html,
)
from .location import resolve as resolve_location

MARKET_PRIMARY = re.compile(r"rynek pierwotn|pierwotny|od dewelopera|nowa inwestycja", re.I)
MARKET_SECONDARY = re.compile(r"rynek wtórn|wtorny|używan", re.I)

# Oferty, których nie chcemy w bazie (spam, usługi, „kupię")
NOISE = re.compile(
    r"\b(kupię|kupie|poszukuję|poszukuje|skup (mieszkań|nieruchomości)|zamienię|"
    r"kredyt|doradztwo|remont|usługi|sprzątanie|projekt wnętrz)\b",
    re.I,
)

#: Tytuły, które nie opisują żadnej nieruchomości, tylko element strony.
#: Scraper AMW wciągał w ten sposób blok „Polecane nieruchomości" i zapisywał
#: go jako ofertę — jedenaście razy, za każdym razem z lokalizacją zgadniętą
#: z przypadkowego słowa w menu. Takie pozycje odrzucamy niezależnie od tego,
#: który scraper je przyniósł: żaden nie powinien ich produkować, ale lepiej
#: mieć jedno miejsce, które tego pilnuje.
NAVIGATION_TITLE = re.compile(
    r"^\s*(?:polecane|wyr[oó]żnione|podobne|ostatnio ogl[ąa]dane|najnowsze|"
    r"wyniki wyszukiwania|lista ofert|oferty|nieruchomo[śs]ci|zobacz|więcej|"
    r"strona \d+|wszystkie)\b[^,:]{0,40}$",
    re.I,
)

#: Wiarygodny metraż dla danego typu (m²). Portal potrafi podać bzdurę —
#: mieszkanie „127,43 m2" z tytułu trafiało do bazy jako 12 743 m², co psuło
#: cenę za metr i statystyki całego rynku.
AREA_LIMITS: dict[PropertyType, tuple[float, float]] = {
    PropertyType.MIESZKANIE: (8, 1000),
    PropertyType.POKOJ: (4, 120),
    PropertyType.DOM: (20, 3000),
    PropertyType.GARAZ: (5, 200),
    PropertyType.KAMIENICA: (50, 10000),
    PropertyType.BIURO: (5, 20000),
    PropertyType.LOKAL: (5, 20000),
    # Grunty mierzy się w hektarach — 300 ha to duże gospodarstwo, ale istnieje.
    PropertyType.DZIALKA: (30, 5_000_000),
    PropertyType.GOSPODARSTWO: (100, 20_000_000),
    PropertyType.HALA: (20, 200_000),
    PropertyType.MAGAZYN: (20, 200_000),
}

#: Typy, przy których „powierzchnia" oferty to powierzchnia gruntu, a nie budynku.
LAND_TYPES = {PropertyType.DZIALKA, PropertyType.GOSPODARSTWO}

#: Ogłoszenie „na sprzedaż" z ceną poniżej tej kwoty to niemal zawsze wynajem
#: wrzucony do złej kategorii portalu (albo cena „do negocjacji" wpisana jako 1).
MIN_SALE_PRICE = 15000

#: Tytuł mówiący wprost o wynajmie bije kategorię portalu — ogłoszeniodawcy
#: notorycznie wrzucają wynajem do działu sprzedaży.
RENT_IN_TITLE = re.compile(
    r"\bdo wynaj|\bna wynaj|\bwynajm|\bwynajem\b|\bdo zamieszkania od zaraz za\b", re.I
)
LEASE_IN_TITLE = re.compile(r"\bdzierżaw|\bwydzierżaw|\boddam w dzierżaw", re.I)

#: Numer działki ewidencyjnej: „działka nr 123/4", „dz. ew. 88". Przy gruntach
#: i licytacjach to jedyny pewny identyfikator nieruchomości — po nim da się
#: odpytać rejestr GUGiK o rzeczywisty kształt i powierzchnię.
PARCEL_RE = re.compile(
    r"(?:dzia[łl]k\w*|dz\.?\s*(?:ew\.?|ewid\w*)?)\s*(?:nr|numer|o\s+numerze)?\s*"
    r"(\d{1,5}(?:/\d{1,5})?(?:\s*,\s*\d{1,5}(?:/\d{1,5})?){0,4})",
    re.I,
)
REGISTER_UNIT_RE = re.compile(r"obr[ęe]b\w*\s*(?:ewidencyjny\w*)?\s*[:\-]?\s*([\w\s.-]{3,60})", re.I)


def _plausible_area(area: float | None, property_type: PropertyType) -> bool:
    if area is None:
        return True
    low, high = AREA_LIMITS.get(property_type, (0.5, 5_000_000))
    return low <= area <= high


@dataclass
class NormalizedListing:
    """Znormalizowana oferta + wydzielone telefony (te idą do osobnej tabeli)."""

    data: dict
    phones: list[PhoneNumber] = field(default_factory=list)
    raw: RawListing | None = None
    #: Pola, które źródło świadomie zostawiło puste — to informacja, a nie brak
    #: danych. Zapis nadpisuje nimi wcześniejszą wartość; bez tego poprawka
    #: typu „ta ulica to adres urzędu, nie nieruchomości" nie doszłaby do bazy.
    cleared: tuple[str, ...] = ()

    @property
    def external_key(self) -> tuple[str, str]:
        return self.data["source_key"], self.data["external_id"]


def _pick(*values):
    for value in values:
        if value not in (None, "", 0):
            return value
    return None


def _parcel_numbers(text: str) -> list[str]:
    match = PARCEL_RE.search(text)
    if not match:
        return []
    return [clean(part) for part in match.group(1).split(",") if clean(part)][:5]


def normalize(
    raw: RawListing,
    *,
    scope: list[str] | None = None,
    voivodeship: str | None = None,
    require_region: bool = False,
) -> NormalizedListing | None:
    """Zwraca `NormalizedListing` albo `None`, jeśli oferta odpada.

    `scope` to lista województw, do których zawężamy zbiór; pusta lista znaczy
    **cała Polska** i tak jest domyślnie. Ofertę bez rozpoznanego województwa
    zostawiamy — region dopisze jej geokoder, który pyta rejestr adresowy,
    zamiast zgadywać z tytułu. Odrzucanie takich ofert gubiło dane.
    """
    title = clean(raw.title)
    description = strip_html(raw.description)
    if not title or not raw.url:
        return None

    haystack = f"{title} {description} {raw.location_text or ''}"

    if raw.kind == OfferKind.NIERUCHOMOSC and NOISE.search(title):
        return None
    if NAVIGATION_TITLE.match(title):
        return None

    # --- lokalizacja ---
    place = resolve_location(raw)
    scope = scope if scope is not None else ([voivodeship] if voivodeship and require_region else [])
    if scope and place.voivodeship and place.voivodeship not in scope:
        return None

    # --- parametry ---
    property_type = raw.property_type
    if property_type == PropertyType.INNE and raw.kind != OfferKind.PRZETARG:
        from ..scrapers.generic_html import guess_property_type

        property_type = guess_property_type(title, description[:800])

    area = _pick(raw.area, extract_area(title), extract_area(description[:1500]))
    plot_area = _pick(
        raw.plot_area,
        extract_plot_area(title),
        extract_plot_area(description[:2500]),
    )
    # Przy gruncie „powierzchnia" to powierzchnia działki. Portale podają ją raz
    # w jednym, raz w drugim polu, a bez tego ujednolicenia filtr „działki od
    # 1000 m²" omijał połowę zasobu.
    if property_type in LAND_TYPES:
        area = _pick(area, plot_area)
        plot_area = _pick(plot_area, area)

    rooms = _pick(raw.rooms, extract_rooms(title), extract_rooms(description[:1500]))
    floor, floors_total = raw.floor, raw.floors_total
    if floor is None:
        floor, detected_total = extract_floor(f"{title} {description[:1500]}")
        floors_total = floors_total or detected_total
    year_built = _pick(raw.year_built, extract_year(description[:3000]))

    price = raw.price
    if price is not None and price <= 0:
        price = None   # „0 zł" znaczy „nie podano", a nie „za darmo"

    market = raw.market
    if not market:
        if MARKET_PRIMARY.search(haystack):
            market = "pierwotny"
        elif MARKET_SECONDARY.search(haystack):
            market = "wtorny"
    elif market.upper() in {"PRIMARY", "SECONDARY"}:
        market = "pierwotny" if market.upper() == "PRIMARY" else "wtorny"

    # Metraż spoza rozsądnych granic dla danego typu odrzucamy i próbujemy
    # odczytać go jeszcze raz z tytułu — tam człowiek pisze prawdziwą liczbę.
    if not _plausible_area(area, property_type):
        from_title = extract_area(title)
        area = from_title if _plausible_area(from_title, property_type) else None
    if plot_area is not None and not 1 <= plot_area <= 20_000_000:
        plot_area = None

    transaction = raw.transaction or TransactionType.SPRZEDAZ
    if raw.kind == OfferKind.NIERUCHOMOSC:
        # Ogłoszeniodawcy wrzucają wynajem do działu sprzedaży — tytuł wie lepiej
        if LEASE_IN_TITLE.search(title):
            transaction = TransactionType.DZIERZAWA
        elif RENT_IN_TITLE.search(title):
            transaction = TransactionType.WYNAJEM
        elif (
            transaction == TransactionType.SPRZEDAZ
            and price is not None
            and price < MIN_SALE_PRICE
            and property_type in (PropertyType.MIESZKANIE, PropertyType.DOM, PropertyType.POKOJ)
        ):
            transaction = TransactionType.WYNAJEM

    # Grunt rolny w dzierżawie potrafi kosztować 100 zł za 29 hektarów, czyli
    # 0,0034 zł/m². Zaokrąglone do groszy dawało to 0,00 — a zero w tym polu
    # windowało takie oferty na sam szczyt listy „najtańsze za m²".
    price_per_m2 = None
    if price and area and area > 1:
        per_meter = price / area
        price_per_m2 = round(per_meter, 2) if per_meter >= 1 else round(per_meter, 4) or None

    # --- telefony ---
    phones: list[PhoneNumber] = []
    for value in raw.phones_raw:
        parsed = parse_phone(value, origin="api")
        if parsed:
            phones.append(parsed)
    if not phones and description:
        phones = extract_phones(description, origin="opis")
    if not phones and raw.kind != OfferKind.NIERUCHOMOSC:
        phones = extract_phones(f"{title} {description}", origin="opis")
    phones = list({p.e164: p for p in phones}.values())

    # --- licytacje / przetargi ---
    case_number = raw.case_number or (
        extract_case_number(f"{title} {description[:3000]}")
        if raw.kind in (OfferKind.LICYTACJA, OfferKind.PRZETARG)
        else None
    )
    opening_price = raw.opening_price
    if raw.kind in (OfferKind.LICYTACJA, OfferKind.PRZETARG, OfferKind.WYKAZ) and price is None:
        price = opening_price or raw.estimate_value

    # --- działka ewidencyjna ---
    extra = dict(raw.extra or {})
    if property_type in LAND_TYPES or raw.kind in (OfferKind.LICYTACJA, OfferKind.PRZETARG):
        parcels = _parcel_numbers(f"{title} {description[:3000]}")
        if parcels:
            extra["dzialki_ewidencyjne"] = parcels
        register = REGISTER_UNIT_RE.search(f"{title} {description[:3000]}")
        if register:
            extra["obreb"] = clean(register.group(1))[:60]

    data = {
        "source_key": raw.source_key,
        "external_id": str(raw.external_id),
        "url": raw.url[:800],
        "kind": raw.kind,
        "transaction": transaction,
        "property_type": property_type,
        "title": title[:600],
        "description": description[:20000] or None,
        "images": [i for i in raw.images if isinstance(i, str)][:12],
        "price": price,
        "currency": raw.currency or "PLN",
        "price_per_m2": price_per_m2,
        "area": area,
        "plot_area": plot_area,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors_total,
        "year_built": year_built,
        "building_type": clean(raw.building_type or "") or None,
        "market": market,
        "voivodeship": place.voivodeship,
        "county": place.county,
        "commune": place.commune,
        "city": place.city,
        "district": place.district,
        "street": place.street,
        "teryt": place.teryt,
        "lat": raw.lat,
        "lon": raw.lon,
        # Współrzędne prosto z portalu są dokładniejsze niż nasze geokodowanie
        # po adresie — oznaczamy je, żeby mapa nie podpisywała ich jako
        # przybliżonych i żeby geokoder ich nie nadpisywał.
        "geo_precision": "portal" if (raw.lat and raw.lon) else None,
        "seller_type": raw.seller_type or SellerType.NIEZNANY,
        "seller_name": clean(raw.seller_name or "")[:300] or None,
        "contact_email": clean(raw.contact_email or "")[:200] or None,
        "published_at": raw.published_at,
        "source_updated_at": raw.source_updated_at,
        "event_date": raw.event_date,
        "deadline": raw.deadline,
        "opening_price": opening_price,
        "estimate_value": raw.estimate_value,
        "deposit": raw.deposit,
        "case_number": (case_number or "")[:120] or None,
        "authority": clean(raw.authority or "")[:300] or None,
        "share": clean(raw.share or "")[:32] or None,
        "extra": extra,
        "raw": raw.raw if isinstance(raw.raw, dict) else {},
    }
    # Źródło, które nie pozwala szukać ulicy w treści (bo treść zaczyna się od
    # adresu instytucji), zgłasza brak ulicy jako ustalenie, nie jako niewiedzę.
    cleared = () if (place.street or raw.street_from_body) else ("street",)
    return NormalizedListing(data=data, phones=phones, raw=raw, cleared=cleared)

"""Normalizacja surowych ofert do kształtu tabeli `listings`.

Tu dzieje się cała brudna robota: uzupełnianie brakujących parametrów z opisu,
rozpoznanie miejscowości i dzielnicy, przeliczenie ceny za m², wyrzucenie ofert
spoza regionu oraz wyciągnięcie telefonów.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..scrapers.base import RawListing
from ..utils.geo import (
    detect_location,
    detect_opole_district,
    extract_street,
    region_phrase,
    resolve_place,
)
from ..utils.phones import PhoneNumber, extract_phones, parse_phone
from ..utils.text import (
    clean,
    extract_area,
    extract_case_number,
    extract_floor,
    extract_rooms,
    extract_year,
    strip_html,
)

MARKET_PRIMARY = re.compile(r"rynek pierwotn|pierwotny|od dewelopera|nowa inwestycja", re.I)
MARKET_SECONDARY = re.compile(r"rynek wtórn|wtorny|używan", re.I)

# Oferty, których nie chcemy w bazie (spam, usługi, „kupię")
NOISE = re.compile(
    r"\b(kupię|kupie|poszukuję|poszukuje|skup (mieszkań|nieruchomości)|zamienię|"
    r"kredyt|doradztwo|remont|usługi|sprzątanie|projekt wnętrz)\b",
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
}

#: Ogłoszenie „na sprzedaż" z ceną poniżej tej kwoty to niemal zawsze wynajem
#: wrzucony do złej kategorii portalu (albo cena „do negocjacji" wpisana jako 1).
MIN_SALE_PRICE = 15000

#: Tytuł mówiący wprost o wynajmie bije kategorię portalu — ogłoszeniodawcy
#: notorycznie wrzucają wynajem do działu sprzedaży.
RENT_IN_TITLE = re.compile(
    r"\bdo wynaj|\bna wynaj|\bwynajm|\bwynajem\b|\bdo zamieszkania od zaraz za\b", re.I
)
LEASE_IN_TITLE = re.compile(r"\bdzierżaw|\bwydzierżaw|\boddam w dzierżaw", re.I)


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


def normalize(
    raw: RawListing,
    *,
    voivodeship: str = "opolskie",
    require_region: bool = True,
) -> NormalizedListing | None:
    """Zwraca `NormalizedListing` albo `None`, jeśli oferta odpada."""
    title = clean(raw.title)
    description = strip_html(raw.description)
    if not title or not raw.url:
        return None

    haystack = f"{title} {description} {raw.location_text or ''}"

    if raw.kind == OfferKind.NIERUCHOMOSC and NOISE.search(title):
        return None

    # --- lokalizacja ---
    # Kolejność ma znaczenie: pole z portalu > tytuł > krótki opis lokalizacji >
    # pełny opis. Opis potrafi wspominać sąsiednie miasta w zupełnie innym
    # kontekście ("przy drodze krajowej Opole – Strzelce Opolskie", "15 minut
    # od Nysy") i bez tej kolejności oferta z Walidróg lądowała w Strzelcach.
    place = (
        resolve_place(raw.city)
        or detect_location(title)
        or resolve_place(raw.location_text)
        or detect_location(raw.location_text or "")
        or detect_location(haystack)
        or {}
    )
    city = _pick(place.get("city"), clean(raw.city))
    commune = _pick(place.get("commune"), clean(raw.commune))
    county = _pick(place.get("county"), clean(raw.county))
    district = _pick(
        clean(raw.district),
        place.get("district"),
        detect_opole_district(haystack) if (city or "").lower() == "opole" else None,
    )
    street = _pick(
        clean(raw.street),
        extract_street(title),
        extract_street(raw.location_text or ""),
        extract_street(description[:600]) if raw.street_from_body else None,
    )
    # Przedrostek „ul." przechowywany w bazie psuł geokodowanie (GUGiK zwraca
    # wtedy zero wyników), a w interfejsie i tak dokłada go szablon. Inne typy
    # — aleja, osiedle, plac — zostawiamy, bo zmieniają znaczenie adresu.
    if street:
        street = re.sub(r"^\s*(?:ul\.?|ulica)\s+", "", street, flags=re.I).strip(" .,") or None

    # Samo słowo „opolskie" gdziekolwiek w treści to za słaba przesłanka.
    # Strony ogólnopolskie (PKP, AMW) wypisują w stopce listę wszystkich
    # województw, przez co do wyników wchodziły działki z Leszna, Kalisza
    # i Lubania. Nazwy województwa szukamy więc tylko w polach opisujących
    # adres, a w pełnym opisie — wyłącznie w zwrocie „woj. opolskie".
    address_fields = f"{title} {raw.location_text or ''} {raw.city or ''}"
    in_region = (
        bool(place)
        or voivodeship.lower() in address_fields.lower()
        or bool(region_phrase(voivodeship).search(description))
    )
    if raw.region_assured:
        # Źródło zostało odpytane pod adresem zawężonym do województwa —
        # portal sam zagwarantował region, nawet jeśli w treści nie ma nazwy
        # miejscowości, której znamy. Odrzucanie takich ofert gubiło dane.
        in_region = True
    if require_region and not in_region:
        return None

    # --- parametry ---
    area = _pick(raw.area, extract_area(title), extract_area(description[:1500]))
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

    property_type = raw.property_type
    if property_type == PropertyType.INNE and raw.kind != OfferKind.PRZETARG:
        from ..scrapers.generic_html import guess_property_type

        property_type = guess_property_type(title, description[:800])

    # Metraż spoza rozsądnych granic dla danego typu odrzucamy i próbujemy
    # odczytać go jeszcze raz z tytułu — tam człowiek pisze prawdziwą liczbę.
    if not _plausible_area(area, property_type):
        from_title = extract_area(title)
        area = from_title if _plausible_area(from_title, property_type) else None

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

    price_per_m2 = round(price / area, 2) if price and area and area > 1 else None

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
    if raw.kind == OfferKind.LICYTACJA and price is None:
        price = opening_price or raw.estimate_value

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
        "plot_area": raw.plot_area,
        "rooms": rooms,
        "floor": floor,
        "floors_total": floors_total,
        "year_built": year_built,
        "building_type": clean(raw.building_type or "") or None,
        "market": market,
        "voivodeship": voivodeship if in_region else None,
        "county": county,
        "commune": commune,
        "city": city,
        "district": district,
        "street": street,
        "lat": raw.lat,
        "lon": raw.lon,
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
        "extra": raw.extra or {},
        "raw": raw.raw if isinstance(raw.raw, dict) else {},
    }
    # Źródło, które nie pozwala szukać ulicy w treści (bo treść zaczyna się od
    # adresu instytucji), zgłasza brak ulicy jako ustalenie, nie jako niewiedzę.
    cleared = () if (street or raw.street_from_body) else ("street",)
    return NormalizedListing(data=data, phones=phones, raw=raw, cleared=cleared)

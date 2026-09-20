"""Otodom.pl — największy zbiór ofert biur i deweloperów w Polsce.

Portal to aplikacja Next.js; komplet danych listy wyników siedzi w
`__NEXT_DATA__`, razem z paginacją i **pełną hierarchią administracyjną**
każdej oferty (województwo → powiat → gmina → miejscowość → dzielnica).
To ostatnie jest tu najcenniejsze: nie trzeba zgadywać lokalizacji z tytułu,
bo portal podaje ją wprost i poprawnie.

**Pokrycie całej Polski.** Wyszukiwanie `cala-polska` stronicuje do końca —
sprawdzone na żywo: mieszkania na sprzedaż to 150 862 oferty na 2096 stronach
i strona 2096 faktycznie się otwiera. Nie ma więc potrzeby dzielenia zapytań
po województwach; zwykły skan bierze kilka pierwszych stron posortowanych od
najnowszych, a przebieg głęboki schodzi do ostatniej strony.

Ścieżka w JSON-ie zmieniała się już kilka razy, więc zamiast jednej sztywnej
lokalizacji przeszukujemy drzewo w poszukiwaniu kolekcji z polami typowymi dla
ogłoszenia. To przeżywa większość przemeblowań frontu.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..models import OfferKind, PropertyType, SellerType, TransactionType
from ..utils.text import clean, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext
from .generic_html import guess_property_type

BASE = "https://www.otodom.pl"

#: Wyszukiwania, które razem obejmują cały zasób portalu.
#: (fragment adresu: transakcja, typ, nasz typ, nasza transakcja)
SEARCHES: list[tuple[str, str, PropertyType, TransactionType]] = [
    ("sprzedaz", "mieszkanie", PropertyType.MIESZKANIE, TransactionType.SPRZEDAZ),
    ("sprzedaz", "dom", PropertyType.DOM, TransactionType.SPRZEDAZ),
    ("sprzedaz", "dzialka", PropertyType.DZIALKA, TransactionType.SPRZEDAZ),
    ("sprzedaz", "lokal", PropertyType.LOKAL, TransactionType.SPRZEDAZ),
    ("sprzedaz", "haleimagazyny", PropertyType.HALA, TransactionType.SPRZEDAZ),
    ("sprzedaz", "garaz", PropertyType.GARAZ, TransactionType.SPRZEDAZ),
    ("wynajem", "mieszkanie", PropertyType.MIESZKANIE, TransactionType.WYNAJEM),
    ("wynajem", "dom", PropertyType.DOM, TransactionType.WYNAJEM),
    ("wynajem", "lokal", PropertyType.LOKAL, TransactionType.WYNAJEM),
    ("wynajem", "haleimagazyny", PropertyType.HALA, TransactionType.WYNAJEM),
    ("wynajem", "pokoj", PropertyType.POKOJ, TransactionType.WYNAJEM),
    ("wynajem", "garaz", PropertyType.GARAZ, TransactionType.WYNAJEM),
]

#: `estate` z portalu -> nasz typ nieruchomości
ESTATES: dict[str, PropertyType] = {
    "FLAT": PropertyType.MIESZKANIE,
    "HOUSE": PropertyType.DOM,
    "TERRAIN": PropertyType.DZIALKA,
    "COMMERCIAL_PROPERTY": PropertyType.LOKAL,
    "HALL": PropertyType.HALA,
    "GARAGE": PropertyType.GARAZ,
    "ROOM": PropertyType.POKOJ,
    "INVESTMENT": PropertyType.MIESZKANIE,
}

ROOMS_WORDS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
               "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10}

#: `floorNumber` przychodzi jako „FLOOR_3", „GROUND", „CELLAR", „GARRET"
FLOOR_WORDS = {"GROUND": 0, "CELLAR": -1, "BASEMENT": -1, "GARRET": 99}

AGENCY_MARKERS = {"AGENCY", "BUSINESS"}
LISTING_MARKERS = {"slug", "title"}

#: Ile pozycji portal oddaje na stronę (maksimum przyjmowane przez wyszukiwarkę).
PAGE_SIZE = 72

#: `Floor_no` z karty oferty: „floor_1", „ground_floor", „cellar", „garret".
DETAIL_FLOORS = {"ground_floor": 0, "cellar": -1, "garret": 99, "attic": 99}


class OtodomScraper(BaseScraper):
    key = "otodom"
    name = "Otodom.pl"
    base_url = BASE
    kind = OfferKind.NIERUCHOMOSC

    def _region_slugs(self, ctx: ScrapeContext) -> list[str]:
        """Fragmenty adresu opisujące obszar wyszukiwania.

        Bez zawężania pytamy raz o całą Polskę. Gdy instancja jest zawężona do
        wybranych województw, pytamy osobno o każde — wtedy portal sam odsiewa
        resztę kraju i nie ściągamy danych, które i tak odrzucimy.
        """
        if self.config.get("region_slug"):
            return [str(self.config["region_slug"])]
        if not ctx.voivodeships:
            return ["cala-polska"]
        return [str(entry["otodom_slug"]) for entry in ctx.regions if entry.get("otodom_slug")]

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        produced = 0
        searches = self.config.get("searches") or SEARCHES
        # (obszar, transakcja, typ) -> ile stron portal deklaruje; None = jeszcze
        # nie wiemy, 0 = sekcja wyczerpana
        limits: dict[tuple[str, str, str], int | None] = {}
        sections = [
            (region, *search)
            for region in self._region_slugs(ctx)
            for search in searches
        ]

        # Pętla po stronach jest **na zewnątrz**, a po sekcjach w środku: przy
        # zawężonym limicie mieszkania nie zjadają całego budżetu, a działki
        # i lokale nie zostają bez ani jednej oferty.
        page = 1
        while page <= ctx.max_pages and sections:
            for region_slug, transaction_slug, type_slug, ptype, ttype in list(sections):
                if produced >= ctx.max_items:
                    return
                key = (region_slug, transaction_slug, type_slug)
                declared = limits.get(key)
                if declared is not None and page > declared:
                    sections.remove((region_slug, transaction_slug, type_slug, ptype, ttype))
                    continue
                url = (
                    f"{BASE}/pl/wyniki/{transaction_slug}/{type_slug}/{region_slug}"
                    f"?page={page}&limit={PAGE_SIZE}&by=LATEST&direction=DESC"
                    f"&viewType=listing"
                )
                try:
                    tree = await self.html(url)
                except Exception:
                    sections.remove((region_slug, transaction_slug, type_slug, ptype, ttype))
                    continue
                data = self.next_data(tree)
                rows = self._find_items(data)
                if not rows:
                    sections.remove((region_slug, transaction_slug, type_slug, ptype, ttype))
                    continue
                # Portal deklaruje, ile stron ma dana sekcja — w trybie głębokim
                # idziemy do ostatniej i to jest cały jego zasób.
                limits[key] = self._total_pages(data) or page
                for row in rows:
                    item = self._parse(row, ptype, ttype)
                    if item:
                        yield item
                        produced += 1
                        if produced >= ctx.max_items:
                            return
            page += 1

    # ------------------------------------------------------------------ #
    def _total_pages(self, data: Any) -> int | None:
        pagination = self._find_pagination(data)
        value = (pagination or {}).get("totalPages")
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    def _find_pagination(self, data: Any, depth: int = 0) -> dict | None:
        if depth > 8 or not isinstance(data, (dict, list)):
            return None
        if isinstance(data, dict):
            if "totalPages" in data and "currentPage" in data:
                return data
            for value in data.values():
                found = self._find_pagination(value, depth + 1)
                if found:
                    return found
            return None
        for element in data:
            found = self._find_pagination(element, depth + 1)
            if found:
                return found
        return None

    def _find_items(self, data: Any, depth: int = 0) -> list[dict]:
        """Rekurencyjnie znajduje listę ofert w `__NEXT_DATA__`."""
        if depth > 8 or data is None:
            return []
        if isinstance(data, list):
            hits = [
                row for row in data
                if isinstance(row, dict) and LISTING_MARKERS <= set(row) and
                ("totalPrice" in row or "areaInSquareMeters" in row or "price" in row)
            ]
            if len(hits) >= 3:
                return hits
            for element in data:
                found = self._find_items(element, depth + 1)
                if found:
                    return found
            return []
        if isinstance(data, dict):
            for key in ("items", "searchAds", "ads", "organic", "data", "pageProps", "props"):
                if key in data:
                    found = self._find_items(data[key], depth + 1)
                    if found:
                        return found
            for value in data.values():
                if isinstance(value, (dict, list)):
                    found = self._find_items(value, depth + 1)
                    if found:
                        return found
        return []

    # ------------------------------------------------------------------ #
    @staticmethod
    def _hierarchy(location: dict) -> dict[str, str]:
        """Rozkłada `reverseGeocoding` na województwo, powiat, gminę i miejscowość.

        Portal podaje tu rzeczywisty adres administracyjny, a nie „najbliższe
        duże miasto": działka w Bezrzeczu ma w polu `address.city` wpisany
        Szczecin, a w hierarchii — gminę Dobra (Szczecińska) w powiecie
        polickim. Ta druga informacja jest prawdziwa i tę bierzemy.
        """
        mapping = {
            "voivodeship": "voivodeship",
            "county": "county",
            "commune": "commune",
            "city_or_village": "city",
            "district": "district",
        }
        out: dict[str, str] = {}
        levels = ((location.get("reverseGeocoding") or {}).get("locations")) or []
        for level in levels:
            if not isinstance(level, dict):
                continue
            field = mapping.get(str(level.get("locationLevel") or ""))
            name = clean(level.get("name") or "")
            if field and name:
                out.setdefault(field, name)
        return out

    def _parse(self, row: dict, ptype: PropertyType, ttype: TransactionType) -> RawListing | None:
        slug = row.get("slug")
        if not slug:
            return None
        url = f"{BASE}/pl/oferta/{slug}"
        title = clean(row.get("title") or "")
        if not title:
            return None

        price = parse_number(self.dig(row, "totalPrice", "value") or row.get("price"))
        if row.get("hidePrice"):
            price = None          # „cena na zapytanie" to brak ceny, a nie zero
        rent = parse_number(self.dig(row, "rentPrice", "value"))
        area = parse_number(row.get("areaInSquareMeters"))
        plot_area = parse_number(row.get("terrainAreaInSquareMeters"))

        rooms = row.get("roomsNumber")
        if isinstance(rooms, str):
            rooms = ROOMS_WORDS.get(rooms.upper())

        floor = None
        floor_raw = row.get("floorNumber")
        if isinstance(floor_raw, (int, float)):
            floor = int(floor_raw)
        elif isinstance(floor_raw, str):
            key = floor_raw.upper().replace("FLOOR_", "")
            floor = FLOOR_WORDS.get(key, FLOOR_WORDS.get(floor_raw.upper()))
            if floor is None and key.isdigit():
                floor = int(key)

        location = row.get("location") or {}
        address = self.dig(location, "address", default={}) or {}
        place = self._hierarchy(location)
        city = place.get("city") or clean(self.dig(address, "city", "name", default="") or "")
        street = clean(self.dig(address, "street", "name", default="") or "") or None
        district = place.get("district") or clean(
            self.dig(address, "district", "name", default="") or ""
        ) or None

        agency = row.get("agency") if isinstance(row.get("agency"), dict) else None
        advertiser = str(
            (agency or {}).get("type") or row.get("extendedAdvertiserType") or ""
        ).upper()
        if row.get("isPrivateOwner"):
            seller_type = SellerType.PRYWATNA
        elif advertiser in AGENCY_MARKERS or agency:
            seller_type = SellerType.POSREDNIK
        elif advertiser in ("DEVELOPER",) or str(row.get("developmentId") or "0") not in ("0", ""):
            seller_type = SellerType.DEWELOPER
        else:
            seller_type = SellerType.NIEZNANY

        images = [
            img.get("large") or img.get("medium") or img.get("small")
            for img in (row.get("images") or [])
            if isinstance(img, dict)
        ]

        estate = ESTATES.get(str(row.get("estate") or "").upper())
        property_type = estate or ptype
        if property_type == PropertyType.INNE:
            property_type = guess_property_type(title)

        return RawListing(
            external_id=str(row.get("id") or slug),
            url=url,
            source_key=self.key,
            title=title,
            description=clean(row.get("shortDescription") or row.get("description") or "") or None,
            price=price,
            area=area,
            plot_area=plot_area,
            rooms=int(rooms) if isinstance(rooms, (int, float)) else None,
            floor=floor,
            city=city or None,
            district=district,
            street=street,
            commune=place.get("commune"),
            county=place.get("county")
            or clean(self.dig(address, "county", "name", default="") or "")
            or None,
            voivodeship=place.get("voivodeship")
            or clean(self.dig(address, "province", "name", default="") or "")
            or None,
            lat=self.dig(location, "coordinates", "latitude"),
            lon=self.dig(location, "coordinates", "longitude"),
            seller_type=seller_type,
            seller_name=clean((agency or {}).get("name") or "") or None,
            images=[i for i in images if i][:12],
            published_at=parse_datetime(row.get("dateCreatedFirst") or row.get("dateCreated")),
            source_updated_at=parse_datetime(row.get("pushedUpAt") or row.get("modifiedAt")),
            property_type=property_type,
            transaction=ttype,
            market="pierwotny" if row.get("market") == "PRIMARY" else
                   ("wtorny" if row.get("market") == "SECONDARY" else None),
            extra={
                "czynsz": rent,
                "promowana": bool(row.get("isPromoted")),
                "cena_za_m2": parse_number(self.dig(row, "pricePerSquareMeter", "value")),
                "biuro_id": (agency or {}).get("id"),
                "biuro_slug": (agency or {}).get("slug"),
            },
            raw=row,
        )


    # ------------------------------------------------------------------ #
    # Karta oferty
    # ------------------------------------------------------------------ #
    async def fetch_detail(self, url: str) -> dict:
        """Dociąga z karty oferty to, czego nie ma na liście wyników.

        Najważniejszy jest **numer telefonu**: Otodom podaje go wprost
        w `__NEXT_DATA__` karty — osobno numer agenta (`contactDetails.phones`)
        i centralę biura (`owner.phones`). Na liście wyników numeru nie ma
        w ogóle, więc bez wejścia na kartę kontakt przy ofertach Otodomu
        pozostawał pusty.

        Poza tym stąd bierzemy pełny opis (lista wyników ma tylko zajawkę),
        rok budowy, piętro, materiał i stan wykończenia.
        """
        tree = await self.html(url)
        ad = self.dig(self.next_data(tree), "props", "pageProps", "ad", default={}) or {}
        if not ad:
            return {}

        phones: list[str] = []
        for source in (ad.get("contactDetails") or {}, ad.get("owner") or {}):
            for phone in source.get("phones") or []:
                value = clean(phone)
                if value and value not in phones:
                    phones.append(value)

        target = ad.get("target") or {}
        characteristics = {
            c.get("key"): c.get("value")
            for c in (ad.get("characteristics") or [])
            if isinstance(c, dict)
        }

        floor = None
        floor_raw = (target.get("Floor_no") or [None])[0]
        if isinstance(floor_raw, str):
            floor = DETAIL_FLOORS.get(floor_raw)
            if floor is None and floor_raw.startswith("floor_"):
                tail = floor_raw.removeprefix("floor_")
                floor = int(tail) if tail.isdigit() else None

        location = ad.get("location") or {}
        place = self._hierarchy(location)

        out: dict[str, Any] = {
            "phones_raw": phones,
            "description": clean(ad.get("description") or "") or None,
            "year_built": parse_number(target.get("Build_year")),
            "floor": floor,
            "floors_total": parse_number(target.get("Building_floors_num")),
            "building_type": clean((target.get("Building_type") or [""])[0]) or None,
            "plot_area": parse_number(target.get("Terrain_area")),
            "lat": self.dig(location, "coordinates", "latitude"),
            "lon": self.dig(location, "coordinates", "longitude"),
            "market": {"secondary": "wtorny", "primary": "pierwotny"}.get(
                str(characteristics.get("market") or "").lower()
            ),
            "city": place.get("city"),
            "district": place.get("district"),
            "commune": place.get("commune"),
            "county": place.get("county"),
            "voivodeship": place.get("voivodeship"),
            "street": clean(self.dig(ad, "location", "address", "street", "name", default="") or "")
            or None,
        }
        return {k: v for k, v in out.items() if v not in (None, "", [])}

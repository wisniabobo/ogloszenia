"""Naprawa danych, które trafiły do bazy przed poprawkami.

Baza na serwerze żyje od miesięcy i ma w sobie ślady każdego błędu, który po
drodze poprawiliśmy: oferty z ceną 0, ceny za metr zaokrąglone do zera,
lokalizacje odczytane starym, zgadującym mechanizmem i wpisy bez województwa,
bo serwis obejmował wtedy jedno.

Skan tego nie naprawi: aktualizuje tylko te pola, które akurat przyszły ze
źródła, a ogłoszenie sprzed pół roku może już ze źródła nie przychodzić.
Stąd osobny przebieg, uruchamiany raz po wdrożeniu.

Naprawa jest **zachowawcza**: nie wymyśla danych, tylko przelicza to, co da
się wyliczyć z pól już zapisanych, i czyści wartości, o których wiadomo,
że są nieprawdziwe. Wszystkiego, czego nie da się rozstrzygnąć na miejscu,
nie rusza — dokończy to geokoder, pytając rejestr adresowy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..geo import detect_location, known_voivodeship, lookup, resolve_place
from ..models import AppState, DuplicateLink, Listing, ListingStatus, OfferKind, utcnow
from ..utils.text import clean
from .dedup import _match, _refresh_copy_count
from .normalize import LAND_TYPES, NAVIGATION_TITLE

#: Źródła, w których lokalizacja pochodzi z **pola** portalu, a nie z treści
#: ogłoszenia. Ich ofert naprawa nie rusza: portal podał miejscowość wprost
#: i jest ona prawdziwa.
#:
#: Reszta portali to zwykły HTML czytany selektorami — tam miejscowość bierze
#: się z tekstu karty i podlega tym samym pomyłkom, co ogłoszenia urzędowe.
#: Domiporta była tu wcześniej przez pomyłkę i przez to jej ogłoszenia ze
#: Starogardu Gdańskiego zostawały w Gdańsku mimo poprawki w rozpoznawaniu nazw.
PORTAL_SOURCES = {"olx", "otodom", "gethome", "gratka", "morizon"}

log = logging.getLogger("metruj.repair")


@dataclass
class RepairStats:
    prices: int = 0
    price_per_m2: int = 0
    land_area: int = 0
    regions: int = 0
    counties_as_cities: int = 0
    stale_points: int = 0
    relocated: int = 0
    navigation: int = 0
    unlinked: int = 0
    regeocode: int = 0
    dates_reparsed: int = 0
    dates_unswapped: int = 0
    dates_cleared: int = 0
    listed_at: int = 0
    cities_restored: int = 0
    titles: int = 0
    descriptions: int = 0
    concluded: int = 0
    old_notices: int = 0

    def __str__(self) -> str:
        return (
            f"ceny={self.prices} cena_za_m2={self.price_per_m2} "
            f"powierzchnia_gruntu={self.land_area} regiony={self.regions} "
            f"lokalizacje={self.relocated} powiat_jako_miasto={self.counties_as_cities} "
            f"nieaktualne_punkty={self.stale_points} śmieci={self.navigation} "
            f"rozpięte_kopie={self.unlinked} do_przeliczenia={self.regeocode} "
            f"daty_z_portalu={self.dates_reparsed} daty_odwrócone={self.dates_unswapped} "
            f"daty_wyczyszczone={self.dates_cleared} data_wystawienia={self.listed_at} "
            f"odtworzone_miejscowości={self.cities_restored} tytuły={self.titles} "
            f"opisy_ze_skryptami={self.descriptions} "
            f"po_terminie={self.concluded} stare_ogłoszenia={self.old_notices}"
        )


def _fix_prices(session: Session) -> tuple[int, int]:
    """Cena 0 znaczy „nie podano", a nie „za darmo" — i nie może sortować się
    przed ofertami z ceną. To samo z ceną za metr zaokrągloną do zera."""
    zeroed = session.execute(
        update(Listing).where(Listing.price <= 0).values(price=None, price_per_m2=None)
    ).rowcount
    # „1234567890 zł" i ceny powyżej granicy rozsądku. Odczyt odrzuca je od
    # razu, ale przy aktualizacji odrzucona cena nic nie zmieniała — i stara,
    # błędna zostawała w bazie.
    from .normalize import _plausible_price

    suspicious = session.execute(
        select(Listing.id, Listing.price).where(Listing.price >= 10_000_000)
    ).all()
    implausible = [row.id for row in suspicious if not _plausible_price(row.price)]
    for start in range(0, len(implausible), 500):
        session.execute(
            update(Listing).where(Listing.id.in_(implausible[start:start + 500]))
            .values(price=None, price_per_m2=None, deal_ratio=None, deal_level=None)
        )
    zeroed = (zeroed or 0) + len(implausible)

    recomputed = 0
    rows = session.execute(
        select(Listing.id, Listing.price, Listing.area).where(
            Listing.price.is_not(None), Listing.area.is_not(None), Listing.area > 1,
            or_(Listing.price_per_m2.is_(None), Listing.price_per_m2 <= 0),
        )
    ).all()
    for row in rows:
        per_meter = row.price / row.area
        value = round(per_meter, 2) if per_meter >= 1 else round(per_meter, 4)
        if not value:
            continue
        session.execute(
            update(Listing).where(Listing.id == row.id).values(price_per_m2=value)
        )
        recomputed += 1
    return int(zeroed or 0), recomputed


def _fix_land_area(session: Session) -> int:
    """Przy działce powierzchnia oferty to powierzchnia gruntu — portale
    podają ją raz w jednym, raz w drugim polu."""
    changed = session.execute(
        update(Listing)
        .where(Listing.property_type.in_(LAND_TYPES),
               Listing.area.is_(None), Listing.plot_area.is_not(None))
        .values(area=Listing.plot_area)
    ).rowcount or 0
    changed += session.execute(
        update(Listing)
        .where(Listing.property_type.in_(LAND_TYPES),
               Listing.plot_area.is_(None), Listing.area.is_not(None))
        .values(plot_area=Listing.area)
    ).rowcount or 0
    return int(changed)


def _fix_regions(session: Session) -> int:
    """Uzupełnia województwo i powiat na podstawie nazwy miejscowości.

    Dotyczy ofert zebranych, gdy serwis obejmował jedno województwo i pola
    po prostu nie wypełniał. Nazwę powtarzającą się w kraju rozstrzygamy
    powiatem, jeśli jest zapisany; bez niego zostawiamy pole puste i niech
    rozstrzygnie geokoder.
    """
    rows = session.execute(
        select(Listing.id, Listing.city, Listing.county, Listing.voivodeship)
        .where(Listing.city.is_not(None), Listing.voivodeship.is_(None))
    ).all()

    changed = 0
    for row in rows:
        hint = None
        if row.county:
            counties = [u for u in lookup(row.county) if u.kind == "powiat"]
            regions = {u.voivodeship for u in counties}
            if len(regions) == 1:
                hint = regions.pop()
        place = resolve_place(row.city, voivodeship_hint=hint)
        values = {
            key: value
            for key, value in (
                ("voivodeship", known_voivodeship(place.get("voivodeship"))),
                ("county", place.get("county") if not row.county else None),
                ("commune", place.get("commune")),
                ("teryt", place.get("teryt")),
            )
            if value
        }
        if not values:
            continue
        session.execute(update(Listing).where(Listing.id == row.id).values(**values))
        changed += 1
    return changed


def _mark_for_regeocode(session: Session, suspicious_only: bool = True) -> int:
    """Kasuje współrzędne tam, gdzie punkt nie zgadza się z województwem.

    Geokoder policzy je od nowa i przy okazji poprawi przynależność
    administracyjną, bo GUGiK oddaje ją razem z adresem. Współrzędnych
    z portalu nie ruszamy — są dokładniejsze od naszego geokodowania.
    """
    from ..geo import in_voivodeship

    rows = session.execute(
        select(Listing.id, Listing.lat, Listing.lon, Listing.voivodeship)
        .where(Listing.lat.is_not(None), Listing.geo_precision != "portal")
    ).all()

    stale = [
        row.id for row in rows
        if row.voivodeship and not in_voivodeship(row.lat, row.lon, row.voivodeship)
    ] if suspicious_only else [row.id for row in rows]

    for chunk_start in range(0, len(stale), 500):
        chunk = stale[chunk_start : chunk_start + 500]
        session.execute(
            update(Listing).where(Listing.id.in_(chunk)).values(
                lat=None, lon=None, geo_precision=None, geo_source=None
            )
        )
    return len(stale)


def _deactivate_navigation(session: Session) -> int:
    """Wygasza pozycje, które nigdy nie były ofertą, tylko elementem strony.

    Scraper AMW zapisał jedenaście razy blok „Polecane nieruchomości" jako
    ogłoszenie, każde z lokalizacją zgadniętą z przypadkowego słowa w menu.
    Normalizacja takich już nie wpuszcza; te, które zdążyły wejść, gasimy —
    a nie kasujemy, bo skasowania nie da się cofnąć, a wygaszenie owszem.
    """
    rows = session.execute(
        select(Listing.id, Listing.title).where(Listing.status == ListingStatus.AKTYWNA)
    ).all()
    junk = [row.id for row in rows if NAVIGATION_TITLE.match(row.title or "")]
    for start in range(0, len(junk), 500):
        session.execute(
            update(Listing)
            .where(Listing.id.in_(junk[start : start + 500]))
            .values(status=ListingStatus.ARCHIWALNA)
        )
    return len(junk)


def _relocate_text_sources(session: Session) -> int:
    """Czyta lokalizację od nowa tam, gdzie brała się z tekstu.

    Ogłoszenia urzędowe, komornicze i instytucjonalne nie mają pola „miasto" —
    lokalizację trzeba wyczytać z tytułu. Robił to stary mechanizm, który znał
    tylko jedno województwo i każdą nazwę spoza niego dopasowywał do czegoś
    w środku: działka w Lubaniu trafiła do Kędzierzyna-Koźla.

    Nową lokalizację przyjmujemy tylko wtedy, gdy rozpoznanie jest pewne
    (miasto albo wyraźna wskazówka) i różni się od zapisanej. Wtedy kasujemy
    też współrzędne, żeby geokoder policzył je od nowa i potwierdził region
    kodem TERYT z rejestru adresowego.
    """
    rows = session.execute(
        select(Listing.id, Listing.source_key, Listing.title, Listing.city,
               Listing.voivodeship, Listing.raw)
        .where(Listing.source_key.notin_(PORTAL_SOURCES))
    ).all()

    changed = 0
    for row in rows:
        if _structured_address(row.raw)["city"]:
            continue  # portal podał adres — ten ma pierwszeństwo przed tytułem
        found = detect_location(row.title)
        if not found.city or found.basis not in ("miasto", "wskazówka"):
            continue
        if found.city == row.city and found.voivodeship == row.voivodeship:
            continue
        session.execute(
            update(Listing).where(Listing.id == row.id).values(
                city=found.city,
                county=found.county,
                commune=found.commune,
                voivodeship=found.voivodeship,
                teryt=found.teryt,
                lat=None, lon=None, geo_precision=None, geo_source=None,
            )
        )
        changed += 1
    return changed


def _structured_address(raw) -> dict:
    """Adres z zapisanego ogłoszenia schema.org (Domiporta i inne portale HTML)."""
    from ..scrapers.generic_html import json_ld_fields

    element = raw.get("jsonld") if isinstance(raw, dict) else None
    if not isinstance(element, dict):
        return {"city": None, "street": None, "voivodeship": None}
    return json_ld_fields(element)


def _relocate_structured(session: Session) -> int:
    """Lokalizacja z adresu, który portal podał w danych strukturalnych.

    Parser czytał adres tylko z pierwszego poziomu ogłoszenia schema.org,
    a Domiporta trzyma go w `offers.itemOffered`. Miejscowość zgadywana
    z tytułu trafiała wtedy do innego województwa: „Górki" (Opolskie)
    do Zielonej Góry, Brożec do Broku, mieszkanie przy Rzeszowskiej
    w Opolu — do Rzeszowa. Surowe ogłoszenie mamy zapisane, więc adres
    czytamy od nowa i — gdy wychodzi inaczej — kasujemy współrzędne, żeby
    geokoder policzył je dla właściwej miejscowości.
    """
    from ..scrapers.base import RawListing
    from .location import resolve

    rows = session.execute(
        select(Listing.id, Listing.title, Listing.raw, Listing.city, Listing.voivodeship,
               Listing.county, Listing.street)
        .where(Listing.source_key.notin_(PORTAL_SOURCES))
    ).all()
    changed = 0
    for row in rows:
        address = _structured_address(row.raw)
        if not address["city"]:
            continue
        found = resolve(RawListing(
            external_id="", url="", title=row.title or "",
            city=address["city"], street=address["street"], voivodeship=address["voivodeship"],
        ))
        if (found.city, found.voivodeship, found.county) == (row.city, row.voivodeship, row.county):
            continue
        session.execute(
            update(Listing).where(Listing.id == row.id).values(
                city=found.city,
                county=found.county,
                commune=found.commune,
                voivodeship=found.voivodeship,
                teryt=found.teryt,
                street=found.street or row.street,
                lat=None, lon=None, geo_precision=None, geo_source=None,
            )
        )
        changed += 1
    return changed


def _restore_cities(session: Session) -> int:
    """Miejscowość z członów adresu, które zostały po usunięciu powiatu.

    Stary odczyt Gratki i Morizona brał człony adresu po kolei: z
    „Biestrzykowice, Świerczów, namysłowski" wychodziło miasto „namysłowski",
    dzielnica „Świerczów" i ulica „Biestrzykowice". Naprawa słusznie zdjęła
    powiat z pola miasta, ale miasto zostało puste, a karta pokazywała
    „ul. Biestrzykowice, Świerczów, —". Dzisiejszy odczyt wybiera
    miejscowość według rejestru TERYT — tu robimy to samo z tym, co zostało
    zapisane.
    """
    from ..scrapers.base import RawListing
    from ..scrapers.ringier import _is_county, _is_place
    from .location import resolve

    rows = session.execute(
        select(Listing.id, Listing.title, Listing.voivodeship, Listing.county,
               Listing.district, Listing.street)
        .where(
            Listing.city.is_(None),
            Listing.source_key.in_(("gratka", "morizon")),
            or_(Listing.district.is_not(None), Listing.street.is_not(None)),
        )
    ).all()
    changed = 0
    for row in rows:
        parts = [clean(p) for p in (row.street or "").split(",") if clean(p)]
        parts += [row.district] if row.district else []
        city = None
        for index in range(len(parts) - 1, -1, -1):
            if _is_place(parts[index]):
                city = parts.pop(index)
                break
        if city is None:
            continue  # rejestr nie zna żadnego członu — nie zgadujemy
        parts = [p for p in parts if p != city]
        district = parts.pop() if parts else None
        found = resolve(RawListing(
            external_id="", url="", title=row.title or "",
            city=city,
            county=row.county if row.county and _is_county(row.county) else None,
            voivodeship=row.voivodeship,
            district=district,
            street=", ".join(parts) or None,
        ))
        if not found.city:
            continue
        session.execute(
            update(Listing).where(Listing.id == row.id).values(
                city=found.city,
                county=found.county,
                commune=found.commune,
                voivodeship=found.voivodeship or row.voivodeship,
                teryt=found.teryt,
                district=found.district,
                street=found.street,
                lat=None, lon=None, geo_precision=None, geo_source=None,
            )
        )
        changed += 1
    return changed


def _unescape_titles(session: Session) -> int:
    """„Garden &amp; Villa" → „Garden & Villa" w tytułach już zapisanych."""
    from ..utils.text import unescape_html

    rows = session.execute(
        select(Listing.id, Listing.title).where(Listing.title.like("%&%;%"))
    ).all()
    changed = 0
    for row in rows:
        title = clean(unescape_html(row.title))
        if title and title != row.title:
            session.execute(update(Listing).where(Listing.id == row.id).values(title=title))
            changed += 1
    return changed


def _drop_script_descriptions(session: Session) -> int:
    """Opis, który jest menu i kodem strony, a nie opisem oferty.

    Odczyt karty oferty brał cały `<body>` razem ze skryptami — opis PKP
    zaczynał się od „A- A A+ O PKP S.A." i ekranów JavaScriptu. Pusty opis
    uzupełni następne przejście, już z poprawionym odczytem.
    """
    return int(session.execute(
        update(Listing).where(or_(
            Listing.description.like("%$(function%"),
            Listing.description.like("%$(document)%"),
            Listing.description.like("%{ margin%"),
        )).values(description=None)
    ).rowcount or 0)


def _close_concluded(session: Session) -> int:
    """Licytacje i przetargi po terminie przestają być aktywnymi ofertami."""
    rows = session.scalars(
        select(Listing).where(
            Listing.status == ListingStatus.AKTYWNA,
            Listing.kind.in_((OfferKind.LICYTACJA, OfferKind.PRZETARG)),
            or_(Listing.event_date.is_not(None), Listing.deadline.is_not(None)),
        )
    ).all()
    closed = 0
    for listing in rows:
        if listing.concluded:
            listing.status = ListingStatus.NIEAKTYWNA
            listing.removed_at = listing.removed_at or utcnow()
            closed += 1
    return closed


def _archive_old_notices(session: Session) -> int:
    """Ogłoszenia z BIP-ów sprzed roku, zapisane zanim rozpoznawaliśmy ich datę.

    „Wykaz nieruchomości … - 14.07.2023r." — litera tuż po roku sprawiała,
    że daty nie było, więc filtr wieku przepuszczał archiwum sprzed lat.
    """
    from datetime import datetime, timedelta

    from ..scrapers.bip import ANY_DATE, CASE_YEAR, TITLE_DATE
    from ..utils.text import parse_datetime

    limit = utcnow() - timedelta(days=365)
    rows = session.execute(
        select(Listing.id, Listing.title, Listing.published_at).where(
            Listing.status == ListingStatus.AKTYWNA,
            Listing.source_key.like("bip_%"),
            Listing.event_date.is_(None),
        )
    ).all()
    archived = []
    for row in rows:
        published = row.published_at
        if published is None:
            match = TITLE_DATE.search(row.title or "") or ANY_DATE.search(row.title or "")
            published = parse_datetime(match.group(1)) if match else None
            if published is None and (case := CASE_YEAR.search(row.title or "")):
                published = datetime(int(case.group(1)), 12, 31)  # „GG.1431.34-35.2013"
        if published is not None and published < limit:
            archived.append(row.id)
    for start in range(0, len(archived), 500):
        session.execute(
            update(Listing).where(Listing.id.in_(archived[start:start + 500]))
            .values(status=ListingStatus.ARCHIWALNA, removed_at=utcnow())
        )
    return len(archived)


def _recheck_duplicates(session: Session) -> int:
    """Rozpina połączenia, które nie przechodzą już dzisiejszych reguł.

    Przez jakiś czas sam odcisk parametrów (miasto + metraż + pokoje)
    wystarczał, żeby uznać dwie oferty za tę samą nieruchomość. W dużym
    mieście pasuje on do setek mieszkań, więc trzy różne kawalerki
    we Wrocławiu stały się jedną — a dwie z nich zniknęły z wyników.

    Przechodzimy więc po istniejących powiązaniach jeszcze raz i zostawiamy
    tylko te, które obroniłyby się przy dzisiejszych regułach. Rozpięta oferta
    wraca na listę jako samodzielna; nic nie jest kasowane.
    """
    links = list(session.scalars(select(DuplicateLink)))
    touched: set[int] = set()
    removed = 0

    for link in links:
        original = session.get(Listing, link.original_id)
        copy = session.get(Listing, link.copy_id)
        if original is None or copy is None:
            session.delete(link)
            removed += 1
            continue
        if _match(original, copy) is not None:
            touched.add(original.id)
            continue
        session.delete(link)
        removed += 1
        if copy.duplicate_of_id == original.id:
            copy.duplicate_of_id = None
            copy.is_original = True
        touched.add(original.id)

    session.flush()
    for original_id in touched:
        _refresh_copy_count(session, original_id)
    return removed


def _county_as_city(session: Session) -> int:
    """Przenosi nazwę powiatu z pola miejscowości tam, gdzie jej miejsce.

    Karty części portali opisują lokalizację samym powiatem („świecki,
    kujawsko-pomorskie"). Parser czytał to po pozycji i wstawiał powiat
    w pole miasta, a wtedy wszystkie oferty z powiatu trafiały na mapie
    w jeden punkt.
    """
    rows = session.execute(
        select(Listing.id, Listing.city, Listing.county).where(Listing.city.is_not(None))
    ).all()

    changed = 0
    for row in rows:
        units = lookup(row.city)
        if not units or any(unit.kind == "gmina" for unit in units):
            continue          # nazwa jest też miejscowością — nie ruszamy
        session.execute(
            update(Listing).where(Listing.id == row.id).values(
                city=None, county=row.county or row.city,
                lat=None, lon=None, geo_precision=None, geo_source=None,
            )
        )
        changed += 1
    return changed


def _recheck_coordinates(session: Session) -> int:
    """Kasuje punkty, które nie pasują już do adresu oferty.

    Po poprawieniu miejscowości współrzędne zostawały te sprzed poprawki —
    oferta z Opola stała tam, gdzie kiedyś rozpoznano ją jako Kamienicę.
    Efekt widać było na mapie: jedna pinezka zbierała oferty z kilku różnych
    miejscowości, odległych od siebie o kilkadziesiąt kilometrów.

    Porównujemy punkt oferty z tym, co geokoder zapisał dla **jej dzisiejszego**
    adresu. Gdy w cache'u nie ma takiego adresu albo punkt jest inny, punkt
    kasujemy i geokoder policzy go od nowa.
    """
    from ..geo.streets import split_house_number
    from ..models import GeocodeCache
    from ..utils.text import sha1

    cache = {
        row.query_hash: (row.lat, row.lon)
        for row in session.scalars(select(GeocodeCache))
        if row.lat is not None
    }

    rows = session.execute(
        select(Listing.id, Listing.city, Listing.street, Listing.district,
               Listing.voivodeship, Listing.lat, Listing.lon)
        .where(Listing.lat.is_not(None), Listing.geo_precision.notin_(["portal"]))
    ).all()

    stale: list[int] = []
    for row in rows:
        name, number = split_house_number(row.street or row.district)
        query = ", ".join(x for x in (clean(row.city), clean(name), clean(number)) if x)
        point = cache.get(sha1(query.lower(), row.voivodeship or "pl"))
        if point is None:
            stale.append(row.id)
            continue
        if abs(point[0] - row.lat) > 0.002 or abs(point[1] - row.lon) > 0.002:
            stale.append(row.id)

    for start in range(0, len(stale), 500):
        session.execute(
            update(Listing).where(Listing.id.in_(stale[start : start + 500])).values(
                lat=None, lon=None, geo_precision=None, geo_source=None
            )
        )
    return len(stale)


#: Skąd w danych portalu da się przeczytać oryginalną datę — dla źródeł, które
#: trzymają surową odpowiedź. Pole -> kolejne klucze do sprawdzenia.
RAW_DATE_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "olx": {"published_at": ("created_time",), "source_updated_at": ("last_refresh_time",)},
    "otodom": {"published_at": ("dateCreatedFirst", "dateCreated"),
               "source_updated_at": ("pushedUpAt", "modifiedAt")},
    "elicytacje_kas": {"published_at": ("publicationDateTime",),
                       "event_date": ("saleBeginDateTime",),
                       "deadline": ("depositDueDate", "saleEndDateTime")},
    "msig": {"published_at": ("dateOfPublication",)},
    "domiporta": {"published_at": ("jsonld.datePosted",)},
}

#: Źródła, które podają czas polski bez strefy („2026-09-21 19:13:17").
NAIVE_LOCAL_SOURCES = frozenset({"otodom"})

#: Pola z godziną zegarową w Polsce, nie chwilą w UTC.
LOCAL_TIME_FIELDS = frozenset({"event_date", "deadline"})

#: Źródła, które podają datę w zapisie „rok-miesiąc-dzień", ale nie trzymają
#: surowej odpowiedzi (Morizon i Gratka: „Dodane: 2026.09.07" z karty, GetHome:
#: ISO z danych strony). Stary parser zamienił im dzień z miesiącem przy
#: każdym dniu od 1 do 12 — i tylko wtedy.
YEAR_FIRST_SOURCES = ("gethome", "morizon", "gratka")

#: Znacznik jednorazowej naprawy — odwrócenie zamiany na danych już
#: poprawionych zamieniłoby je z powrotem.
DATES_MIGRATION = "naprawa-dat-iso-2026-09"


def _reparse_raw_dates(session: Session) -> int:
    """Czyta daty od nowa z oryginalnej odpowiedzi portalu (tam, gdzie ją mamy).

    Bezpieczne do wielokrotnego uruchamiania: wynik zależy wyłącznie od
    napisu z portalu i poprawionego parsera, a zapisujemy tylko to, co się
    zmieniło. Terminy licytacji i wadium to godziny zegarowe w Polsce, reszta
    — chwile w UTC.
    """
    from ..utils.text import parse_datetime, parse_local_datetime

    changed = 0
    for source_key, fields in RAW_DATE_KEYS.items():
        columns = [getattr(Listing, field) for field in fields]
        rows = session.execute(
            select(Listing.id, Listing.raw, *columns).where(Listing.source_key == source_key)
        ).all()
        for row in rows:
            raw = row.raw if isinstance(row.raw, dict) else {}
            if not raw:
                continue
            values = {}
            for field, keys in fields.items():
                text = next((v for v in (_raw_value(raw, k) for k in keys) if v), None)
                if not text:
                    continue
                if field in LOCAL_TIME_FIELDS:
                    moment = parse_local_datetime(text)
                else:
                    moment = parse_datetime(text, naive_local=source_key in NAIVE_LOCAL_SOURCES)
                if moment != getattr(row, field):
                    values[field] = moment
            if values:
                session.execute(update(Listing).where(Listing.id == row.id).values(**values))
                changed += 1
    return changed


def _raw_value(raw: dict, path: str):
    """Wartość z surowej odpowiedzi; „jsonld.datePosted" schodzi o poziom niżej."""
    value = raw
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _unswap(moment):
    """Odwraca zamianę dnia z miesiącem zrobioną przez stary parser."""
    if moment is None or moment.day > 12 or moment.day == moment.month:
        return moment
    try:
        return moment.replace(month=moment.day, day=moment.month)
    except ValueError:
        return moment


def _unswap_year_first(session: Session) -> int:
    """Jednorazowo odwraca zamienione daty w źródłach bez surowej odpowiedzi."""
    changed = 0
    rows = session.execute(
        select(Listing.id, Listing.published_at, Listing.source_updated_at)
        .where(Listing.source_key.in_(YEAR_FIRST_SOURCES))
    ).all()
    for row in rows:
        published, refreshed = _unswap(row.published_at), _unswap(row.source_updated_at)
        if published != row.published_at or refreshed != row.source_updated_at:
            session.execute(
                update(Listing).where(Listing.id == row.id)
                .values(published_at=published, source_updated_at=refreshed)
            )
            changed += 1
    return changed


def _clear_future_dates(session: Session) -> int:
    """Data wystawienia w przyszłości to pomyłka — lepiej pusta niż fałszywa."""
    from datetime import timedelta

    limit = utcnow() + timedelta(days=1)
    changed = session.execute(
        update(Listing).where(Listing.published_at > limit).values(published_at=None)
    ).rowcount or 0
    changed += session.execute(
        update(Listing).where(Listing.source_updated_at > limit).values(source_updated_at=None)
    ).rowcount or 0
    return int(changed)


def _fill_listed_at(session: Session) -> int:
    """Data wystawienia na rynku — patrz `Listing.listed_at`."""
    from ..models import listed_at_expression

    market_since = listed_at_expression()
    return int(
        session.execute(
            update(Listing)
            .where(Listing.listed_at.is_distinct_from(market_since))
            .values(listed_at=market_since)
        ).rowcount or 0
    )


def _fix_dates(session: Session, stats: RepairStats) -> None:
    stats.dates_reparsed = _reparse_raw_dates(session)
    done = session.get(AppState, DATES_MIGRATION)
    if done is None:
        stats.dates_unswapped = _unswap_year_first(session)
        session.add(AppState(key=DATES_MIGRATION, value=str(stats.dates_unswapped)))
    stats.dates_cleared = _clear_future_dates(session)
    stats.listed_at = _fill_listed_at(session)


def repair(session: Session, *, regeocode_all: bool = False) -> RepairStats:
    """Przelicza pola, które dało się policzyć źle, i czyści nieprawdziwe."""
    stats = RepairStats()
    _fix_dates(session, stats)
    stats.prices, stats.price_per_m2 = _fix_prices(session)
    stats.land_area = _fix_land_area(session)
    stats.navigation = _deactivate_navigation(session)
    stats.relocated = _relocate_text_sources(session) + _relocate_structured(session)
    stats.counties_as_cities = _county_as_city(session)
    stats.cities_restored = _restore_cities(session)
    stats.titles = _unescape_titles(session)
    stats.descriptions = _drop_script_descriptions(session)
    stats.concluded = _close_concluded(session)
    stats.old_notices = _archive_old_notices(session)
    stats.regions = _fix_regions(session)
    stats.stale_points = _recheck_coordinates(session)
    stats.unlinked = _recheck_duplicates(session)
    stats.regeocode = _mark_for_regeocode(session, suspicious_only=not regeocode_all)
    log.info("Naprawa danych: %s", stats)
    return stats

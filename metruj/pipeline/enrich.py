"""Wzbogacanie ofert: rozpoznanie pośrednika i budowa rejestru biur.

Rejestr biur nieruchomości nie jest przepisany z jakiejś listy — powstaje
z danych. Każda oferta oznaczona jako pośrednik dokłada nazwę i telefon;
warianty zapisu tej samej firmy ("ABC Nieruchomości", "ABC Nieruchomosci Sp. z o.o.",
"ABC NIERUCHOMOŚCI Opole") scalamy porównaniem rozmytym.
"""

from __future__ import annotations

import functools
import re

from rapidfuzz import fuzz, process
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Agency, Listing, OfferKind, SellerType
from ..settings import agencies_config
from ..utils.phones import PhoneNumber
from ..utils.text import clean, norm_key, slugify

#: minimalne podobieństwo nazw uznawane za to samo biuro
AGENCY_MATCH_THRESHOLD = 90

INSTITUTION_HINTS = re.compile(
    r"komornik|syndyk|urząd|urzad|gmina|miasto|starostwo|skarb państwa|kowr|"
    r"agencja mienia|zus\b|pkp\b|lasy państwowe|nadleśnictwo",
    re.I,
)
DEVELOPER_HINTS = re.compile(r"deweloper|development|investment|inwestycje|budownictwo", re.I)


@functools.lru_cache(maxsize=1)
def _agency_patterns() -> list[re.Pattern[str]]:
    cfg = agencies_config()
    return [re.compile(p, re.I) for p in cfg.get("wzorce_posrednika", [])]


@functools.lru_cache(maxsize=1)
def _known_networks() -> list[dict]:
    cfg = agencies_config()
    networks = list(cfg.get("sieci_ogolnopolskie", []))
    for entry in networks:
        entry["_keys"] = [norm_key(entry["name"])] + [norm_key(a) for a in entry.get("aliases", [])]
    return networks


def looks_like_agency(name: str | None) -> bool:
    if not name:
        return False
    return any(rx.search(name) for rx in _agency_patterns())


def detect_seller_type(data: dict) -> SellerType:
    """Ustala typ oferenta, gdy portal go nie podał albo podał zbyt ogólnie."""
    declared: SellerType = data.get("seller_type") or SellerType.NIEZNANY
    if declared in (SellerType.KOMORNIK, SellerType.SYNDYK, SellerType.URZAD, SellerType.INSTYTUCJA):
        return declared

    kind = data.get("kind")
    if kind == OfferKind.LICYTACJA and declared == SellerType.NIEZNANY:
        return SellerType.KOMORNIK
    if kind in (OfferKind.PRZETARG, OfferKind.WYKAZ) and declared == SellerType.NIEZNANY:
        return SellerType.URZAD

    name = data.get("seller_name") or ""
    authority = data.get("authority") or ""
    haystack = f"{name} {authority}"
    if INSTITUTION_HINTS.search(haystack):
        if re.search(r"komornik", haystack, re.I):
            return SellerType.KOMORNIK
        if re.search(r"syndyk", haystack, re.I):
            return SellerType.SYNDYK
        return SellerType.URZAD if re.search(r"gmina|urz|starost|miasto", haystack, re.I) \
            else SellerType.INSTYTUCJA
    if DEVELOPER_HINTS.search(haystack):
        return SellerType.DEWELOPER
    if looks_like_agency(name):
        return SellerType.POSREDNIK

    # opis sam się przyznaje
    description = (data.get("description") or "")[:1200]
    if re.search(r"prowizj|nasze biuro|zapraszamy do biura|licencja zawodowa", description, re.I):
        return SellerType.POSREDNIK
    if re.search(r"bez pośredników|bezpośrednio od właściciela|oferta prywatna",
                 f"{data.get('title', '')} {description}", re.I):
        return SellerType.PRYWATNA
    return declared


def _network_for(name: str) -> dict | None:
    key = norm_key(name)
    for network in _known_networks():
        if any(nk and nk in key for nk in network["_keys"]):
            return network
    return None


#: Ile biur najwyżej porównujemy rozmyto przy jednym dopasowaniu.
FUZZY_CANDIDATES = 300


def _fuzzy_candidates(session: Session, name: str, city: str | None) -> list[Agency]:
    """Biura, które w ogóle mogą być tym samym co `name`.

    Porównywanie rozmyte z **całym** rejestrem było do przyjęcia przy pięciuset
    biurami z jednego województwa. Krajowy katalog ma ich 13 tysięcy, a ofert
    są setki tysięcy — wczytywanie całej tabeli przy każdym ogłoszeniu robiło
    z zapisu operację kwadratową i zatykało skan.

    Zawężamy więc kandydatów do tych, którzy mają szansę pasować: to samo
    miasto albo ten sam początek nazwy. Warianty zapisu tej samej firmy
    („ABC Nieruchomości" / „ABC Nieruchomosci Sp. z o.o.") zawsze spełniają
    przynajmniej jeden z tych warunków.
    """
    head = clean(name)[:4]
    filters = [Agency.name.ilike(f"{head}%")] if len(head) >= 3 else []
    if city:
        filters.append(Agency.city == city)
    if not filters:
        return []
    return list(
        session.scalars(
            select(Agency).where(or_(*filters)).limit(FUZZY_CANDIDATES)
        )
    )


def _best_match(session: Session, name: str, city: str | None) -> Agency | None:
    candidates = _fuzzy_candidates(session, name, city)
    if not candidates:
        return None
    choices = {a.slug: norm_key(a.name) for a in candidates}
    best = process.extractOne(
        norm_key(name), choices, scorer=fuzz.token_set_ratio,
        score_cutoff=AGENCY_MATCH_THRESHOLD,
    )
    return next((a for a in candidates if a.slug == best[2]), None) if best else None


def match_agency(
    session: Session, name: str | None, *, city: str | None = None,
    phones: list[PhoneNumber] | None = None, website: str | None = None,
) -> Agency | None:
    """Znajduje lub tworzy biuro. Zwraca `None` dla ofert prywatnych."""
    name = clean(name or "")
    if not name or len(name) < 3:
        return None

    network = _network_for(name)
    canonical = network["name"] if network else name
    slug = slugify(f"{canonical}-{city}" if city and not network else canonical)

    agency = session.scalar(select(Agency).where(Agency.slug == slug))
    if agency is None:
        # dopasowanie rozmyte do już znanych biur (literówki, dopiski)
        agency = _best_match(session, canonical, city)

    if agency is None:
        agency = Agency(
            slug=slug,
            name=canonical[:300],
            city=city,
            website=website,
            verified=False,
            discovered=True,
            phones=[],
            source_hint="wykryte z ofert",
        )
        session.add(agency)
        session.flush()

    from ..models import utcnow

    agency.last_seen_at = utcnow()
    if city and not agency.city:
        agency.city = city
    if website and not agency.website:
        agency.website = website
    if phones:
        # Rejestr biur pokazuje numery w tej samej postaci, co lista ofert:
        # zamaskowanej, dopóki operator świadomie nie wyłączy maskowania.
        from ..settings import get_settings

        settings = get_settings()
        hide = settings.mask_phones or settings.store_phone_hash_only
        stored = set(agency.phones or [])
        for phone in phones:
            stored.add(phone.masked if hide else phone.e164)
        agency.phones = sorted(stored)[:10]
    return agency


def enrich_listing(session: Session, data: dict, phones: list[PhoneNumber]) -> dict:
    """Uzupełnia typ oferenta i podpina biuro. Modyfikuje i zwraca `data`."""
    data["seller_type"] = detect_seller_type(data)

    if data["seller_type"] in (SellerType.POSREDNIK, SellerType.DEWELOPER):
        agency = match_agency(
            session,
            data.get("seller_name"),
            city=data.get("city"),
            phones=phones,
        )
        data["agency_id"] = agency.id if agency else None
    else:
        data["agency_id"] = None
    return data


def upsert_agency_record(session: Session, raw) -> bool:
    """Zapisuje biuro z katalogu portalu. Zwraca True, gdy wpis jest nowy.

    Dane z katalogu są **pewniejsze** niż zgadywane z ogłoszeń: biuro samo je
    wpisało. Dlatego nadpisują to, co wcześniej wywnioskowaliśmy, i oznaczają
    wpis jako zweryfikowany.
    """
    from ..models import utcnow
    from ..settings import get_settings
    from ..utils.phones import parse_phone

    name = clean(raw.seller_name or raw.title)
    if not name:
        return False
    extra = raw.extra or {}
    slug = slugify(name)

    agency = session.scalar(select(Agency).where(Agency.slug == slug))
    if agency is None:
        agency = _best_match(session, name, raw.city)

    is_new = agency is None
    if agency is None:
        agency = Agency(slug=slug, name=name[:300])
        session.add(agency)
        session.flush()

    settings = get_settings()
    hide = settings.mask_phones or settings.store_phone_hash_only
    phones = []
    for value in raw.phones_raw or []:
        parsed = parse_phone(value, origin="katalog")
        if parsed:
            phones.append(parsed.masked if hide else parsed.e164)

    agency.name = name[:300]
    agency.city = raw.city or agency.city
    agency.voivodeship = raw.voivodeship or agency.voivodeship
    agency.address = extra.get("adres") or agency.address
    agency.postal_code = extra.get("kod_pocztowy") or agency.postal_code
    agency.profile_url = raw.url or agency.profile_url
    agency.listings_expected = int(extra.get("oferty_razem") or 0)
    agency.source_hint = f"katalog: {raw.source_key}"
    agency.verified = True          # biuro samo się zarejestrowało w katalogu
    agency.discovered = False
    agency.last_seen_at = utcnow()
    if phones:
        agency.phones = sorted(set((agency.phones or []) + phones))[:10]
    return is_new


def recount_agencies(session: Session) -> int:
    """Przelicza liczniki ofert i ustala miasto biura (po skanie).

    Miasto z pojedynczej oferty to lokalizacja *nieruchomości*, a nie siedziba
    biura — jedno ogłoszenie z Górek robiło z opolskiego biura firmę z Górek.
    Bierzemy więc miasto, które przy danym biurze występuje najczęściej, i to
    tylko wtedy, gdy ma wyraźną przewagę.
    """
    from collections import Counter

    from sqlalchemy import func

    counts = dict(
        session.execute(
            select(Listing.agency_id, func.count(Listing.id))
            .where(Listing.agency_id.is_not(None))
            .group_by(Listing.agency_id)
        ).all()
    )

    cities: dict[int, Counter] = {}
    for agency_id, city, hits in session.execute(
        select(Listing.agency_id, Listing.city, func.count(Listing.id))
        .where(Listing.agency_id.is_not(None), Listing.city.is_not(None))
        .group_by(Listing.agency_id, Listing.city)
    ).all():
        cities.setdefault(agency_id, Counter())[city] = hits

    updated = 0
    for agency in session.scalars(select(Agency)):
        changed = False
        new_count = int(counts.get(agency.id, 0))
        if agency.listings_count != new_count:
            agency.listings_count = new_count
            changed = True

        if not agency.verified:
            ranking = (cities.get(agency.id) or Counter()).most_common(2)
            if ranking:
                top_city, top_hits = ranking[0]
                runner_up = ranking[1][1] if len(ranking) > 1 else 0
                # przewaga musi być wyraźna, inaczej wolimy nie zgadywać
                if top_hits > runner_up and agency.city != top_city:
                    agency.city = top_city
                    changed = True
        updated += int(changed)
    return updated

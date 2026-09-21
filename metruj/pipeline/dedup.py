"""Deduplikacja: rozróżnienie oferty ORYGINALNEJ od KOPII.

Ta sama nieruchomość potrafi wisieć na ośmiu portalach, w kilku biurach i pod
trzema różnymi cenami. Bot musi pokazać ją RAZ — z licznikiem kopii — bo inaczej
lista jest bezużyteczna.

Sygnały (od najmocniejszego):
  1. **telefon** — ten sam numer + zbliżone parametry = ta sama nieruchomość,
  2. **shingle opisu** — odporny na przestawienie zdań i drobne przeróbki,
  3. **odcisk parametrów** — miasto + metraż + pokoje + typ + transakcja.
     To jest *podpowiedź, nie dowód*: we Wrocławiu „wynajem, 30 m², 1 pokój"
     pasuje do kilkuset różnych mieszkań. Sam odcisk niczego nie łączy —
     musi go potwierdzić ulica, numer telefonu albo podobieństwo treści.
  4. **cena** — jako rozstrzygnięcie remisu (kopie często mają inną prowizję).

Kierunek pomyłki nie jest obojętny. Niepołączenie dwóch kopii pokazuje tę samą
nieruchomość dwa razy — to irytujące, ale uczciwe. Połączenie dwóch różnych
ofert **ukrywa jedną z nich** przed szukającym. Dlatego przy wątpliwości
zostawiamy je osobno.

Za oryginał uznajemy ofertę o **najwcześniejszej dacie publikacji**, a przy
remisie — ofertę prywatną przed pośrednikiem (tak samo działa nbot: oryginał to
ten, kto pierwszy wystawił).
"""

from __future__ import annotations

from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import DuplicateLink, Listing, OfferKind, SellerType
from ..utils.phones import PhoneNumber, phones_fingerprint
from ..utils.text import norm_key, sha1, shingle_hash

#: tolerancja metrażu przy porównaniu (m²)
AREA_TOLERANCE = 1.5
#: próg podobieństwa tytułu/opisu (0-100)
TEXT_THRESHOLD = 88
#: Ile podobieństwa treści wystarcza, żeby potwierdzić odcisk parametrów.
#: Niżej niż TEXT_THRESHOLD, bo parametry już się zgadzają — ale nie zero,
#: bo odcisk sam w sobie w dużym mieście nic nie znaczy.
CORROBORATION_THRESHOLD = 80
#: Poniżej tylu znaków opis jest zbyt ogólny, żeby cokolwiek potwierdzać.
MIN_DESCRIPTION = 200
#: okno czasowe, w którym w ogóle szukamy duplikatów
WINDOW_DAYS = 400


def compute_fingerprints(data: dict, phones: list[PhoneNumber]) -> dict:
    """Uzupełnia `fingerprint`, `phone_fingerprint` i `text_shingle`.

    Odcisk budujemy **tylko z cech, które podaje praktycznie każdy portal**:
    miejscowość, metraż, liczba pokoi, typ i rodzaj transakcji. Ulica i piętro
    do niego nie wchodzą, bo jeden serwis potrafi je znać, a drugi nie — i ta
    sama nieruchomość dostawała dwa różne odciski. Zamiast tego ulica i cena
    są sprawdzane przy samym dopasowaniu (`_compatible`).

    Metraż jest tu najważniejszy: bez niego odcisk w ogóle nie powstaje.
    """
    area = data.get("area")
    city = norm_key(data.get("city"))
    if not area or not city:
        data["fingerprint"] = None
    else:
        data["fingerprint"] = sha1(
            city,
            f"{round(float(area), 1)}",
            str(data.get("rooms") or ""),
            str(data.get("property_type").value if data.get("property_type") else ""),
            str(data.get("transaction").value if data.get("transaction") else ""),
        )
    data["phone_fingerprint"] = phones_fingerprint(phones)
    text = f"{data.get('title', '')} {(data.get('description') or '')[:2500]}"
    data["text_shingle"] = shingle_hash(text)
    return data


def _similar_area(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return True
    return abs(a - b) <= AREA_TOLERANCE


def _candidates(session: Session, listing: Listing) -> list[Listing]:
    """Wąski zbiór kandydatów — bez tego porównywalibyśmy każdy z każdym."""
    since = listing.first_seen_at - timedelta(days=WINDOW_DAYS)
    filters = []
    if listing.phone_fingerprint:
        filters.append(Listing.phone_fingerprint == listing.phone_fingerprint)
    if listing.fingerprint:
        filters.append(Listing.fingerprint == listing.fingerprint)
    if listing.text_shingle:
        filters.append(Listing.text_shingle == listing.text_shingle)
    if listing.case_number:
        # obwieszczenia o tej samej sygnaturze bywają publikowane kilka razy
        # (I i II termin, wersja stacjonarna i elektroniczna)
        filters.append(Listing.case_number == listing.case_number)
    if not filters:
        return []

    stmt = (
        select(Listing)
        .where(
            Listing.id != listing.id,
            Listing.kind == listing.kind,
            Listing.first_seen_at >= since,
            or_(*filters),
        )
        .limit(60)
    )
    return list(session.scalars(stmt))


def _auctions_match(a: Listing, b: Listing) -> bool:
    """Dwa obwieszczenia to ta sama licytacja tylko przy twardej przesłance.

    Tytuły obwieszczeń bywają identyczne i zupełnie nieopisowe, więc jedyne
    wiarygodne sygnały to ta sama sygnatura akt albo ta sama cena wywołania
    i ten sam termin.
    """
    if a.case_number and b.case_number:
        return a.case_number == b.case_number
    same_price = (
        a.opening_price is not None
        and b.opening_price is not None
        and abs(a.opening_price - b.opening_price) < 1.0
    )
    same_date = a.event_date is not None and a.event_date == b.event_date
    return same_price and same_date


def _match(a: Listing, b: Listing) -> tuple[str, float] | None:
    """Zwraca (metoda, pewność) albo None."""
    if a.transaction != b.transaction or a.property_type != b.property_type:
        return None
    if a.kind == OfferKind.LICYTACJA:
        return ("licytacja", 0.95) if _auctions_match(a, b) else None

    # sprzeczne dane (inna ulica, inne piętro, rozjechana cena) wykluczają
    # połączenie niezależnie od tego, który sygnał je zaproponował
    if not _compatible(a, b):
        return None

    if a.phone_fingerprint and a.phone_fingerprint == b.phone_fingerprint:
        if _similar_area(a.area, b.area):
            return "phone", 0.97

    if a.text_shingle and a.text_shingle == b.text_shingle:
        return "text", 0.9

    if a.fingerprint and a.fingerprint == b.fingerprint:
        # Odcisk parametrów to podpowiedź, nie dowód. We Wrocławiu pasuje do
        # setek mieszkań naraz, bo metraże są okrągłe (30, 40, 50 m²), a cena
        # różni się w granicach, które przepuszcza `_compatible`. Bez tego
        # potwierdzenia trzy różne kawalerki na Legnickiej stawały się jedną.
        if a.street and b.street:
            return "fingerprint", 0.93     # ta sama ulica, ten sam metraż
        confirmation = _text_similarity(a, b)
        if confirmation is not None and confirmation >= CORROBORATION_THRESHOLD:
            return "fingerprint", round(min(0.92, confirmation / 100), 2)
        return None

    # dopasowanie po tekście wymaga znanego metrażu po obu stronach —
    # bez niego "lokal mieszkalny, Opole" pasowałby do każdego innego
    if a.area and b.area and _similar_area(a.area, b.area) and a.city and a.city == b.city:
        title_score = fuzz.token_set_ratio(norm_key(a.title), norm_key(b.title))
        if title_score >= TEXT_THRESHOLD:
            desc_score = 100.0
            if a.description and b.description:
                desc_score = fuzz.token_set_ratio(
                    norm_key(a.description[:1500]), norm_key(b.description[:1500])
                )
            if desc_score >= TEXT_THRESHOLD - 8:
                return "text", round(min(title_score, desc_score) / 100, 2)
    return None


def _text_similarity(a: Listing, b: Listing) -> float | None:
    """Jak bardzo treść obu ofert mówi o tym samym. `None` = nie ma z czego sądzić.

    Opis waży więcej niż tytuł: pośrednicy przepisują opis między portalami
    niemal dosłownie, a tytuł układają pod wyszukiwarkę każdego z osobna.
    Krótkie opisy („Do wynajęcia mieszkanie") nic nie potwierdzają, więc
    poniżej `MIN_DESCRIPTION` znaków w ogóle ich nie liczymy.
    """
    if a.description and b.description and (
        len(a.description) >= MIN_DESCRIPTION and len(b.description) >= MIN_DESCRIPTION
    ):
        return float(
            fuzz.token_set_ratio(norm_key(a.description[:1500]), norm_key(b.description[:1500]))
        )
    if a.title and b.title:
        return float(fuzz.token_set_ratio(norm_key(a.title), norm_key(b.title)))
    return None


#: o ile mogą różnić się ceny tej samej nieruchomości na dwóch portalach
MAX_PRICE_SPREAD = 0.25


def _compatible(a: Listing, b: Listing) -> bool:
    """Sprawdza cechy, których część portali nie podaje.

    Zasada: brak danych nie jest sprzecznością. Sprzecznością są dwie **różne**
    znane wartości — inna ulica albo cena rozjeżdżająca się o ćwierć.
    """
    if a.street and b.street and norm_key(a.street) != norm_key(b.street):
        return False
    if a.floor is not None and b.floor is not None and a.floor != b.floor:
        return False
    if a.price and b.price:
        spread = abs(a.price - b.price) / max(a.price, b.price)
        if spread > MAX_PRICE_SPREAD:
            return False
    return True


def _is_earlier(a: Listing, b: Listing) -> bool:
    """Czy `a` powinna być oryginałem względem `b`."""
    a_date = a.published_at or a.first_seen_at
    b_date = b.published_at or b.first_seen_at
    if a_date != b_date:
        return a_date < b_date
    priority = {SellerType.PRYWATNA: 0, SellerType.KOMORNIK: 0, SellerType.SYNDYK: 0,
                SellerType.URZAD: 0, SellerType.DEWELOPER: 1}
    return priority.get(a.seller_type, 2) <= priority.get(b.seller_type, 2)


def link_duplicates(session: Session, listing: Listing) -> int:
    """Znajduje kopie/oryginały dla nowej oferty i spina je w jedną grupę.

    Zwraca liczbę nowo utworzonych powiązań.
    """
    if listing.kind == OfferKind.PRZETARG:
        return 0  # przetargi publikowane są w jednym miejscu — nie duplikujemy

    created = 0
    touched: set[int] = set()

    for other in _candidates(session, listing):
        verdict = _match(listing, other)
        if not verdict:
            continue
        method, score = verdict

        original, copy = (listing, other) if _is_earlier(listing, other) else (other, listing)
        original = _root_of(session, original)
        if original.id == copy.id:
            continue

        copy.is_original = False
        copy.duplicate_of_id = original.id
        original.is_original = True
        original.duplicate_of_id = None

        # kopie, które wskazywały na `copy`, przepinamy na nowy korzeń grupy
        for grandchild in session.scalars(
            select(Listing).where(Listing.duplicate_of_id == copy.id)
        ):
            grandchild.duplicate_of_id = original.id
            _ensure_link(session, original.id, grandchild.id, method, score * 0.9)

        if _ensure_link(session, original.id, copy.id, method, score):
            created += 1
        touched.add(original.id)

    for original_id in touched:
        _refresh_copy_count(session, original_id)
    return created


def _root_of(session: Session, listing: Listing) -> Listing:
    """Wchodzi na szczyt grupy duplikatów (grupa jest płaska: oryginał + kopie)."""
    seen: set[int] = set()
    current = listing
    while current.duplicate_of_id and current.duplicate_of_id not in seen:
        seen.add(current.id)
        parent = session.get(Listing, current.duplicate_of_id)
        if parent is None or parent.id == current.id:
            break
        current = parent
    return current


def _ensure_link(session: Session, original_id: int, copy_id: int, method: str, score: float) -> bool:
    if original_id == copy_id:
        return False
    exists = session.scalar(
        select(DuplicateLink).where(
            DuplicateLink.original_id == original_id, DuplicateLink.copy_id == copy_id
        )
    )
    if exists:
        return False
    session.add(
        DuplicateLink(original_id=original_id, copy_id=copy_id, method=method, score=round(score, 2))
    )
    return True


def _refresh_copy_count(session: Session, original_id: int) -> None:
    count = session.scalar(
        select(func.count(DuplicateLink.id)).where(DuplicateLink.original_id == original_id)
    ) or 0
    original = session.get(Listing, original_id)
    if original:
        original.copies_count = int(count)

"""Deduplikacja: rozróżnienie oferty ORYGINALNEJ od KOPII.

Ta sama nieruchomość potrafi wisieć na ośmiu portalach, w kilku biurach i pod
trzema różnymi cenami. Bot musi pokazać ją RAZ — z licznikiem kopii — bo inaczej
lista jest bezużyteczna.

Sygnały (od najmocniejszego):
  1. **telefon** — ten sam numer + zbliżone parametry = ta sama nieruchomość,
  2. **odcisk parametrów** — miasto + ulica + metraż + pokoje + piętro,
  3. **shingle opisu** — odporny na przestawienie zdań i drobne przeróbki,
  4. **cena** — jako rozstrzygnięcie remisu (kopie często mają inną prowizję).

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
#: okno czasowe, w którym w ogóle szukamy duplikatów
WINDOW_DAYS = 400


def compute_fingerprints(data: dict, phones: list[PhoneNumber]) -> dict:
    """Uzupełnia `fingerprint`, `phone_fingerprint` i `text_shingle`."""
    area = data.get("area")
    parts = [
        norm_key(data.get("city")),
        norm_key(data.get("street") or data.get("district") or ""),
        f"{round(float(area), 1)}" if area else "",
        str(data.get("rooms") or ""),
        str(data.get("floor") if data.get("floor") is not None else ""),
        str(data.get("property_type").value if data.get("property_type") else ""),
        str(data.get("transaction").value if data.get("transaction") else ""),
    ]
    strong = [p for p in parts if p]
    data["fingerprint"] = sha1(*parts) if len(strong) >= 3 else None
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


def _match(a: Listing, b: Listing) -> tuple[str, float] | None:
    """Zwraca (metoda, pewność) albo None."""
    if a.transaction != b.transaction or a.property_type != b.property_type:
        return None

    if a.phone_fingerprint and a.phone_fingerprint == b.phone_fingerprint:
        if _similar_area(a.area, b.area):
            return "phone", 0.97

    if a.fingerprint and a.fingerprint == b.fingerprint:
        return "fingerprint", 0.93

    if a.text_shingle and a.text_shingle == b.text_shingle:
        return "text", 0.9

    if _similar_area(a.area, b.area) and a.city and a.city == b.city:
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

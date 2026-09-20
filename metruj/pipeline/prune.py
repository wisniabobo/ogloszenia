"""Bezpieczne usuwanie ofert.

Oferta nie jest samotnym rekordem: jej kopie wskazują na nią przez
`duplicate_of_id`, a powiązania siedzą w `duplicate_links`. Zwykłe DELETE
kończy się naruszeniem klucza obcego („FOREIGN KEY constraint failed"),
dlatego najpierw trzeba odpiąć to, co na ofertę wskazuje.

Kolejność ma znaczenie i dlatego jest w jednym miejscu, a nie przepisywana
przy każdym kasowaniu.
"""

from __future__ import annotations

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from ..models import DuplicateLink, Favorite, Listing


def delete_listings(session: Session, listing_ids: list[int]) -> int:
    """Usuwa oferty razem z tym, co się na nie powołuje. Zwraca liczbę usuniętych."""
    if not listing_ids:
        return 0

    # 1. kopie, które wskazywały na usuwaną ofertę, przestają być kopiami
    session.execute(
        update(Listing)
        .where(Listing.duplicate_of_id.in_(listing_ids))
        .values(duplicate_of_id=None, is_original=True)
    )
    # 2. powiązania w obie strony
    session.execute(
        delete(DuplicateLink).where(
            or_(
                DuplicateLink.original_id.in_(listing_ids),
                DuplicateLink.copy_id.in_(listing_ids),
            )
        )
    )
    # 3. schowek
    session.execute(delete(Favorite).where(Favorite.listing_id.in_(listing_ids)))
    session.flush()

    removed = 0
    for listing in session.scalars(select(Listing).where(Listing.id.in_(listing_ids))):
        session.delete(listing)
        removed += 1
    return removed


def delete_by_source(session: Session, source_key: str) -> int:
    """Usuwa wszystko z danego źródła — przydaje się po zmianie jego klucza."""
    ids = [
        row[0]
        for row in session.execute(select(Listing.id).where(Listing.source_key == source_key))
    ]
    return delete_listings(session, ids)

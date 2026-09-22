"""Skąd wziąć numer kontaktowy do oferty.

Portale coraz częściej chowają numer za logowaniem — OLX na zapytanie o telefon
odpowiada wprost „Disallowed for this user". Numer w treści ogłoszenia ma
u nas zaledwie kilka procent ofert.

Jest jednak druga droga, i to zupełnie jawna: **katalog biur**, w którym
pośrednicy sami publikują swój numer. Jeśli ofertę wystawiło biuro, którego
numer znamy, to jest to numer kontaktowy do tej oferty — po prostu pochodzi
z rejestru firmy, a nie z treści ogłoszenia.

Jest i trzecia: ta sama nieruchomość wisi zwykle na kilku portalach, a nie
każdy z nich chowa numer. GetHome podaje go wprost przy 99% ofert. Skoro
deduplikacja rozpoznała, że to jedno i to samo mieszkanie, to numer z tamtego
ogłoszenia jest numerem do tej nieruchomości.

Dlatego każdy numer niesie ze sobą **pochodzenie**, a interfejs mówi wprost,
czy to numer z ogłoszenia, czy centrala biura, czy bliźniacze ogłoszenie
z innego portalu. Bez tego rozróżnienia podpowiadalibyśmy numer, sugerując,
że stoi w tym konkretnym ogłoszeniu.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import object_session

from .models import Listing

#: etykiety pochodzenia pokazywane użytkownikowi
ORIGIN_LABELS = {
    "ogloszenie": "z ogłoszenia",
    "api": "z ogłoszenia",
    "opis": "z treści ogłoszenia",
    "katalog": "centrala biura",
    "biuro": "centrala biura",
    "blizniacze": "z bliźniaczego ogłoszenia",
}


@dataclass(slots=True)
class Contact:
    masked: str
    origin: str
    label: str
    from_listing: bool

    def as_dict(self) -> dict:
        return {
            "numer": self.masked,
            "pochodzenie": self.origin,
            "opis": self.label,
            "z_ogloszenia": self.from_listing,
        }


def contacts_for(listing: Listing) -> list[Contact]:
    """Numery kontaktowe oferty: najpierw z ogłoszenia, potem z rejestru biur."""
    out: list[Contact] = []
    seen: set[str] = set()

    for phone in listing.phones or []:
        masked = phone.masked or "***"
        if masked in seen:
            continue
        seen.add(masked)
        origin = phone.origin or "ogloszenie"
        out.append(
            Contact(
                masked=masked,
                origin=origin,
                label=ORIGIN_LABELS.get(origin, "z ogłoszenia"),
                from_listing=True,
            )
        )

    agency = listing.agency
    if agency is not None:
        for number in (agency.phones or [])[:2]:
            if number in seen:
                continue
            seen.add(number)
            out.append(
                Contact(
                    masked=number,
                    origin="katalog",
                    label=f"centrala: {_short(agency.name)}",
                    from_listing=False,
                )
            )

    if out:
        return out

    # Dopiero gdy przy samym ogłoszeniu nie ma nic, sięgamy po bliźniacze
    # ogłoszenie tej samej nieruchomości z innego portalu. Tam numer bywa
    # wprost w ogłoszeniu albo w rejestrze biura, które je wystawiło.
    for twin in _twins(listing):
        skad = _source_label(twin)
        for phone in twin.phones or []:
            masked = phone.masked or "***"
            if masked in seen:
                continue
            seen.add(masked)
            out.append(
                Contact(
                    masked=masked,
                    origin="blizniacze",
                    label=f"z tej samej oferty na {skad}",
                    from_listing=False,
                )
            )
        twin_agency = twin.agency
        if not out and twin_agency is not None:
            for number in (twin_agency.phones or [])[:1]:
                if number in seen:
                    continue
                seen.add(number)
                out.append(
                    Contact(
                        masked=number,
                        origin="blizniacze",
                        label=f"centrala {_short(twin_agency.name)} · z {skad}",
                        from_listing=False,
                    )
                )
        if out:
            break
    return out


#: Etykieta pochodzenia stoi w wąskiej kolumnie pod numerem — pełne nazwy biur
#: („AFKPOL Biuro Obrotu Nieruchomościami i Wycen") łamałyby ją na cztery wiersze;
#: 40 znaków mieści się w dwóch, a pełna nazwa jest w dymku.
LABEL_MAX = 40


def _short(name: str) -> str:
    name = (name or "").strip()
    return name if len(name) <= LABEL_MAX else name[: LABEL_MAX - 1].rstrip(" ,-") + "…"


def _source_label(listing: Listing) -> str:
    """Nazwa portalu widoczna dla czytającego, nie klucz z konfiguracji."""
    source = listing.source
    if source is not None and source.name:
        return source.name.split(" — ")[0]
    return (listing.source_key or "").replace("_", " ")


def _twins(listing: Listing) -> list[Listing]:
    """Ogłoszenia tej samej nieruchomości na innych portalach.

    Deduplikacja wskazuje jedno ogłoszenie jako pierwotne, a resztę wiąże
    z nim przez `duplicate_of_id`. Szukamy więc i w górę, i w bok.
    """
    # Oferta bez powtórek nie ma bliźniaków — a to zdecydowana większość.
    # Bez tego sprawdzenia każda karta bez telefonu robiła osobne zapytanie.
    if not listing.duplicate_of_id and not listing.copies_count:
        return []
    session = object_session(listing)
    if session is None:
        return []
    root = listing.duplicate_of_id or listing.id
    rows = session.scalars(
        select(Listing)
        .where(
            Listing.id != listing.id,
            or_(Listing.id == root, Listing.duplicate_of_id == root),
        )
        .limit(8)
    )
    return [row for row in rows if row.phones or (row.agency and row.agency.phones)]


def has_any_contact(listing: Listing) -> bool:
    return bool(listing.phones) or bool(listing.agency and listing.agency.phones)

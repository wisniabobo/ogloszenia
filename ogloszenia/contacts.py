"""Skąd wziąć numer kontaktowy do oferty.

Portale coraz częściej chowają numer za logowaniem — OLX na zapytanie o telefon
odpowiada wprost „Disallowed for this user". Numer w treści ogłoszenia ma
u nas zaledwie kilka procent ofert.

Jest jednak druga droga, i to zupełnie jawna: **katalog biur**, w którym
pośrednicy sami publikują swój numer. Jeśli ofertę wystawiło biuro, którego
numer znamy, to jest to numer kontaktowy do tej oferty — po prostu pochodzi
z rejestru firmy, a nie z treści ogłoszenia.

Dlatego każdy numer niesie ze sobą **pochodzenie**, a interfejs mówi wprost,
czy to numer z ogłoszenia, czy centrala biura. Bez tego rozróżnienia
podpowiadalibyśmy numer, sugerując, że stoi w ogłoszeniu.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Listing

#: etykiety pochodzenia pokazywane użytkownikowi
ORIGIN_LABELS = {
    "ogloszenie": "z ogłoszenia",
    "api": "z ogłoszenia",
    "opis": "z treści ogłoszenia",
    "katalog": "centrala biura",
    "biuro": "centrala biura",
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
                    label=f"centrala: {agency.name[:40]}",
                    from_listing=False,
                )
            )
    return out


def has_any_contact(listing: Listing) -> bool:
    return bool(listing.phones) or bool(listing.agency and listing.agency.phones)

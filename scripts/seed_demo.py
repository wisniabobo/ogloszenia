"""Wypełnia bazę danymi demonstracyjnymi (bez ruchu sieciowego).

Pozwala obejrzeć interfejs i przetestować deduplikację, historię cen oraz
alerty, zanim wypuścisz bota na żywe portale:

    python scripts/seed_demo.py
    ogl web
"""

from __future__ import annotations

import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ogloszenia.db import init_db, session_scope  # noqa: E402
from ogloszenia.models import (  # noqa: E402
    Listing,
    OfferKind,
    PriceHistory,
    PropertyType,
    SellerType,
    Source,
    TransactionType,
    utcnow,
)
from ogloszenia.pipeline.dedup import compute_fingerprints, link_duplicates  # noqa: E402
from ogloszenia.pipeline.enrich import enrich_listing, recount_agencies  # noqa: E402
from ogloszenia.pipeline.normalize import normalize  # noqa: E402
from ogloszenia.pipeline.runner import sync_sources  # noqa: E402
from ogloszenia.scrapers.base import RawListing  # noqa: E402

random.seed(17)

DEMO = [
    # (źródło, tytuł, opis, cena, oferent, nazwa, dni temu, rodzaj)
    ("olx", "Mieszkanie z charakterem | Blisko trasy spacerowe | po remoncie",
     "Mieszkanie z charakterem w centrum Opola, cisza, zieleń i wszystko w zasięgu spaceru. "
     "49 m2, 2 pokoje, 2 piętro z 3, blok. ul. Leona Powolnego. Kontakt 537 214 908.",
     629000, SellerType.POSREDNIK, "Opolskie Nieruchomości Premium", 41, OfferKind.NIERUCHOMOSC),
    ("otodom", "Mieszkanie 2 pokoje, centrum Opola, po remoncie",
     "Do sprzedania mieszkanie 49 m2 przy ul. Leona Powolnego w Opolu, 2 pokoje, 2 piętro z 3. "
     "Tel. 537 214 908.",
     645000, SellerType.POSREDNIK, "Opolskie Nieruchomości Premium", 38, OfferKind.NIERUCHOMOSC),
    ("morizon", "Komfortowe mieszkanie po remoncie — Opole, Powolnego",
     "Mieszkanie 49 m2, 2 pokoje, 2 piętro z 3, ul. Leona Powolnego, Opole. Telefon 537 214 908.",
     639000, SellerType.POSREDNIK, "Opolskie Nieruchomości Premium", 30, OfferKind.NIERUCHOMOSC),

    ("otodom", "Przestronne 4-pokojowe mieszkanie 75,28 m²",
     "Na sprzedaż przestronne i funkcjonalne 4-pokojowe mieszkanie o powierzchni 75,28 m2 "
     "przy ul. Wrocławskiej, Półwieś, Opole. 2 piętro z 2, blok. Kontakt 663 118 240.",
     749000, SellerType.PRYWATNA, None, 12, OfferKind.NIERUCHOMOSC),

    ("olx", "Dom wolnostojący 180 m2, Nysa, działka 900 m2",
     "Dom w Nysie, 180 m2, 6 pokoi, działka 900 m2, rok budowy 2008. Tel 601 447 882.",
     1090000, SellerType.PRYWATNA, None, 5, OfferKind.NIERUCHOMOSC),
    ("gratka", "Kawalerka do wynajęcia, Opole Zaodrze",
     "Kawalerka 28 m2 na Zaodrzu w Opolu, parter, umeblowana. Kontakt 792 330 115.",
     1800, SellerType.PRYWATNA, None, 2, OfferKind.NIERUCHOMOSC),
    ("nieruchomosci_online", "Działka budowlana 1200 m2, Turawa",
     "Działka budowlana 1200 m2 w Turawie, media w drodze. Biuro: 77 402 11 90.",
     195000, SellerType.POSREDNIK, "Dom i Grunt Biuro Nieruchomości", 120, OfferKind.NIERUCHOMOSC),
    ("domiporta", "Lokal użytkowy 120 m2, Kędzierzyn-Koźle, Rynek",
     "Lokal użytkowy 120 m2 w ścisłym centrum Kędzierzyna-Koźla, witryna od Rynku.",
     450000, SellerType.POSREDNIK, "Kozielskie Nieruchomości s.c.", 210, OfferKind.NIERUCHOMOSC),
    ("olx", "Mieszkanie 3 pokoje, Kluczbork, do remontu",
     "Mieszkanie 62 m2, 3 pokoje, 1 piętro, Kluczbork. Do remontu. Tel 604 771 220.",
     289000, SellerType.PRYWATNA, None, 68, OfferKind.NIERUCHOMOSC),
    ("morizon", "Hala magazynowa 800 m2, Strzelce Opolskie",
     "Hala magazynowa 800 m2 z zapleczem biurowym, Strzelce Opolskie, przy DK94.",
     2400000, SellerType.POSREDNIK, "Invest Silesia Nieruchomości Sp. z o.o.", 95,
     OfferKind.NIERUCHOMOSC),
    ("otodom", "Mieszkanie 2 pokoje, Brzeg, rynek pierwotny",
     "Nowe mieszkanie 52 m2 od dewelopera, Brzeg, 3 piętro z 4, rynek pierwotny.",
     412000, SellerType.DEWELOPER, "Brzeskie Inwestycje Development", 22, OfferKind.NIERUCHOMOSC),
]

AUCTIONS = [
    ("licytacje_komornik", "Pierwsza licytacja lokalu mieszkalnego — Nysa, ul. Rynek",
     "Komornik Sądowy przy Sądzie Rejonowym w Nysie ogłasza pierwszą licytację lokalu "
     "mieszkalnego o powierzchni 48,5 m2 położonego w Nysie. Suma oszacowania 240 000,00 zł, "
     "cena wywoławcza 180 000,00 zł, rękojmia 24 000,00 zł. Sygn. akt Km 1245/24.",
     180000, 240000, 24000, "Km 1245/24", 8, 14),
    ("licytacje_komornik", "Druga licytacja nieruchomości gruntowej — Prudnik",
     "Druga licytacja działki o powierzchni 1200 m2 w Prudniku. Suma oszacowania 150 000,00 zł, "
     "cena wywoławcza 100 000,00 zł, rękojmia 15 000,00 zł. Sygn. akt Km 45/24.",
     100000, 150000, 15000, "Km 45/24", 15, 27),
    ("krz", "Obwieszczenie o sprzedaży z wolnej ręki — dom, Głubczyce",
     "Syndyk masy upadłości ogłasza sprzedaż z wolnej ręki nieruchomości zabudowanej domem "
     "jednorodzinnym o powierzchni 140 m2 w Głubczycach. Cena wywoławcza 320 000,00 zł, "
     "wadium 32 000,00 zł. Oferty do 30 dni od obwieszczenia.",
     320000, 410000, 32000, "VIII GUp 88/25", 21, 4),
]

TENDERS = [
    ("kowr", "Przetarg ustny nieograniczony — grunty rolne, gm. Pawłowiczki",
     "KOWR OT Opole ogłasza przetarg ustny nieograniczony na sprzedaż nieruchomości rolnej "
     "o powierzchni 12,4 ha położonej w gminie Pawłowiczki.", 620000, 30, 9),
    ("zus", "Ogłoszenie o sprzedaży nieruchomości — budynek biurowy, Opole",
     "ZUS Oddział w Opolu ogłasza sprzedaż budynku biurowego o powierzchni 1 240 m2 "
     "przy ul. Wrocławskiej w Opolu.", 4200000, 12, 18),
    ("amw", "Sprzedaż lokalu mieszkalnego po zasobie wojskowym — Brzeg",
     "Agencja Mienia Wojskowego przeznacza do sprzedaży lokal mieszkalny 54 m2 w Brzegu.",
     245000, 44, 6),
]


def build() -> list[RawListing]:
    now = utcnow()
    items: list[RawListing] = []

    for i, (source, title, desc, price, seller, name, days, kind) in enumerate(DEMO):
        items.append(RawListing(
            external_id=f"demo-{i}", source_key=source, url=f"https://{source}.pl/oferta/demo-{i}",
            title=title, description=desc, price=float(price), kind=kind,
            seller_type=seller, seller_name=name,
            transaction=TransactionType.WYNAJEM if "wynaj" in title.lower() else TransactionType.SPRZEDAZ,
            published_at=now - timedelta(days=days, hours=random.randint(0, 20)),
            images=[],
        ))

    for i, (source, title, desc, opening, estimate, deposit, case, published, event) in enumerate(AUCTIONS):
        items.append(RawListing(
            external_id=f"lic-{i}", source_key=source, url=f"https://{source}.pl/obwieszczenie/{i}",
            title=title, description=desc, kind=OfferKind.LICYTACJA,
            seller_type=SellerType.KOMORNIK if source == "licytacje_komornik" else SellerType.SYNDYK,
            opening_price=float(opening), estimate_value=float(estimate), deposit=float(deposit),
            case_number=case, event_date=now + timedelta(days=event),
            published_at=now - timedelta(days=published),
            authority="Komornik Sądowy" if source == "licytacje_komornik" else "Syndyk masy upadłości",
        ))

    for i, (source, title, desc, price, published, deadline) in enumerate(TENDERS):
        items.append(RawListing(
            external_id=f"prz-{i}", source_key=source, url=f"https://{source}.pl/przetarg/{i}",
            title=title, description=desc, kind=OfferKind.PRZETARG, price=float(price),
            seller_type=SellerType.INSTYTUCJA, property_type=PropertyType.INNE,
            deadline=now + timedelta(days=deadline), published_at=now - timedelta(days=published),
            authority={"kowr": "KOWR OT Opole", "zus": "ZUS Oddział w Opolu",
                       "amw": "Agencja Mienia Wojskowego"}[source],
        ))
    return items


def main() -> None:
    init_db()
    inserted = 0
    with session_scope() as session:
        sync_sources(session)
        sources = {s.key: s for s in session.query(Source).all()}

        for raw in build():
            result = normalize(raw, require_region=True)
            if result is None:
                print(f"  pominięto (poza regionem): {raw.title[:60]}")
                continue
            compute_fingerprints(result.data, result.phones)
            enrich_listing(session, result.data, result.phones)

            source = sources.get(raw.source_key)
            listing = Listing(**result.data, source_id=source.id if source else None,
                              initial_price=result.data.get("price"))
            if raw.published_at:
                listing.first_seen_at = raw.published_at
            session.add(listing)
            session.flush()

            for phone in result.phones:
                from ogloszenia.models import Phone

                listing.phones.append(Phone(
                    e164=phone.e164, national=phone.national, masked=phone.masked,
                    hashed=phone.hashed, origin=phone.origin,
                ))

            # historia ceny — dla części ofert symulujemy obniżkę
            if listing.price and random.random() < 0.35:
                start = round(listing.price * 1.08, -3)
                listing.initial_price = start
                session.add(PriceHistory(listing_id=listing.id, price=start,
                                         changed_at=listing.first_seen_at))
                session.add(PriceHistory(listing_id=listing.id, price=listing.price,
                                         previous_price=start,
                                         changed_at=listing.first_seen_at + timedelta(days=9)))
            elif listing.price:
                session.add(PriceHistory(listing_id=listing.id, price=listing.price,
                                         changed_at=listing.first_seen_at))
            inserted += 1

        session.flush()
        linked = 0
        for listing in session.query(Listing).order_by(Listing.first_seen_at).all():
            linked += link_duplicates(session, listing)
        recount_agencies(session)

    print(f"Dodano {inserted} ofert, powiązano {linked} kopii.")
    print("Uruchom interfejs:  ogl web   (albo: python -m ogloszenia.cli web)")


if __name__ == "__main__":
    main()

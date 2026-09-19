from __future__ import annotations

from datetime import timedelta

from ogloszenia.models import OfferKind, PropertyType, SellerType, TransactionType, utcnow
from ogloszenia.pipeline.dedup import compute_fingerprints, link_duplicates
from ogloszenia.pipeline.enrich import detect_seller_type, looks_like_agency, match_agency
from ogloszenia.pipeline.normalize import normalize
from ogloszenia.scrapers.base import RawListing


def make_raw(**kw) -> RawListing:
    base = {
        "external_id": "1",
        "url": "https://example.pl/oferta/1",
        "title": "Mieszkanie 2-pokojowe, ul. Leona Powolnego, Opole",
        "source_key": "test",
        "description": "Ładne mieszkanie 49 m2, 2 pokoje, 2 piętro z 3. Kontakt 537 123 123.",
        "price": 629000.0,
    }
    base.update(kw)
    return RawListing(**base)


class TestNormalizacja:
    def test_uzupelnia_parametry_z_opisu(self):
        result = normalize(make_raw())
        assert result is not None
        data = result.data
        assert data["area"] == 49.0
        assert data["rooms"] == 2
        assert data["floor"] == 2
        assert data["floors_total"] == 3
        assert data["city"] == "Opole"
        assert data["street"] == "Leona Powolnego"
        assert data["price_per_m2"] == round(629000 / 49, 2)
        assert data["property_type"] == PropertyType.MIESZKANIE

    def test_wyciaga_telefon_z_opisu(self):
        result = normalize(make_raw())
        assert [p.e164 for p in result.phones] == ["+48537123123"]
        assert result.phones[0].masked == "537 *** ***"

    def test_odrzuca_oferte_spoza_regionu(self):
        raw = make_raw(title="Mieszkanie w Gdańsku", description="Wrzeszcz, 50 m2")
        assert normalize(raw) is None

    def test_przepuszcza_spoza_regionu_gdy_wylaczony_filtr(self):
        raw = make_raw(title="Mieszkanie w Gdańsku", description="Wrzeszcz, 50 m2")
        assert normalize(raw, require_region=False) is not None

    def test_odrzuca_ogloszenia_kupie(self):
        assert normalize(make_raw(title="Kupię mieszkanie w Opolu")) is None

    def test_licytacja_bierze_cene_wywolawcza(self):
        raw = make_raw(
            kind=OfferKind.LICYTACJA, price=None, opening_price=180000.0,
            estimate_value=240000.0, title="Licytacja lokalu, Nysa",
        )
        data = normalize(raw).data
        assert data["price"] == 180000.0
        assert data["city"] == "Nysa"


class TestRozpoznanieOferenta:
    def test_wzorzec_biura(self):
        assert looks_like_agency("ABC Nieruchomości Sp. z o.o.")
        assert not looks_like_agency("Jan")

    def test_typ_z_nazwy(self):
        data = {"seller_name": "XYZ Nieruchomości", "kind": OfferKind.NIERUCHOMOSC}
        assert detect_seller_type(data) == SellerType.POSREDNIK

    def test_licytacja_to_komornik(self):
        assert detect_seller_type({"kind": OfferKind.LICYTACJA}) == SellerType.KOMORNIK

    def test_bez_posrednikow_w_tytule(self):
        data = {
            "kind": OfferKind.NIERUCHOMOSC, "seller_name": "Anna",
            "title": "Mieszkanie bez pośredników", "description": "",
        }
        assert detect_seller_type(data) == SellerType.PRYWATNA

    def test_biuro_trafia_do_rejestru(self, session):
        agency = match_agency(session, "Testowe Nieruchomości", city="Opole")
        session.flush()
        again = match_agency(session, "TESTOWE NIERUCHOMOSCI", city="Opole")
        assert again.id == agency.id


class TestDeduplikacja:
    def _add(self, session, **kw):
        from ogloszenia.models import Listing

        raw = make_raw(**kw)
        result = normalize(raw)
        compute_fingerprints(result.data, result.phones)
        listing = Listing(**result.data)
        session.add(listing)
        session.flush()
        return listing

    def test_ta_sama_oferta_na_dwoch_portalach(self, session):
        first = self._add(
            session, external_id="A1", source_key="portal_a",
            url="https://a.pl/1", published_at=utcnow() - timedelta(days=3),
        )
        second = self._add(
            session, external_id="B1", source_key="portal_b",
            url="https://b.pl/1", published_at=utcnow() - timedelta(days=1),
        )
        link_duplicates(session, second)
        session.flush()

        assert second.is_original is False
        assert second.duplicate_of_id == first.id
        assert first.is_original is True

    def test_rozne_nieruchomosci_nie_sa_kopiami(self, session):
        self._add(session, external_id="C1", source_key="portal_a", url="https://a.pl/2")
        other = self._add(
            session, external_id="D1", source_key="portal_b", url="https://b.pl/2",
            title="Dom wolnostojący, Nysa",
            description="Dom 180 m2, 6 pokoi, działka 900 m2. Telefon 601 999 888.",
            price=890000.0,
        )
        link_duplicates(session, other)
        session.flush()
        assert other.is_original is True

    def test_odcisk_wymaga_kilku_cech(self):
        data = {"city": None, "street": None, "area": None, "rooms": None,
                "floor": None, "property_type": PropertyType.INNE,
                "transaction": TransactionType.SPRZEDAZ, "title": "x", "description": ""}
        compute_fingerprints(data, [])
        assert data["fingerprint"] is None

from __future__ import annotations

from datetime import timedelta

from metruj.models import OfferKind, PropertyType, SellerType, TransactionType, utcnow
from metruj.pipeline.dedup import compute_fingerprints, link_duplicates
from metruj.pipeline.enrich import detect_seller_type, looks_like_agency, match_agency
from metruj.pipeline.normalize import normalize
from metruj.scrapers.base import RawListing


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

    def test_przyjmuje_oferte_z_dowolnego_wojewodztwa(self):
        """Serwis obejmuje całą Polskę — Gdańsk nie jest „spoza regionu"."""
        raw = make_raw(title="Mieszkanie w Gdańsku", description="Wrzeszcz, 50 m2")
        result = normalize(raw)
        assert result is not None
        assert result.data["city"] == "Gdańsk"
        assert result.data["voivodeship"] == "pomorskie"

    def test_zawezenie_do_wojewodztw_odsiewa_reszte(self):
        """Instancja zawężona do wybranych województw pomija pozostałe."""
        raw = make_raw(title="Mieszkanie w Gdańsku", description="Wrzeszcz, 50 m2")
        assert normalize(raw, scope=["opolskie"]) is None
        assert normalize(raw, scope=["pomorskie"]) is not None

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
        from metruj.models import Listing

        raw = make_raw(**kw)
        result = normalize(raw, require_region=False)
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

    def test_rozne_kawalerki_w_tym_samym_miescie_to_nie_kopie(self, session):
        """Odcisk parametrów w dużym mieście pasuje do setek mieszkań.

        Z produkcji: trzy różne kawalerki 30 m² we Wrocławiu zostały scalone
        w jedną, bo miały ten sam metraż, tę samą liczbę pokoi i ceny
        mieszczące się w dopuszczalnym rozrzucie. Sam odcisk nie może
        wystarczać — musi go potwierdzić ulica, telefon albo treść.
        """
        self._add(
            session, external_id="W1", source_key="portal_a", url="https://a.pl/w1",
            title="Mieszkanie 1-pokojowe w centrum Wrocławia",
            description="Kawalerka 30 m2 w centrum, do wynajęcia od zaraz.",
            city="Wrocław", area=30.0, rooms=1, price=2100.0, street=None,
        )
        other = self._add(
            session, external_id="W2", source_key="portal_b", url="https://b.pl/w2",
            title="30m2 z sypialnią - Legnicka, nowy budynek",
            description="Ustawne mieszkanie przy Legnickiej, osobna sypialnia.",
            city="Wrocław", area=30.0, rooms=1, price=2050.0, street=None,
        )
        link_duplicates(session, other)
        session.flush()
        assert other.is_original is True
        assert other.duplicate_of_id is None

    def test_odcisk_potwierdzony_trescia_laczy(self, session):
        """Ten sam opis przepisany na drugi portal to już przesłanka."""
        opis = (
            "Przestronne mieszkanie dwupokojowe o powierzchni 54 m2 na trzecim "
            "piętrze w budynku z windą. Kuchnia w zabudowie, duży balkon od "
            "strony parku, miejsce postojowe w hali garażowej. Do mieszkania "
            "przynależy piwnica. Okolica cicha, w pobliżu szkoła i przedszkole."
        )
        first = self._add(
            session, external_id="T1", source_key="portal_a", url="https://a.pl/t1",
            title="Dwupokojowe z balkonem, Gdańsk", description=opis,
            city="Gdańsk", area=54.0, rooms=2, price=690000.0, street=None,
            published_at=utcnow() - timedelta(days=4),
        )
        second = self._add(
            session, external_id="T2", source_key="portal_b", url="https://b.pl/t2",
            title="Gdańsk | 54 m² | 2 pokoje | winda", description=opis,
            city="Gdańsk", area=54.0, rooms=2, price=690000.0, street=None,
            published_at=utcnow() - timedelta(days=1),
        )
        link_duplicates(session, second)
        session.flush()
        assert second.is_original is False
        assert second.duplicate_of_id == first.id

    def test_ten_sam_portal_dwie_ceny_to_dwa_mieszkania(self, session):
        """Deweloper wystawia kilkadziesiąt lokali pod jednym opisem.

        Z produkcji: „Nowe mieszkanie trzypokojowe: Wrocław Iwiny" za 669 312
        i za 678 163 zł to dwa różne lokale w tej samej inwestycji, a nie
        ta sama oferta w dwóch cenach. Portal nie wystawia jednego mieszkania
        dwa razy z różną ceną.
        """
        opis = (
            "Nowa inwestycja we Wrocławiu. Mieszkania o wysokim standardzie "
            "wykończenia, z balkonem lub ogródkiem, w budynku z windą i halą "
            "garażową. Termin oddania: czwarty kwartał. Zapraszamy do kontaktu."
        )
        self._add(
            session, external_id="D1", source_key="deweloper_portal",
            url="https://d.pl/1", title="Nowe mieszkanie trzypokojowe: Wrocław Iwiny",
            description=opis, city="Wrocław", area=72.0, rooms=3, price=669312.0, street=None,
        )
        other = self._add(
            session, external_id="D2", source_key="deweloper_portal",
            url="https://d.pl/2", title="Nowe mieszkanie trzypokojowe: Wrocław Iwiny",
            description=opis, city="Wrocław", area=72.0, rooms=3, price=678163.0, street=None,
        )
        link_duplicates(session, other)
        session.flush()
        assert other.is_original is True

    def test_numer_lokalu_rozroznia_mieszkania(self, session):
        opis = (
            "Mieszkanie w nowej inwestycji, wysoki standard, winda, balkon, "
            "hala garażowa w cenie. Doskonała lokalizacja blisko centrum. "
            "Zapraszamy na prezentację w biurze sprzedaży inwestycji."
        )
        self._add(
            session, external_id="N1", source_key="portal_a", url="https://a.pl/n1",
            title="Mieszkanie nr 23 | 45,96 m² | 2 piętro", description=opis,
            city="Świecie", area=45.0, rooms=2, price=404337.0, street=None,
        )
        other = self._add(
            session, external_id="N2", source_key="portal_b", url="https://b.pl/n2",
            title="Mieszkanie nr 29 | 45,81 m² | Os. Jagiełły", description=opis,
            city="Świecie", area=45.0, rooms=2, price=411293.0, street=None,
        )
        link_duplicates(session, other)
        session.flush()
        assert other.is_original is True

    def test_ten_sam_numer_bez_metrazu_nie_laczy(self, session):
        """Jeden pośrednik ma na kontakcie kilkadziesiąt różnych mieszkań."""
        self._add(
            session, external_id="P1", source_key="portal_a", url="https://a.pl/p1",
            title="Mieszkanie w centrum", description="Kontakt 537 123 123.",
            city="Opole", area=None, price=390000.0, street=None,
        )
        other = self._add(
            session, external_id="P2", source_key="portal_b", url="https://b.pl/p2",
            title="Dom pod miastem", description="Kontakt 537 123 123.",
            city="Opole", area=None, price=420000.0, street=None,
        )
        link_duplicates(session, other)
        session.flush()
        assert other.is_original is True

    def test_rozne_licytacje_o_tym_samym_tytule(self, session):
        """Obwieszczenia bywają zatytułowane identycznie — to nie czyni ich kopiami."""
        from metruj.models import OfferKind

        common = {
            "kind": OfferKind.LICYTACJA,
            "title": "Nieruchomość gruntowa zabudowana",
            "description": None,
            "price": None,
        }
        first = self._add(session, external_id="L1", source_key="licytacje_komornik",
                          url="https://k.pl/1", opening_price=69750.0, **common)
        second = self._add(session, external_id="L2", source_key="licytacje_komornik",
                           url="https://k.pl/2", opening_price=334000.0, **common)
        link_duplicates(session, second)
        session.flush()
        assert second.is_original is True
        assert first.copies_count == 0

    def test_ta_sama_licytacja_po_sygnaturze(self, session):
        from metruj.models import OfferKind

        common = {
            "kind": OfferKind.LICYTACJA,
            "title": "Licytacja lokalu, sygn. akt Km 999/25",
            "description": None,
            "price": None,
            "opening_price": 150000.0,
        }
        first = self._add(session, external_id="S1", source_key="licytacje_komornik",
                          url="https://k.pl/s1", **common)
        second = self._add(session, external_id="S2", source_key="elicytacje",
                           url="https://k.pl/s2", **common)
        link_duplicates(session, second)
        session.flush()
        assert second.duplicate_of_id == first.id

    def test_laczy_mimo_roznych_danych_uzupelniajacych(self, session):
        """Jeden portal zna ulicę, drugi piętro — to wciąż ta sama nieruchomość.

        Przypadek z prawdziwych danych: ten sam apartament w Górkach na OLX
        (piętro znane, ulica nie) i na Otodom (ulica znana, piętro nie).
        """
        first = self._add(
            session, external_id="G1", source_key="olx", url="https://olx.pl/g1",
            title="Nowoczesny apartament z ogrodem", description=None, city="Górki",
            price=680000.0, area=50.4, rooms=3, floor=0,
        )
        second = self._add(
            session, external_id="G2", source_key="otodom", url="https://otodom.pl/g2",
            title="NOWOCZESNY APARTAMENT Z OGRODEM", description=None, city="Górki",
            price=680000.0, area=50.4, rooms=3, street="Zbożowa",
        )
        link_duplicates(session, second)
        session.flush()
        assert second.duplicate_of_id == first.id

    def test_inna_ulica_wyklucza_polaczenie(self, session):
        first = self._add(
            session, external_id="U1", source_key="olx", url="https://olx.pl/u1",
            title="Mieszkanie 2 pokoje", description=None, city="Opole",
            price=400000.0, area=45.0, rooms=2, street="Wrocławska",
        )
        second = self._add(
            session, external_id="U2", source_key="otodom", url="https://otodom.pl/u2",
            title="Mieszkanie 2 pokoje", description=None, city="Opole",
            price=400000.0, area=45.0, rooms=2, street="Ozimska",
        )
        link_duplicates(session, second)
        session.flush()
        assert second.is_original is True
        assert first.copies_count == 0

    def test_duza_roznica_ceny_wyklucza_polaczenie(self, session):
        self._add(
            session, external_id="P1", source_key="olx", url="https://olx.pl/p1",
            title="Mieszkanie 3 pokoje", description=None, city="Opole",
            price=300000.0, area=60.0, rooms=3,
        )
        second = self._add(
            session, external_id="P2", source_key="otodom", url="https://otodom.pl/p2",
            title="Mieszkanie 3 pokoje", description=None, city="Opole",
            price=700000.0, area=60.0, rooms=3,
        )
        link_duplicates(session, second)
        session.flush()
        assert second.is_original is True

    def test_odcisk_wymaga_metrazu(self):
        data = {"city": "Opole", "area": None, "rooms": 2,
                "property_type": PropertyType.MIESZKANIE,
                "transaction": TransactionType.SPRZEDAZ, "title": "x", "description": ""}
        compute_fingerprints(data, [])
        assert data["fingerprint"] is None

    def test_krotki_tytul_nie_daje_odcisku_tekstowego(self):
        from metruj.utils.text import shingle_hash

        assert shingle_hash("lokal mieszkalny") is None
        assert shingle_hash(" ".join(f"slowo{i}" for i in range(20))) is not None

    def test_odcisk_powstaje_z_miasta_i_metrazu(self):
        data = {"city": "Opole", "area": 49.0, "rooms": 2,
                "property_type": PropertyType.MIESZKANIE,
                "transaction": TransactionType.SPRZEDAZ, "title": "x", "description": ""}
        compute_fingerprints(data, [])
        assert data["fingerprint"] is not None


class TestBudowanieScrapera:
    """Każde źródło musi zapisywać się pod WŁASNYM kluczem.

    Scrapery uniwersalne (sitemap, generic_html) obsługują wiele witryn naraz.
    Bez przekazania klucza wszystkie strony biur lądowały pod wspólnym
    „sitemap" i zlewały się w jedno źródło.
    """

    def _source(self, scraper: str, key: str):
        from metruj.models import OfferKind, Source

        return Source(key=key, scraper=scraper, kind=OfferKind.NIERUCHOMOSC, name=key, config={})

    def test_scrapery_uniwersalne_dostaja_klucz_zrodla(self):
        from metruj.pipeline.runner import _build_scraper

        for scraper, key in [("sitemap", "investdom_pl"), ("generic_html", "bip_nysa")]:
            built = _build_scraper(self._source(scraper, key), client=None)
            assert built.source_key == key

    def test_scrapery_dedykowane_maja_wlasny_klucz(self):
        from metruj.pipeline.runner import _build_scraper

        for scraper in ("olx", "otodom", "licytacje_komornik"):
            built = _build_scraper(self._source(scraper, scraper), client=None)
            assert built.key == scraper

    def test_nieznany_scraper_schodzi_na_generyczny(self):
        from metruj.pipeline.runner import _build_scraper
        from metruj.scrapers.generic_html import GenericHtmlScraper

        built = _build_scraper(self._source("nie-ma-takiego", "cokolwiek"), client=None)
        assert isinstance(built, GenericHtmlScraper)
        assert built.source_key == "cokolwiek"


class TestBezpieczneUsuwanie:
    """Oferta, na którą wskazują kopie, też musi dać się usunąć.

    Zwykłe DELETE kończyło się naruszeniem klucza obcego — na produkcji
    wywróciło to czyszczenie źródła zapisanego pod błędnym kluczem.
    """

    def _pair(self, session, tag: str):
        """Para oryginał + kopia. `tag` rozdziela przypadki, bo baza jest
        wspólna dla całej sesji testowej i identyfikatory nie mogą się powtarzać."""
        from metruj.pipeline.dedup import link_duplicates

        common = {"description": None, "price": 500000.0, "area": 55.0, "rooms": 2,
                  "city": "Opole"}
        first = TestDeduplikacja()._add(
            session, external_id=f"{tag}-1", source_key=f"src-{tag}-a",
            url=f"https://a/{tag}", title="Mieszkanie do usunięcia", **common)
        second = TestDeduplikacja()._add(
            session, external_id=f"{tag}-2", source_key=f"src-{tag}-b",
            url=f"https://b/{tag}", title="Mieszkanie do usunięcia", **common)
        link_duplicates(session, second)
        session.flush()
        assert second.duplicate_of_id == first.id
        return first, second

    def test_usuwa_oryginal_majacy_kopie(self, session):
        from metruj.models import Listing
        from metruj.pipeline.prune import delete_listings

        first, second = self._pair(session, "usun")
        removed = delete_listings(session, [first.id])
        session.flush()
        assert removed == 1
        assert session.get(Listing, first.id) is None
        # kopia zostaje i przestaje być kopią
        survivor = session.get(Listing, second.id)
        assert survivor is not None
        assert survivor.duplicate_of_id is None
        assert survivor.is_original is True

    def test_usuwa_cale_zrodlo(self, session):
        from metruj.models import Listing
        from metruj.pipeline.prune import delete_by_source

        self._pair(session, "zrodlo")
        removed = delete_by_source(session, "src-zrodlo-a")
        session.flush()
        assert removed >= 1
        assert session.query(Listing).filter(Listing.source_key == "src-zrodlo-a").count() == 0


class TestPrzeliczanieWspolrzednych:
    """Cache geokodowania trzyma też nieudane dopasowania.

    Gdy poprawka w geokoderze pozwala trafić lepiej, trzeba unieważnić wpisy
    po TREŚCI ZAPYTANIA — bo przy trafieniu na poziomie miejscowości wynik
    nie zawiera ulicy i filtrowanie po nim niczego nie znajduje.
    """

    def test_wpis_z_ulica_w_zapytaniu_jest_wykrywany(self, session):
        from sqlalchemy import select

        from metruj.models import GeocodeCache

        session.add_all([
            GeocodeCache(query_hash="h-ulica", query="Opole, Telesfora",
                         lat=50.6, lon=17.9, precision="city", street=None),
            GeocodeCache(query_hash="h-miasto", query="Zawadzkie",
                         lat=50.6, lon=18.4, precision="city", street=None),
        ])
        session.flush()

        do_przeliczenia = {
            row[0] for row in session.execute(
                select(GeocodeCache.query_hash).where(
                    GeocodeCache.precision.in_(["city", "district"]),
                    GeocodeCache.query.contains(","),
                )
            )
        }
        assert "h-ulica" in do_przeliczenia      # zapytanie zawierało ulicę
        assert "h-miasto" not in do_przeliczenia  # sama miejscowość — nie ma co poprawiać


class TestGeokodowanieDzielnic:
    """Dzielnica jest dokładniejsza niż środek miasta, ale bywa pułapką.

    Nazwy dzielnic powtarzają się w całej Polsce: „Śródmieście" istnieje
    w każdym większym mieście, a „Gosławice" to też wieś na Dolnym Śląsku.
    Dlatego wynik z OpenStreetMap przyjmujemy tylko wtedy, gdy leży blisko
    środka swojej miejscowości.
    """

    def test_prawdziwa_dzielnica_przechodzi(self):
        from metruj.pipeline.geocode import MAX_DISTRICT_KM, _distance_km

        # Zaodrze wobec centrum Opola
        assert _distance_km(50.6751, 17.9213, 50.66430, 17.89755) <= MAX_DISTRICT_KM

    def test_ta_sama_nazwa_w_innym_wojewodztwie_odpada(self):
        from metruj.pipeline.geocode import MAX_DISTRICT_KM, _distance_km

        # Gosławice na Dolnym Śląsku i Śródmieście w Lublinie
        assert _distance_km(50.6751, 17.9213, 51.2280, 16.8307) > MAX_DISTRICT_KM
        assert _distance_km(50.6751, 17.9213, 51.2483, 22.5558) > MAX_DISTRICT_KM

    def test_odleglosc_liczona_poprawnie(self):
        from metruj.pipeline.geocode import _distance_km

        # Opole - Wrocław to ok. 80 km w linii prostej
        assert 75 < _distance_km(50.6751, 17.9213, 51.1079, 17.0385) < 90


class TestWygaszanieOfert:
    """Oferty wolno wygaszać tylko po pełnym przejściu wyników.

    Zwykły skan bierze najnowsze strony i z definicji nie widzi starszych
    ofert. Uznawanie ich wtedy za zdjęte wykasowało z widoku 4785 z 6198
    ofert w kilkanaście minut od ich zebrania.
    """

    def test_zwykly_skan_nie_wygasza(self, session):
        from datetime import timedelta

        from metruj.models import ListingStatus, Source, utcnow
        from metruj.pipeline.runner import _mark_missing

        source = Source(key="wygasz-test", name="test", interval_minutes=15)
        session.add(source)
        listing = TestDeduplikacja()._add(
            session, external_id="W1", source_key="wygasz-test", url="https://w/1",
            title="Mieszkanie testowe", description=None, price=300000.0, area=50.0,
        )
        listing.last_seen_at = utcnow() - timedelta(hours=2)
        session.flush()

        # dwie godziny bez kontaktu to dla zwykłego skanu norma
        assert _mark_missing(session, source) == 0
        assert listing.status == ListingStatus.AKTYWNA

    def test_wygasza_dopiero_po_kilku_dniach(self, session):
        from datetime import timedelta

        from metruj.models import ListingStatus, Source, utcnow
        from metruj.pipeline.runner import DAYS_MISSING_BEFORE_REMOVAL, _mark_missing

        source = Source(key="wygasz-test2", name="test", interval_minutes=15)
        session.add(source)
        listing = TestDeduplikacja()._add(
            session, external_id="W2", source_key="wygasz-test2", url="https://w/2",
            title="Mieszkanie testowe", description=None, price=300000.0, area=51.0,
        )
        listing.last_seen_at = utcnow() - timedelta(days=DAYS_MISSING_BEFORE_REMOVAL + 1)
        session.flush()

        assert _mark_missing(session, source) == 1
        assert listing.status == ListingStatus.NIEAKTYWNA
        assert listing.removed_at is not None


def test_zmiana_adresu_uniewaznia_punkt_na_mapie(session):
    """Poprawiona miejscowość nie może zostać ze współrzędnymi sprzed poprawki.

    Z produkcji: jedna pinezka pod Kłodzkiem zbierała oferty z Opola, Brzegu
    i Głuchołaz — wszystkie rozpoznano kiedyś jako Kamienicę, a po poprawieniu
    miejscowości stary punkt został.
    """
    from metruj.models import Source
    from metruj.pipeline.runner import upsert_listing

    source = Source(key="test-adres", name="Test")
    session.add(source)
    session.flush()

    first = normalize(make_raw(title="Mieszkanie w Kamienicy", city="Kamienica"))
    listing, action, _ = upsert_listing(session, first.data, first.phones, source)
    session.flush()
    listing.lat, listing.lon = 50.4494, 16.9510
    listing.geo_precision, listing.geo_source = "city", "gugik"
    session.flush()

    corrected = normalize(make_raw(title="Mieszkanie w centrum Opola", city="Opole"))
    listing, action, _ = upsert_listing(session, corrected.data, corrected.phones, source)
    session.flush()

    assert listing.city == "Opole"
    assert listing.lat is None, "stary punkt został przy nowym adresie"
    assert listing.geo_source is None


def test_najnowsze_to_najswiezej_wystawione_a_nie_zebrane(session):
    """Po pełnym przejściu przez portal na górę listy wskakiwały oferty sprzed
    miesięcy, bo sortowanie szło po dacie naszego pierwszego spotkania."""
    from datetime import timedelta

    from metruj.models import Source
    from metruj.pipeline.runner import upsert_listing
    from metruj.query import Filters, search_listings

    source = Source(key="test-najnowsze", name="Test")
    session.add(source)
    session.flush()

    stara = normalize(make_raw(external_id="S", url="https://x.pl/s", source_key="test-najnowsze",
                               title="Mieszkanie w Opolu, wystawione pół roku temu",
                               published_at=utcnow() - timedelta(days=180)))
    swieza = normalize(make_raw(external_id="N", url="https://x.pl/n", source_key="test-najnowsze",
                                title="Dom w Nysie, wystawiony dziś",
                                published_at=utcnow() - timedelta(hours=2)))
    # świeżą zbieramy PIERWSZĄ, starą — później (tak jak w pełnym przejściu)
    upsert_listing(session, swieza.data, swieza.phones, source)
    upsert_listing(session, stara.data, stara.phones, source)
    session.flush()

    wyniki, _ = search_listings(
        session, Filters(sort="najnowsze", only_original=False, source=["test-najnowsze"])
    )
    assert wyniki[0].external_id == "N"
    assert wyniki[0].is_fresh is True
    assert wyniki[-1].is_fresh is False
    assert wyniki[-1].days_on_market >= 179


def test_data_wystawienia_z_przyszlosci_jest_odrzucana():
    from datetime import timedelta

    wynik = normalize(make_raw(published_at=utcnow() + timedelta(days=80)))
    assert wynik.data["published_at"] is None


def test_stopka_z_lista_wojewodztw_nie_przestawia_lokalizacji():
    """Portale ogólnopolskie wypisują w stopce wszystkie województwa naraz.

    Działka z Leszna ma zostać w Wielkopolsce, a nie przejąć województwa
    z pierwszej nazwy wymienionej w stopce.
    """
    stopka = "Województwa: dolnośląskie kujawsko-pomorskie lubelskie opolskie podkarpackie"
    leszno = normalize(make_raw(title="Leszno ul. Towarowa działki", description=stopka,
                                kind=OfferKind.PRZETARG, price=None))
    assert leszno is not None
    assert leszno.data["city"] == "Leszno"
    assert leszno.data["voivodeship"] == "wielkopolskie"

    nysa = normalize(make_raw(title="Nysa, ul. Kolejowa 8 — działka", description=stopka,
                              kind=OfferKind.PRZETARG, price=None))
    assert nysa is not None
    assert nysa.data["voivodeship"] == "opolskie"


def test_zwrot_woj_opolskie_w_opisie_wystarczy():
    """Wsi spoza słownika miejscowości nie wolno gubić."""
    raw = make_raw(
        title="Sprzedaż działki nr 118/2",
        description="Nieruchomość położona w gminie Pakosławice, woj. opolskie.",
        kind=OfferKind.PRZETARG, price=None)
    assert normalize(raw) is not None


def test_adres_urzedu_z_tresci_nie_staje_sie_adresem_nieruchomosci():
    """Strona BIP-u zaczyna się od adresu urzędu, nie wystawianej działki.

    65 przetargów z Kluczborka lądowało przez to pod ratuszem przy Katowickiej.
    """
    raw = make_raw(
        title="Wykaz nieruchomości przeznaczonych do sprzedaży",
        description="Urząd Miejski w Kluczborku, ul. Katowicka 1. Burmistrz ogłasza wykaz…",
        city="Kluczbork", kind=OfferKind.PRZETARG, price=None, street_from_body=False)
    assert normalize(raw).data["street"] is None

    # Przy zwykłym ogłoszeniu z portalu ulica z treści nadal jest wskazówką.
    portal = make_raw(
        title="Mieszkanie na sprzedaż",
        description="Mieszkanie przy ul. Katowickiej 1 w Kluczborku.",
        city="Kluczbork", price=None)
    assert normalize(portal).data["street"] == "Katowickiej"


def test_zrodlo_moze_swiadomie_wyczyscic_ulice():
    """Poprawka „ta ulica to adres urzędu" musi dojść do już zapisanej oferty.

    Zapis z zasady nie nadpisuje pola pustą wartością — portal potrafi raz nie
    oddać pola i to nie znaczy, że dane zniknęły. Ale gdy źródło *ustala*,
    że ulicy nie zna, stara wartość musi ustąpić.
    """
    z_ulica = normalize(make_raw(
        title="Wykaz nieruchomości", description="Urząd Miejski, ul. Katowicka 1",
        city="Kluczbork", kind=OfferKind.PRZETARG, price=None))
    assert z_ulica.data["street"] == "Katowicka"
    assert z_ulica.cleared == ()

    bez_ulicy = normalize(make_raw(
        title="Wykaz nieruchomości", description="Urząd Miejski, ul. Katowicka 1",
        city="Kluczbork", kind=OfferKind.PRZETARG, price=None, street_from_body=False))
    assert bez_ulicy.data["street"] is None
    assert bez_ulicy.cleared == ("street",)


def test_licytacja_po_terminie_nie_wraca_do_aktywnych(session):
    """Komornicy i BIP-y nie zdejmują ogłoszeń od razu po licytacji — zbieracz
    przywracał je do aktywnych przy każdym przejściu."""
    from metruj.models import ListingStatus, Source
    from metruj.pipeline.runner import upsert_listing

    source = Source(key="test-po-terminie", name="Test")
    session.add(source)
    session.flush()

    def licytacja(external_id: str, days: int):
        return normalize(make_raw(
            external_id=external_id, url=f"https://x.pl/{external_id}",
            source_key="test-po-terminie", kind=OfferKind.LICYTACJA,
            title="Licytacja mieszkania w Opolu", event_date=utcnow() + timedelta(days=days),
        ))

    minela = licytacja("po-terminie", -5)
    przed_nami = licytacja("przed-nami", 10)
    stara, _, _ = upsert_listing(session, minela.data, minela.phones, source)
    nowa, _, _ = upsert_listing(session, przed_nami.data, przed_nami.phones, source)
    # i ponowne zobaczenie w źródle nie przywraca zakończonej
    stara, _, _ = upsert_listing(session, minela.data, minela.phones, source)
    session.flush()

    assert stara.status == ListingStatus.NIEAKTYWNA
    assert nowa.status == ListingStatus.AKTYWNA


def test_miejscowosc_odtworzona_z_czlonow_adresu(session):
    """Z produkcji: „Biestrzykowice, Świerczów, namysłowski" zapisane jako
    ulica, dzielnica i puste miasto — karta pokazywała sam powiat."""
    from metruj.models import Source
    from metruj.pipeline.repair import _restore_cities
    from metruj.pipeline.runner import upsert_listing

    source = Source(key="test-odtworzenie", name="Test")
    session.add(source)
    session.flush()
    raw = normalize(make_raw(external_id="odtw-1", url="https://gratka.pl/odtw-1",
                             source_key="gratka", title="Budynek gospodarczy z poddaszem"))
    listing, _, _ = upsert_listing(session, raw.data, raw.phones, source)
    listing.city, listing.district, listing.street = None, "Świerczów", "Biestrzykowice"
    listing.county, listing.voivodeship = "namysłowski", "opolskie"
    session.flush()

    assert _restore_cities(session) >= 1
    session.refresh(listing)
    assert listing.city == "Świerczów"
    assert listing.district == "Biestrzykowice"
    assert listing.county == "namysłowski"

from __future__ import annotations

from ogloszenia.utils.geo import (
    detect_location,
    detect_opole_district,
    extract_street,
    is_in_opolskie,
    resolve_place,
)
from ogloszenia.utils.phones import extract_phones, mask, parse_phone
from ogloszenia.utils.text import (
    extract_area,
    extract_case_number,
    extract_floor,
    extract_rooms,
    parse_datetime,
    parse_number,
    shingle_hash,
    slugify,
)


class TestLiczby:
    def test_cena_ze_spacjami(self):
        assert parse_number("629 000 zł") == 629000.0

    def test_cena_z_kropka_tysieczna(self):
        assert parse_number("629.000") == 629000.0

    def test_metraz_z_przecinkiem(self):
        assert parse_number("75,28 m²") == 75.28

    def test_cena_za_metr(self):
        assert parse_number("12 837 zł/m²") == 12837.0

    def test_twarda_spacja(self):
        assert parse_number("749 000 zł") == 749000.0

    def test_brak_liczby(self):
        assert parse_number("cena do negocjacji") is None


class TestParametry:
    def test_powierzchnia(self):
        assert extract_area("Mieszkanie 49 m2 w centrum") == 49.0

    def test_pokoje(self):
        assert extract_rooms("Przestronne 4-pokojowe mieszkanie") == 4

    def test_pietro_z_calkowitym(self):
        assert extract_floor("2 piętro z 3") == (2, 3)

    def test_parter(self):
        assert extract_floor("lokal na parterze")[0] == 0

    def test_powierzchnia_z_separatorem_tysiecy(self):
        assert extract_area("budynek biurowy o powierzchni 1 240 m2") == 1240.0

    def test_separator_nie_zlepia_sasiednich_liczb(self):
        """„3 pokoje, 49 m2" to 49 m², a nie 349 m²."""
        assert extract_area("Mieszkanie 3 pokoje, 49 m2") == 49.0

    def test_sygnatura_komornicza(self):
        assert extract_case_number("sygn. akt Km 1234/23") == "Km 1234/23"


class TestDaty:
    def test_dzisiaj_z_godzina(self):
        assert parse_datetime("dzisiaj 14:30").hour == 14

    def test_polska_data_slowna(self):
        dt = parse_datetime("12 marca 2025")
        assert (dt.year, dt.month, dt.day) == (2025, 3, 12)

    def test_iso(self):
        assert parse_datetime("2025-06-01T10:00:00Z").year == 2025


class TestTelefony:
    def test_numer_ze_spacjami(self):
        phones = extract_phones("Kontakt: 537 123 123")
        assert phones and phones[0].e164 == "+48537123123"

    def test_numer_z_myslnikami(self):
        assert extract_phones("tel. 604-123-456")[0].e164 == "+48604123456"

    def test_numer_stacjonarny_opolski(self):
        assert extract_phones("77 123 45 67")[0].e164 == "+48771234567"

    def test_maskowanie(self):
        assert mask("537123123") == "537 *** ***"

    def test_maskowanie_w_obiekcie(self):
        assert parse_phone("+48 537 123 123").masked == "537 *** ***"

    def test_haszowanie_stabilne(self):
        a = parse_phone("537123123")
        b = parse_phone("+48537123123")
        assert a.hashed == b.hashed

    def test_odrzuca_smiec(self):
        assert extract_phones("numer działki 111/22, powierzchnia 1200") == []


class TestGeo:
    def test_rozpoznanie_opola(self):
        hit = detect_location("Mieszkanie, ul. Wrocławska, Półwieś, Opole")
        assert hit["city"] == "Opole"
        assert hit["district"] == "Półwieś"

    def test_powiat_z_miejscowosci(self):
        assert resolve_place("Kędzierzyn-Koźle")["county"] == "kędzierzyńsko-kozielski"

    def test_mala_miejscowosc(self):
        assert resolve_place("Zawadzkie")["county"] == "strzelecki"

    def test_poza_regionem(self):
        assert detect_location("Mieszkanie w Gdańsku, Wrzeszcz") == {}

    def test_ulica(self):
        assert extract_street("ul. Leona Powolnego, Opole") == "Leona Powolnego"

    def test_ulica_konczy_sie_na_zdaniu(self):
        assert extract_street("ul. Leona Powolnego. Kontakt 537 214 908.") == "Leona Powolnego"

    def test_ulica_konczy_sie_na_przecinku(self):
        assert extract_street("ul. Wrocławska, Półwieś, Opole") == "Wrocławska"

    def test_ulica_wieloczlonowa(self):
        assert extract_street("al. Wincentego Witosa 12, Opole") == "Wincentego Witosa"

    def test_brak_ulicy(self):
        assert extract_street("Mieszkanie w centrum Opola") is None

    def test_dzielnica(self):
        assert detect_opole_district("Sprzedam na Zaodrzu") == "Zaodrze"

    def test_wojewodztwo_po_nazwie(self):
        assert is_in_opolskie("Działka, gmina Turawa")

    def test_odmiana_miejscownik(self):
        assert detect_location("Mieszkanie w centrum Opola, po remoncie")["city"] == "Opole"

    def test_odmiana_dzielnicy(self):
        assert detect_opole_district("Sprzedam mieszkanie na Zaodrzu w Opolu") == "Zaodrze"

    def test_pokoje_to_nie_gmina_pokoj(self):
        """Wieś Pokój nie może wygrywać ze słowem „pokoje" z opisu."""
        hit = detect_location("Mieszkanie 3 pokoje, 62 m2, Kluczbork, do remontu")
        assert hit["city"] == "Kluczbork"

    def test_wieloznaczna_nazwa_z_wielkiej_litery_liczy_sie(self):
        hit = detect_location("Dom na sprzedaż, Pokój, gmina Pokój")
        assert hit["city"] == "Pokój"

    def test_dzielnica_jako_slowo_nie_jest_wsia(self):
        hit = detect_location("Spokojna dzielnica, Nysa, 48 m2")
        assert hit["city"] == "Nysa"

    def test_nazwa_dwuczlonowa(self):
        assert detect_location("Lokal w Kędzierzynie-Koźlu")["city"] == "Kędzierzyn-Koźle"

    def test_wybiera_najlepszego_kandydata(self):
        hit = detect_location("Mieszkanie w Strzelcach Opolskich, blisko rynku")
        assert hit["city"] == "Strzelce Opolskie"


class TestPozostale:
    def test_slug(self):
        assert slugify("Biuro Nieruchomości Śródmieście") == "biuro-nieruchomosci-srodmiescie"

    def test_shingle_odporny_na_przestawienie(self):
        a = shingle_hash("Ładne mieszkanie w centrum Opola z balkonem i piwnicą do remontu")
        b = shingle_hash("ŁADNE MIESZKANIE W CENTRUM OPOLA Z BALKONEM I PIWNICĄ DO REMONTU")
        assert a == b

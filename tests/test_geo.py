"""Geografia: rejestr TERYT, rozpoznawanie miejscowości, adresy.

Każdy przypadek w `TestBledyZProdukcji` to ogłoszenie, które naprawdę trafiło
do bazy pod złym adresem. Trzymamy je tutaj, żeby nie wróciły.
"""

from __future__ import annotations

import pytest

from metruj.geo import (
    detect_location,
    detect_voivodeship,
    extract_street,
    find_places,
    in_poland,
    in_voivodeship,
    known_voivodeship,
    lookup,
    normalize_street,
    parse_jednostka,
    resolve_place,
    split_house_number,
)
from metruj.geo.teryt import by_teryt, counties, towns, units, voivodeships


class TestRejestrTeryt:
    def test_szesnascie_wojewodztw(self):
        assert len(voivodeships()) == 16
        assert "opolskie" in voivodeships()
        assert "warmińsko-mazurskie" in voivodeships()

    def test_liczba_powiatow(self):
        # 314 powiatów ziemskich + 66 miast na prawach powiatu
        powiaty = [u for u in units() if u.kind == "powiat"]
        assert len(powiaty) == 380

    def test_miasta_sa_kompletne(self):
        wszystkie = towns()
        assert len(wszystkie) > 900
        for miasto in ("Warszawa", "Kraków", "Opole", "Nysa", "Gdańsk", "Zakopane"):
            assert miasto in wszystkie

    def test_teryt_wskazuje_jednostke(self):
        krakow = by_teryt("126101")
        assert krakow is not None
        assert krakow.voivodeship == "małopolskie"
        assert krakow.city_county is True

    def test_nazwy_bez_przedrostkow_gus(self):
        """BDL nazywa powiaty „Powiat m. Kraków" — w adresie tak nikt nie pisze."""
        assert not any(u.name.startswith(("Powiat", "m.", "st.")) for u in units())

    def test_bez_jednostek_zniesionych(self):
        """Gminy zniesione w 2002 („Warszawa - Bemowo do 2001") nie są adresem."""
        assert not any("do 20" in u.name for u in units())

    def test_jednostka_z_gugik(self):
        assert parse_jednostka("{Polska,małopolskie,wadowicki,Spytkowice}") == {
            "voivodeship": "małopolskie",
            "county": "wadowicki",
            "commune": "Spytkowice",
        }

    def test_jednostka_skrocona_po_kodzie(self):
        hit = parse_jednostka("{Nysa,160705}")
        assert hit["voivodeship"] == "opolskie"
        assert hit["county"] == "nyski"

    def test_ramki_wojewodztw(self):
        assert in_voivodeship(50.67, 17.92, "opolskie")       # Opole
        assert not in_voivodeship(52.23, 21.01, "opolskie")   # Warszawa
        assert in_poland(54.35, 18.65)                        # Gdańsk
        assert not in_poland(48.85, 2.35)                     # Paryż


class TestOdmiana:
    """Polska odmiana przez przypadki — największe źródło pudeł przy dopasowaniu."""

    @pytest.mark.parametrize(
        "tekst,miasto",
        [
            ("Kawalerka po remoncie w Opolu", "Opole"),
            ("Mieszkanie 2 pokoje w Nysie", "Nysa"),
            ("Apartament w Łodzi przy Piotrkowskiej", "Łódź"),
            ("Garaż w Krakowie", "Kraków"),
            ("Dom w Strzelcach Opolskich", "Strzelce Opolskie"),
            ("Mieszkanie w Kędzierzynie-Koźlu", "Kędzierzyn-Koźle"),
            ("Hala magazynowa w Poznaniu", "Poznań"),
            ("Grunt inwestycyjny we Wrocławiu", "Wrocław"),
            ("Mieszkanie w Bielsku-Białej", "Bielsko-Biała"),
            ("Dom w Zielonej Górze", "Zielona Góra"),
            ("Lokal w Rzeszowie", "Rzeszów"),
            ("Mieszkanie w Szczecinie", "Szczecin"),
            ("Dom w Toruniu", "Toruń"),
            ("Mieszkanie w Białej Podlaskiej", "Biała Podlaska"),
        ],
    )
    def test_miejscownik(self, tekst, miasto):
        assert detect_location(tekst).city == miasto


class TestBledyZProdukcji:
    """Ogłoszenia, które trafiły do bazy pod złym adresem."""

    def test_garaz_w_krakowie_to_nie_wies_miejsce(self):
        """„Miejsce postojowe … w Krakowie" lądowało we wsi Miejsce pod Namysłowem."""
        hit = detect_location("Miejsce postojowe w garażu podziemnym w Krakowie")
        assert hit.city == "Kraków"
        assert hit.voivodeship == "małopolskie"

    def test_dzialka_spod_starogardu_to_nie_gmina_dabrowa(self):
        hit = detect_location(
            "nieruchomość gruntowa niezabudowana położona w miejscowości "
            "Dąbrówka Gmina Starogard Gdański"
        )
        assert hit.voivodeship == "pomorskie"
        assert hit.county == "starogardzki"

    def test_przetarg_pkp_z_leszna(self):
        hit = detect_location("Leszno ul. Towarowa działki")
        assert hit.city == "Leszno"
        assert hit.voivodeship == "wielkopolskie"

    def test_pokoje_to_nie_gmina_pokoj(self):
        hit = detect_location("Mieszkanie 3 pokoje, 62 m2, Kluczbork, do remontu")
        assert hit.city == "Kluczbork"

    def test_dobra_oferta_to_nie_gmina_dobra(self):
        assert detect_location("Dobra oferta, tanio, pilnie").city is None

    def test_nowe_mieszkanie_to_nie_gmina_nowe(self):
        assert detect_location("Nowe mieszkanie 3 pokoje z balkonem").city is None

    def test_wieloznaczna_nazwa_ze_wskazowka_sie_liczy(self):
        assert detect_location("Sprzedam działkę w gminie Pokój").city == "Pokój"
        assert detect_location("Dom w miejscowości Dobra, gm. Dobra").city == "Dobra"

    def test_stopka_z_lista_wojewodztw_nie_jest_adresem(self):
        """Portale ogólnopolskie wypisują w stopce wszystkie szesnaście naraz."""
        stopka = "Nieruchomości: dolnośląskie kujawsko-pomorskie lubelskie lubuskie"
        assert detect_voivodeship(stopka) is None

    def test_wojewodztwo_podane_wprost(self):
        assert detect_voivodeship("Działka, woj. opolskie") == "opolskie"
        assert detect_voivodeship("Dom w województwie mazowieckim") == "mazowieckie"

    def test_opis_bez_lokalizacji(self):
        assert detect_location("mieszkanie z balkonem i piwnicą, do wprowadzenia").city is None


class TestDopasowanieNazwy:
    def test_nazwa_wieloczlonowa_bije_jednoczlonowa(self):
        trafienia = find_places("Mieszkanie w Strzelcach Opolskich")
        assert trafienia and trafienia[0].name == "Strzelce Opolskie"

    def test_dokladne_bije_odmienione(self):
        """Wieś Świercze nie może wygrywać z gminą Świerczów."""
        hit = detect_location("Działka, gm. Świerczów, pow. namysłowski")
        assert hit.city == "Świerczów"
        assert hit.voivodeship == "opolskie"

    def test_powtarzajaca_sie_nazwa_rozstrzygana_regionem(self):
        assert resolve_place("Opole")["voivodeship"] == "opolskie"
        assert lookup("Opole")[0].voivodeship == "opolskie"

    def test_normalizacja_nazwy_wojewodztwa(self):
        assert known_voivodeship("OPOLSKIE") == "opolskie"
        assert known_voivodeship("slaskie") == "śląskie"
        assert known_voivodeship("Podlaskie") == "podlaskie"
        assert known_voivodeship("mazurskie") is None

    def test_powiaty_sa_dostepne_do_filtrowania(self):
        assert "nyski" in counties()
        assert len(counties()) > 300


class TestAdres:
    def test_ulica_z_przecinkiem(self):
        assert extract_street("ul. Leona Powolnego, Opole") == "Leona Powolnego"

    def test_ulica_konczy_sie_na_zdaniu(self):
        assert extract_street("ul. Leona Powolnego. Kontakt 537 214 908.") == "Leona Powolnego"

    def test_ulica_wieloczlonowa(self):
        assert extract_street("al. Wincentego Witosa 12, Opole") == "Wincentego Witosa"

    def test_brak_ulicy(self):
        assert extract_street("Mieszkanie w centrum Opola") is None

    def test_numer_domu_osobno(self):
        """Geokoder GUGiK chce nazwy i numeru w osobnych polach."""
        assert split_house_number("Wrocławska 12A") == ("Wrocławska", "12A")
        assert split_house_number("Piastowska 3/5") == ("Piastowska", "3/5")
        assert split_house_number("Rynek") == ("Rynek", None)

    def test_przedrostek_ulicy_znika_a_aleja_zostaje(self):
        assert normalize_street("ul. Wrocławska") == "Wrocławska"
        assert normalize_street("al. Solidarności") == "al. Solidarności"
        assert normalize_street("os. Chabry") == "os. Chabry"

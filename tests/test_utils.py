from __future__ import annotations

from metruj.utils.phones import extract_phones, mask, parse_phone
from metruj.utils.text import (
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

    def test_hektary_z_czterema_miejscami(self):
        """Areał działek podaje się w hektarach z dokładnością do metra.

        „0,0436 ha" to 436 m². Czytane jako separator tysięcy dawało 436 ha.
        """
        assert parse_number("0,0436") == 0.0436
        assert parse_number("0,5981 ha") == 0.5981

    def test_zapis_angielski_z_dwoma_przecinkami(self):
        assert parse_number("1,250,000") == 1250000.0


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


class TestHtml:
    def test_usuwa_znaczniki(self):
        """OLX zwraca opis jako HTML — do bazy ma trafić czysty tekst."""
        from metruj.utils.text import strip_html

        assert strip_html("<p>Ładne <b>mieszkanie</b></p><br>49 m2") == "Ładne mieszkanie 49 m2"

    def test_zamienia_encje(self):
        from metruj.utils.text import strip_html

        assert strip_html("Dom&nbsp;z ogrodem &amp; garażem") == "Dom z ogrodem & garażem"


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


class TestPozostale:
    def test_slug(self):
        assert slugify("Biuro Nieruchomości Śródmieście") == "biuro-nieruchomosci-srodmiescie"

    def test_shingle_odporny_na_przestawienie(self):
        a = shingle_hash("Ładne mieszkanie w centrum Opola z balkonem i piwnicą do remontu")
        b = shingle_hash("ŁADNE MIESZKANIE W CENTRUM OPOLA Z BALKONEM I PIWNICĄ DO REMONTU")
        assert a == b


class TestDatyISO:
    """Z produkcji: tysiące ogłoszeń „dodanych" w przyszłości.

    Parser z ustawieniem „dzień pierwszy" czytał zapis ISO „2026-09-12" jako
    rok-dzień-miesiąc i robił z niego 9 grudnia — przy każdym dniu od 1 do 12.
    """

    def test_iso_z_dniem_do_dwunastu(self):
        assert parse_datetime("2026-09-12").strftime("%Y-%m-%d") == "2026-09-12"

    def test_iso_z_godzina_i_strefa_w_utc(self):
        assert str(parse_datetime("2026-09-12T10:41:34+02:00")) == "2026-09-12 08:41:34"

    def test_iso_ze_spacja(self):
        assert str(parse_datetime("2026-09-05 10:41:34")) == "2026-09-05 10:41:34"

    def test_data_z_karty_morizona(self):
        """Karta Morizona i Gratki: „Dodane: 2026.09.07"."""
        assert parse_datetime("2026.09.07".replace(".", "-")).strftime("%Y-%m-%d") == "2026-09-07"

    def test_polski_zapis_dzien_miesiac_rok(self):
        assert parse_datetime("05.03.2026").strftime("%Y-%m-%d") == "2026-03-05"
        assert parse_datetime("05-03-2026").strftime("%Y-%m-%d") == "2026-03-05"

    def test_slownie(self):
        assert parse_datetime("5 marca 2026").strftime("%Y-%m-%d") == "2026-03-05"

    def test_znacznik_uniksowy(self):
        assert parse_datetime(1757672494).strftime("%Y-%m-%d") == "2025-09-12"
        assert parse_datetime(1757672494000).strftime("%Y-%m-%d") == "2025-09-12"

    def test_termin_licytacji_w_czasie_polskim(self):
        from datetime import datetime

        from metruj.utils.text import parse_local_datetime

        # KAS podaje UTC; licytacja o 11:00 latem i o 10:00 zimą
        assert parse_local_datetime("2026-09-09T09:00:00Z") == datetime(2026, 9, 9, 11, 0)
        assert parse_local_datetime("2026-11-26T09:00:00Z") == datetime(2026, 11, 26, 10, 0)
        # zapis bez strefy to już czas polski — bez przeliczania
        assert parse_local_datetime("12.10.2026 10:00") == datetime(2026, 10, 12, 10, 0)
        assert parse_local_datetime("2026-11-13") == datetime(2026, 11, 13)
        # a chwile do porównań dalej idą w UTC
        assert parse_datetime("2026-09-09T11:00:00+02:00") == datetime(2026, 9, 9, 9, 0)

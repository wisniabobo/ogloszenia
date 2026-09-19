"""Testy parsera robots.txt (RFC 9309).

Przypadki wzięte z prawdziwych plików portali — to one zdecydowały o tym,
z których adresów bot w ogóle korzysta.
"""

from __future__ import annotations

from ogloszenia.utils.robots import RobotsTxt

UA = "ogloszenia-bot/1.0"

OLX = """
User-agent: *
Disallow: */ajax/
Disallow: /api/
Disallow: /platnosci/
Allow: /api/v1/offers/
Allow: /api/v1/targeting/
"""

GRATKA = """
User-agent: *
Disallow: /mapa/*
Disallow: /*sort=*
Disallow: *page=*

User-agent: *
Allow: *page=2$
Allow: *page=3$
Disallow: /dla-agencji
"""

Z_CRAWL_DELAY = """
User-agent: *
Crawl-delay: 2.5
Disallow: /prywatne/

User-agent: ogloszenia-bot
Disallow: /tylko-dla-nas/
"""


class TestNajdluzszeDopasowanie:
    def test_allow_bije_krotszy_disallow(self):
        """To jest sedno: OLX blokuje /api/, ale wprost dopuszcza /api/v1/offers/."""
        robots = RobotsTxt.parse(OLX)
        assert robots.can_fetch(UA, "https://www.olx.pl/api/v1/offers/?region_id=12")

    def test_reszta_api_pozostaje_zablokowana(self):
        robots = RobotsTxt.parse(OLX)
        assert not robots.can_fetch(UA, "https://www.olx.pl/api/v1/geo-encoder/regions/")
        assert not robots.can_fetch(UA, "https://www.olx.pl/api/v2/cokolwiek")

    def test_sciezka_bez_regul_jest_dozwolona(self):
        robots = RobotsTxt.parse(OLX)
        assert robots.can_fetch(UA, "https://www.olx.pl/nieruchomosci/mieszkania/")


class TestWieloznaczniki:
    def test_gwiazdka_w_srodku(self):
        robots = RobotsTxt.parse(GRATKA)
        assert not robots.can_fetch(UA, "https://gratka.pl/x?sort=newest")

    def test_kotwica_konca(self):
        robots = RobotsTxt.parse(GRATKA)
        assert robots.can_fetch(UA, "https://gratka.pl/oferty?page=2")
        assert not robots.can_fetch(UA, "https://gratka.pl/oferty?page=22")

    def test_grupy_o_tej_samej_nazwie_sa_scalane(self):
        """Gratka rozbija reguły na dwa bloki `User-agent: *` — to jedna grupa."""
        robots = RobotsTxt.parse(GRATKA)
        assert not robots.can_fetch(UA, "https://gratka.pl/dla-agencji")
        assert not robots.can_fetch(UA, "https://gratka.pl/oferty?page=5")


class TestWyborGrupy:
    def test_wlasna_nazwa_bota_ma_pierwszenstwo(self):
        robots = RobotsTxt.parse(Z_CRAWL_DELAY)
        assert not robots.can_fetch(UA, "https://x.pl/tylko-dla-nas/")
        # reguła z grupy `*` nas nie dotyczy, bo mamy własną grupę
        assert robots.can_fetch(UA, "https://x.pl/prywatne/")

    def test_inny_bot_dostaje_gwiazdke(self):
        robots = RobotsTxt.parse(Z_CRAWL_DELAY)
        assert not robots.can_fetch("InnyBot/2.0", "https://x.pl/prywatne/")

    def test_crawl_delay(self):
        assert RobotsTxt.parse(Z_CRAWL_DELAY).crawl_delay("InnyBot/2.0") == 2.5


class TestPrzypadkiBrzegowe:
    def test_pusty_plik_dopuszcza_wszystko(self):
        assert RobotsTxt.parse("").can_fetch(UA, "https://x.pl/cokolwiek")

    def test_pusty_disallow_oznacza_zgode(self):
        robots = RobotsTxt.parse("User-agent: *\nDisallow:")
        assert robots.can_fetch(UA, "https://x.pl/cokolwiek")

    def test_komentarze_sa_ignorowane(self):
        robots = RobotsTxt.parse("User-agent: *  # wszyscy\nDisallow: /tajne/  # nie wchodzić")
        assert not robots.can_fetch(UA, "https://x.pl/tajne/plik")

    def test_remis_dlugosci_wygrywa_allow(self):
        robots = RobotsTxt.parse("User-agent: *\nDisallow: /abc\nAllow: /abc")
        assert robots.can_fetch(UA, "https://x.pl/abc")

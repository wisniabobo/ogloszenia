"""Warstwa web: SEO, sitemapa, zabezpieczenie zapisu, limit odsłon numerów."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import metruj.api as api
from metruj.api import app
from metruj.settings import get_settings


@pytest.fixture
def client(monkeypatch):
    # Klient testowy przedstawia się jako „testclient", więc adres sieciowy
    # ustawiamy wprost: domyślnie udajemy gościa z internetu, bo tak wygląda
    # publiczna instancja. Test „lokalnie" zmienia to u siebie.
    monkeypatch.setattr(api, "_is_local", lambda request: False)
    return TestClient(app)


@pytest.fixture
def local_client(monkeypatch):
    monkeypatch.setattr(api, "_is_local", lambda request: True)
    return TestClient(app)


@pytest.fixture
def oferta(session):
    """Jedna aktywna oferta w Opolu — tytuły i dane strukturalne mają z czego powstać."""
    from metruj.models import Listing
    from metruj.pipeline.normalize import normalize
    from metruj.scrapers.base import RawListing

    result = normalize(RawListing(
        external_id="seo1", url="https://example.pl/seo/1",
        title="Mieszkanie 2-pokojowe, ul. Ozimska, Opole", source_key="test_seo",
        description="Mieszkanie 49 m2, 2 pokoje, 2 piętro z 3.", price=629000.0,
        images=["https://example.pl/foto.jpg"],
    ), require_region=False)
    listing = Listing(**{**result.data, "city": "Opole", "street": "Ozimska",
                         "voivodeship": "opolskie", "is_original": True})
    session.add(listing)
    # Widoki otwierają własną sesję, więc oferta musi być naprawdę zapisana,
    # a nie tylko wypchnięta do transakcji testu.
    session.commit()
    yield listing
    session.delete(listing)
    session.commit()


def _meta(html: str, name: str) -> str | None:
    match = re.search(rf'<meta name="{name}" content="(.*?)"', html)
    return match.group(1) if match else None


def _canonical(html: str) -> str | None:
    match = re.search(r'<link rel="canonical" href="(.*?)"', html)
    return match.group(1) if match else None


class TestSeo:
    def test_tytul_i_naglowek_z_filtrow(self, client, oferta):
        html = client.get("/nieruchomosci?city=Opole&property_type=mieszkanie").text
        assert "Mieszkania na sprzedaż — Opole" in html
        assert _meta(html, "robots") == "index, follow"

    def test_ulica_w_tytule(self, client, oferta):
        html = client.get("/nieruchomosci?city=Opole&street=Ozimska").text
        assert "ul. Ozimska, Opole" in re.search(r"<title>(.*?)</title>", html).group(1)

    def test_kanoniczny_gubi_parametry_nieistotne(self, client):
        html = client.get("/nieruchomosci?city=Opole&sort=okazje&utm_source=fb").text
        assert _canonical(html).endswith("/nieruchomosci?city=Opole")

    def test_glebokie_filtry_to_noindex(self, client):
        html = client.get("/nieruchomosci?city=Opole&price_min=100000").text
        assert _meta(html, "robots") == "noindex, follow"

    def test_strony_wlasciciela_bez_indeksowania(self, client):
        for path in ("/schowek", "/poszukiwania", "/wejscie"):
            assert _meta(client.get(path).text, "robots") == "noindex, nofollow"

    def test_robots_txt_wskazuje_sitemape(self, client):
        body = client.get("/robots.txt").text
        assert "Disallow: /api/" in body
        assert "sitemap.xml" in body

    def test_sitemapa_ma_spis_i_czesci(self, client):
        index = client.get("/sitemap.xml")
        assert index.status_code == 200
        assert "sitemap-strony.xml" in index.text
        pages = client.get("/sitemap-strony.xml")
        assert pages.status_code == 200
        assert "/nieruchomosci" in pages.text
        assert client.get("/sitemap-oferty-999.xml").status_code == 404

    def test_dane_strukturalne_oferty(self, client, oferta):
        import json

        html = client.get(f"/oferta/{oferta.id}").text
        raw = re.search(r'application/ld\+json">(.*?)</script>', html, re.S).group(1)
        data = json.loads(raw)
        assert data[0]["@type"] == "RealEstateListing"
        assert data[1]["@type"] == "BreadcrumbList"

    def test_stopka_linkuje_do_miast(self, client):
        assert 'class="footer__places"' in client.get("/nieruchomosci").text


class TestZapis:
    """Bez hasła instancja publiczna jest tylko do czytania."""

    def test_bez_hasla_z_zewnatrz_nie_wolno_zapisywac(self, client, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "admin_token", "sekret")
        assert client.post("/api/searches", json={"name": "x"}).status_code == 401
        assert client.delete("/api/searches/1").status_code == 401
        assert client.post("/api/favorites/1").status_code == 401

    def test_z_haslem_wolno(self, client, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "admin_token", "sekret")
        resp = client.post(
            "/api/searches",
            json={"name": "test-zapis", "query": {"city": "Opole"}},
            headers={"X-Admin-Token": "sekret"},
        )
        assert resp.status_code == 200
        client.delete(
            f"/api/searches/{resp.json()['id']}", headers={"X-Admin-Token": "sekret"}
        )

    def test_lokalnie_bez_hasla_dziala(self, local_client, monkeypatch, oferta):
        """Na własnym komputerze schowek ma działać bez konfiguracji."""
        monkeypatch.setattr(get_settings(), "admin_token", "")
        assert local_client.post(f"/api/favorites/{oferta.id}").status_code == 200

    def test_z_internetu_bez_hasla_tylko_do_czytania(self, client, monkeypatch, oferta):
        monkeypatch.setattr(get_settings(), "admin_token", "")
        assert client.post(f"/api/favorites/{oferta.id}").status_code == 403

    def test_formularz_hasla_odrzuca_bledne(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "admin_token", "sekret")
        assert client.post("/wejscie", data={"token": "nie-to"}).status_code == 401
        ok = client.post("/wejscie", data={"token": "sekret"}, follow_redirects=False)
        assert ok.status_code == 303
        assert "metruj_token" in ok.headers.get("set-cookie", "")


class TestLimitNumerow:
    def test_limit_odslon_numeru(self, client, monkeypatch, oferta):
        monkeypatch.setattr(get_settings(), "phone_reveal_limit", 3)
        api._reveals.clear()
        codes = [
            client.get(f"/api/listings/{oferta.id}/kontakt").status_code for _ in range(5)
        ]
        assert codes[-1] == 429
        api._reveals.clear()

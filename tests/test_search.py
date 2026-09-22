"""Wyszukiwanie po miejscowości i ulicy — polskie znaki, przedrostki, odmiana."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from metruj.models import Listing
from metruj.pipeline.normalize import normalize
from metruj.query import Filters, apply_filters, street_terms
from metruj.scrapers.base import RawListing


@pytest.fixture
def streets(session):
    added = []
    for i, (street, city) in enumerate((
        ("Łódzka 12", "Wrocław"),
        ("Jana Pawła II", "Wrocław"),
        ("Ozimskiej", "Opole"),
        ("Czesława Miłosza, Kleczków", "Wrocław"),
    )):
        result = normalize(RawListing(
            external_id=f"ul{i}", url=f"https://example.pl/ul/{i}",
            title="Mieszkanie 2-pokojowe", source_key="test_ulice",
            description="Mieszkanie 49 m2, 2 pokoje.", price=500000.0,
        ), require_region=False)
        data = {**result.data, "street": street, "city": city, "is_original": True}
        listing = Listing(**data)
        session.add(listing)
        added.append(listing)
    session.flush()
    yield
    for listing in added:
        session.delete(listing)
    session.flush()


def _streets(session, **filters) -> set[str]:
    stmt = apply_filters(select(Listing.street), Filters(source=["test_ulice"], **filters))
    return set(session.scalars(stmt))


def test_termy_bez_przedrostka_i_numeru():
    assert street_terms("ul. Browarna 12/4") == ["browarn"]
    assert street_terms("al. Wojska Polskiego") == ["wojsk", "polskiego"]
    assert street_terms("") == []


@pytest.mark.parametrize("query", ["Łódzka", "łódzka", "lodzka", "ul. Łódzka 12"])
def test_ulica_bez_wzgledu_na_ogonki(session, streets, query):
    assert _streets(session, street=query) == {"Łódzka 12"}


def test_ulica_w_innej_kolejnosci_slow(session, streets):
    assert _streets(session, street="Miłosza Czesława") == {"Czesława Miłosza, Kleczków"}
    assert _streets(session, street="Pawła II") == {"Jana Pawła II"}


def test_ulica_w_odmianie(session, streets):
    assert _streets(session, street="Ozimska") == {"Ozimskiej"}


def test_miasto_malymi_literami_i_bez_ogonkow(session, streets):
    assert len(_streets(session, city="wroclaw")) == 3
    assert _streets(session, city="wrocław", street="lodzka") == {"Łódzka 12"}


def test_procent_to_zwykly_znak(session, streets):
    assert _streets(session, street="%") == set()


def test_api_zle_parametry_strony():
    from fastapi.testclient import TestClient

    from metruj.api import app

    client = TestClient(app)
    assert client.get("/api/listings?page=abc").status_code == 200
    assert client.get("/api/listings?per_page=-1").json()["per_page"] == 1
    assert client.get("/api/geojson?limit=-5").status_code == 422
    assert client.get("/api/ulice?q=ab").status_code == 200

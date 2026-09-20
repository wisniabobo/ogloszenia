"""Testy scraperów na zamrożonych odpowiedziach (bez ruchu sieciowego)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ogloszenia.models import OfferKind, PropertyType, SellerType, TransactionType
from ogloszenia.scrapers import ScrapeContext
from ogloszenia.scrapers.bip import BipScraper
from ogloszenia.scrapers.generic_html import GenericHtmlScraper, guess_property_type
from ogloszenia.scrapers.olx import OLXScraper
from ogloszenia.scrapers.otodom import OtodomScraper
from ogloszenia.utils.http import HttpClient

OLX_OFFER = {
    "id": 26034079,
    "url": "https://www.olx.pl/d/oferta/mieszkanie-opole-ID123.html",
    "title": "Mieszkanie z charakterem | po remoncie",
    "description": "Mieszkanie w centrum Opola, ul. Leona Powolnego. Tel 537 123 123",
    "created_time": "2026-09-19T08:00:00+02:00",
    "last_refresh_time": "2026-09-19T09:00:00+02:00",
    "business": True,
    "user": {"name": "ABC Nieruchomości", "is_business": True},
    "location": {
        "city": {"name": "Opole"},
        "district": {"name": "Śródmieście"},
        "region": {"name": "Opolskie"},
        "lat": 50.67, "lon": 17.92,
    },
    "params": [
        {"key": "price", "value": {"value": 629000, "currency": "PLN"}},
        {"key": "m", "value": "49"},
        {"key": "rooms", "value": {"key": "two"}},
        {"key": "floor_select", "value": {"key": "floor_2"}},
        {"key": "builttype", "value": {"key": "blok"}},
    ],
    "photos": [{"link": "https://img.olx.pl/{width}x{height}/a.jpg"}],
    "contact": {"phone": True},   # OLX wystawia tu flagę, nie numer
    "status": "active",
}

OTODOM_NEXT = {
    "props": {
        "pageProps": {
            "data": {
                "searchAds": {
                    "items": [
                        {
                            "id": 64000001,
                            "slug": "mieszkanie-opole-ID4abc",
                            "title": "Przestronne 4-pokojowe mieszkanie 75,28 m²",
                            "totalPrice": {"value": 749000, "currency": "PLN"},
                            "areaInSquareMeters": 75.28,
                            "roomsNumber": "FOUR",
                            "market": "SECONDARY",
                            "ownerType": "PRIVATE",
                            "dateCreated": "2026-09-18 10:00:00",
                            "location": {
                                "address": {
                                    "city": {"name": "Opole"},
                                    "district": {"name": "Półwieś"},
                                    "street": {"name": "Wrocławska"},
                                },
                                "coordinates": {"latitude": 50.66, "longitude": 17.9},
                            },
                            "images": [{"large": "https://img.otodom.pl/1.jpg"}],
                        },
                        {
                            "id": 64000002,
                            "slug": "mieszkanie-nysa-ID5xyz",
                            "title": "Mieszkanie 2 pokoje, Nysa",
                            "totalPrice": {"value": 329000, "currency": "PLN"},
                            "areaInSquareMeters": 42.0,
                            "roomsNumber": "TWO",
                            "ownerType": "AGENCY",
                            "agency": {"name": "Nyskie Nieruchomości"},
                            "location": {"address": {"city": {"name": "Nysa"}}},
                        },
                        {
                            "id": 64000003,
                            "slug": "dom-brzeg-ID6qwe",
                            "title": "Dom wolnostojący, Brzeg",
                            "totalPrice": {"value": 890000, "currency": "PLN"},
                            "areaInSquareMeters": 180.0,
                            "location": {"address": {"city": {"name": "Brzeg"}}},
                        },
                    ]
                }
            }
        }
    }
}

JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@type":"ItemList","itemListElement":[
  {"item":{"@type":"Product","name":"Lokal użytkowy, Kędzierzyn-Koźle",
   "url":"/oferta/555","description":"Lokal 120 m2 w centrum",
   "offers":{"@type":"Offer","price":"450000","priceCurrency":"PLN"},
   "image":"https://example.pl/a.jpg",
   "address":{"addressLocality":"Kędzierzyn-Koźle","streetAddress":"Rynek 5"}}}
]}
</script></head><body></body></html>
"""

SELECTOR_PAGE = """
<html><body>
<article class="offer">
  <a href="/licytacja/9001">Licytacja lokalu mieszkalnego w Nysie</a>
  <span class="price">180 000 zł</span>
  <span class="loc">Nysa, ul. Rynek</span>
  <span class="area">48,5 m2, 2 pokoje</span>
  <time>2026-09-15</time>
</article>
<article class="offer">
  <a href="/licytacja/9002">Licytacja działki, Prudnik, sygn. akt Km 45/24</a>
  <span class="price">95 000 zł</span>
  <span class="loc">Prudnik</span>
  <span class="area">1200 m2</span>
</article>
</body></html>
"""


async def collect(scraper, ctx):
    return [item async for item in scraper.run(ctx)]


@pytest.mark.asyncio
@respx.mock
async def test_olx_parsuje_oferte_z_api():
    route = respx.get(url__startswith="https://www.olx.pl/api/v1/offers/").mock(
        return_value=httpx.Response(200, json={"data": [OLX_OFFER]})
    )

    async with HttpClient() as client:
        # jedna kategoria, żeby test nie zależał od pełnej mapy kategorii OLX
        scraper = OLXScraper(client, {"categories": {"14": ["mieszkanie", "sprzedaz"]}})
        items = await collect(scraper, ScrapeContext(max_pages=1, max_items=10))

    # bot sięga wyłącznie po ścieżkę, którą OLX dopuszcza w robots.txt
    assert all("/api/v1/offers/" in str(call.request.url) for call in route.calls)

    assert len(items) == 1
    offer = items[0]
    assert offer.external_id == "26034079"
    assert offer.price == 629000
    assert offer.area == 49
    assert offer.rooms == 2
    assert offer.floor == 2
    assert offer.city == "Opole"
    assert offer.district == "Śródmieście"
    assert offer.seller_type == SellerType.POSREDNIK
    assert offer.phones_raw == []   # flaga bool nie może trafić do numerów
    assert offer.images == ["https://img.olx.pl/1000x750/a.jpg"]


@pytest.mark.asyncio
@respx.mock
async def test_otodom_czyta_next_data():
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(OTODOM_NEXT)}</script></body></html>"
    )
    respx.get(url__startswith="https://www.otodom.pl/pl/wyniki/").mock(
        return_value=httpx.Response(200, html=html)
    )

    searches = [("sprzedaz", "mieszkanie", PropertyType.MIESZKANIE, TransactionType.SPRZEDAZ)]
    async with HttpClient() as client:
        scraper = OtodomScraper(client, {"searches": searches})
        items = await collect(scraper, ScrapeContext(max_pages=1, max_items=10))

    assert len(items) == 3
    first = items[0]
    assert first.price == 749000
    assert first.area == 75.28
    assert first.rooms == 4
    assert first.district == "Półwieś"
    assert first.street == "Wrocławska"
    assert first.market == "wtorny"
    assert items[1].seller_type == SellerType.POSREDNIK
    assert items[1].seller_name == "Nyskie Nieruchomości"


@pytest.mark.asyncio
@respx.mock
async def test_generic_czyta_jsonld():
    respx.get("https://example.pl/lista").mock(return_value=httpx.Response(200, html=JSONLD_PAGE))

    async with HttpClient() as client:
        scraper = GenericHtmlScraper(
            client, {"urls": ["https://example.pl/lista"]}, source_key="test_jsonld"
        )
        items = await collect(scraper, ScrapeContext(max_pages=1, max_items=10))

    assert len(items) == 1
    assert items[0].price == 450000
    assert items[0].city == "Kędzierzyn-Koźle"
    assert items[0].url == "https://example.pl/oferta/555"


@pytest.mark.asyncio
@respx.mock
async def test_generic_czyta_selektory_i_sygnature():
    respx.get("https://komornik.example/lista").mock(
        return_value=httpx.Response(200, html=SELECTOR_PAGE)
    )

    async with HttpClient() as client:
        scraper = GenericHtmlScraper(
            client,
            {
                "urls": ["https://komornik.example/lista"],
                "list_selector": "article.offer",
                "link_selector": "a",
                "price_selector": ".price",
                "location_selector": ".loc",
                "area_selector": ".area",
                "date_selector": "time",
            },
            source_key="test_komornik",
            kind=OfferKind.LICYTACJA,
        )
        items = await collect(scraper, ScrapeContext(max_pages=1, max_items=10))

    assert len(items) == 2
    assert items[0].price == 180000
    assert items[0].area == 48.5
    assert items[0].rooms == 2
    assert items[0].kind == OfferKind.LICYTACJA
    assert items[1].case_number == "Km 45/24"


def test_rozpoznanie_typu_nieruchomosci():
    assert guess_property_type("Sprzedam dom wolnostojący").value == "dom"
    assert guess_property_type("Działka budowlana 1200 m2").value == "dzialka"
    assert guess_property_type("Hala magazynowa").value == "hala"
    assert guess_property_type("Miejsce postojowe w garażu").value == "garaz"


BIP_SEKCJA = """<html><body>
<div id="tresc">
  <a href="/a,1">Skorochów, działka nr 140/4 — nieruchomość gruntowa niezabudowana</a>
  <a href="/a,2">Informacja o wyniku przetargu na sprzedaż działki nr 12</a>
  <a href="/a,3">Oświadczenia przetargowe do pobrania dla uczestników</a>
  <a href="/a,4">więcej » (GNP.6840.5.2014.JK - Ogłoszenie przetargu na sprzedaż działki)</a>
  <a href="/zalacznik/ogloszenie.pdf">ogłoszenie sprzedaż działki 1303_ (PDF | 114,38KB)</a>
  <a href="/a,5">Przejdź do treści strony ALT + 5</a>
</div></body></html>"""

BIP_OFERTA = """<html><body><div id="tresc">
  <h1>Skorochów, działka nr 140/4</h1>
  <p>Burmistrz ogłasza przetarg ustny nieograniczony na sprzedaż nieruchomości
  gruntowej niezabudowanej położonej w Skorochowie, dz. nr 140/4.</p>
  <p>Cena wywoławcza nieruchomości wynosi: 115 000,00 zł</p>
  <p>Przetarg odbędzie się w dniu 6 października 2026 r. o godzinie 10:00.</p>
  <p>Data publikacji 2026-09-01 08:30</p>
</div></body></html>"""


@pytest.mark.asyncio
@respx.mock
async def test_bip_bierze_oferty_a_pomija_wyniki_druki_i_zalaczniki():
    archiwalna = BIP_OFERTA.replace("Data publikacji 2026-09-01 08:30",
                                    "Data publikacji 2014-09-01 08:30")
    archiwalna = archiwalna.replace("6 października 2026", "6 października 2014")
    respx.get("https://bip.test.pl/sekcja").mock(
        return_value=httpx.Response(200, text=BIP_SEKCJA))
    respx.get("https://bip.test.pl/a,1").mock(
        return_value=httpx.Response(200, text=BIP_OFERTA))
    respx.get("https://bip.test.pl/a,4").mock(
        return_value=httpx.Response(200, text=archiwalna))

    async with HttpClient() as client:
        scraper = BipScraper(
            client,
            {"sections": ["https://bip.test.pl/sekcja"], "commune": "Nysa",
             "authority": "Urząd Miejski w Nysie"},
            source_key="bip_test", name="BIP testowy")
        items = await collect(scraper, ScrapeContext(max_items=10, max_pages=1))

    # Z sześciu odnośników zostaje jeden: wynik przetargu, druk do wypełnienia,
    # załącznik PDF i pozycja nawigacji odpadają, archiwalne ogłoszenie też.
    tytuly = [i.title for i in items]
    assert len(items) == 1, tytuly
    oferta = items[0]
    assert oferta.title.startswith("Skorochów")
    assert oferta.price == 115000.0            # kwota z treści ogłoszenia
    assert oferta.property_type is PropertyType.DZIALKA   # nie „dom" od „niezabudowana"
    assert oferta.transaction is TransactionType.SPRZEDAZ
    assert oferta.event_date.date().isoformat() == "2026-10-06"
    assert oferta.region_assured is True


@pytest.mark.asyncio
@respx.mock
async def test_bip_odrzuca_ogloszenia_sprzed_lat():
    """Sekcje BIP-ów trzymają razem bieżące ogłoszenia i archiwum."""
    stare = BIP_OFERTA.replace("Data publikacji 2026-09-01 08:30",
                               "Data publikacji 2014-08-24 08:33")
    stare = stare.replace("6 października 2026", "6 października 2014")
    respx.get("https://bip.test.pl/sekcja").mock(
        return_value=httpx.Response(200, text=BIP_SEKCJA))
    for n in (1, 4):
        respx.get(f"https://bip.test.pl/a,{n}").mock(return_value=httpx.Response(200, text=stare))

    async with HttpClient() as client:
        scraper = BipScraper(
            client, {"sections": ["https://bip.test.pl/sekcja"]},
            source_key="bip_test", name="BIP testowy")
        items = await collect(scraper, ScrapeContext(max_items=10, max_pages=1))
    assert items == []

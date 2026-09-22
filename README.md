<div align="center">

# metruj

### Wyszukiwarka i analiza ogłoszeń nieruchomości z całej Polski

Oferty z portali, licytacje komornicze i skarbowe, przetargi gmin — w jednym miejscu,
bez powtórek, z ceną porównaną do mediany okolicy.

**▶ Wypróbuj: [bot.wisnia.dev](https://bot.wisnia.dev)**

[![testy](https://github.com/wisniabobo/ogloszenia/actions/workflows/ci.yml/badge.svg)](https://github.com/wisniabobo/ogloszenia/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.10%2B-3776ab)
![licencja](https://img.shields.io/badge/licencja-MIT-green)
![zasięg](https://img.shields.io/badge/zasi%C4%99g-16%20wojew%C3%B3dztw-15654a)

</div>

---

## Co to jest

Ta sama nieruchomość wisi zwykle na kilku portalach naraz, często w różnych
cenach. Nie widać też, czy oferta jest nowa, czy stoi od pół roku i właśnie
potaniała, ani czy 12 000 zł za metr to w tej okolicy dużo, czy mało.

**metruj** zbiera ogłoszenia z ogólnopolskich portali, licytacji
komorniczych i skarbowych, przetargów gmin (BIP) i instytucji publicznych.
Sprowadza je do jednego formatu i skleja powtórki tej samej nieruchomości.
Zapisuje każdą zmianę ceny i porównuje cenę za metr z medianą ofert tego
samego rodzaju w tej samej miejscowości.

| Stan bazy (wrzesień 2026) | |
|---|---:|
| aktywne ogłoszenia | **~180 000** |
| po sklejeniu powtórek | **~146 000** |
| oferty wyraźnie tańsze od mediany okolicy | **~28 000** |
| licytacje i przetargi | **~2 000** |
| biura nieruchomości w rejestrze | **~16 000** |
| źródła danych | **57** |

---

## Wypróbuj

| | |
|---|---|
| 🔎 [Wyszukiwarka](https://bot.wisnia.dev) | województwo, miejscowość, powiat, dzielnica, ulica, cena, metraż, cena za m², pokoje, piętro, rok budowy, działka, rynek, kto wystawia, serwis, jak długo wisi, obniżki |
| 💸 [Okazje](https://bot.wisnia.dev/okazje) | oferty co najmniej 15% tańsze za metr niż mediana w swojej miejscowości albo powiecie |
| 🗺️ [Mapa](https://bot.wisnia.dev/mapa) | wszystkie oferty na mapie, pinezki w kolorze ceny za m², te same filtry co lista |
| ⚖️ [Licytacje](https://bot.wisnia.dev/licytacje) · [Przetargi](https://bot.wisnia.dev/przetargi) | komornicze, skarbowe (KAS), z mas upadłości, gminne z BIP, AMW, PKP; terminy, wadium, oszacowanie |
| 📊 [Rynek](https://bot.wisnia.dev/rynek) | mediany cen, liczba nowych ofert, obniżki, gdzie jest najwięcej ogłoszeń |
| 🏢 [Biura](https://bot.wisnia.dev/biura) | ~16 tys. pośredników i deweloperów z telefonem, miastem i liczbą ofert |
| 🧩 [API](https://bot.wisnia.dev/docs) | otwarte REST API z tymi samymi filtrami co strona |

---

## Funkcje

**Na liście ofert**
- **Jedna nieruchomość = jeden wpis.** Powtórki z innych portali są sklejone, z licznikiem i porównaniem cen między serwisami.
- **Od najnowszych naprawdę od najnowszych.** Liczy się data wystawienia na portalu, a nie data, kiedy ogłoszenie zebraliśmy.
- **Ile oferta wisi i czy potaniała.** Liczba dni na rynku, cena startowa, każda obniżka z datą.
- **Kontakt.** Numer z ogłoszenia, a gdy portal go ukrywa, numer centrali biura, które je wystawiło, z wyraźnym opisem, skąd pochodzi.
- **Alerty i schowek.** Zapisany zestaw filtrów wysyła powiadomienie o nowych pasujących ofertach, raz na nieruchomość, nie raz na portal.

**Na stronie oferty**
- **Podobne oferty.** Ten sam rodzaj i transakcja, cena i metraż w pobliżu, pokoje ±1. Najpierw ta sama miejscowość, potem powiat i województwo, w kolejności podobieństwa, z różnicą ceny za m².
- **Na tle rynku.** Mediana ceny za m² w miejscowości, powiecie i województwie, liczba ofert w próbie i o ile ta oferta od niej odbiega.
- **Historia ceny i ta sama oferta w innych serwisach.**
- **Działka ewidencyjna.** Numer działki pod adresem oferty z rejestru GUGiK.

**Dla licytacji i przetargów**
- Cena wywoławcza, suma oszacowania, wadium i sygnatura akt.
- Terminy w czasie polskim, okno przyjmowania ofert.
- Licytacje po terminie same znikają z listy.

Całość działa na telefonie, tablecie i komputerze. Na telefonie filtry chowają się pod przyciskiem.

---

## Skąd są dane

| Źródło | Co daje |
|---|---|
| **OLX, Otodom, Morizon, Gratka, Domiporta, GetHome** | oferty prywatne, biur i deweloperów z 16 województw |
| **licytacje.komornik.pl** | licytacje komornicze: cena wywoławcza, oszacowanie, rękojmia, termin |
| **e-Licytacje KAS** | licytacje urzędów skarbowych, także etap przed licytacją |
| **Monitor Sądowy i Gospodarczy** | sprzedaż z mas upadłości |
| **BIP gmin i powiatów** | przetargi i wykazy nieruchomości komunalnych, cena czytana z PDF-u |
| **AMW, PKP, KOWR, ZUS** | mienie instytucji publicznych |
| **katalog biur Otodom** | rejestr pośredników z telefonami |

Rejestry i usługi publiczne, na których stoi analiza:
- **GUS BDL**: rejestr TERYT, czyli 16 województw, 380 powiatów i 2 477 gmin;
- **GUGiK**: geokodowanie adresów, przynależność administracyjna, działki ewidencyjne;
- **OpenStreetMap**: Nominatim, Overpass, mapa.

Wszystkie są bezpłatne i bez kluczy.

Zbieramy tylko publicznie dostępne ogłoszenia, z poszanowaniem `robots.txt`
każdego serwisu (parser zgodny z RFC 9309), z limitami zapytań na serwis.
Numery telefonów na liście są maskowane (`537 *** ***`), a pełny numer
pojawia się dopiero po kliknięciu.

---

## Jak to działa

```
portale · licytacje · BIP  ──►  normalizacja  ──►  lokalizacja  ──►  sklejanie powtórek  ──►  mediany i ocena
                                (ceny, daty,       (portal → rejestr     (odcisk parametrów     (miejscowość → powiat
                                 metraż, ary,       TERYT → punkt na      + telefon + adres)     → województwo)
                                 piętro z opisu)    mapie → tekst)
```

- **Lokalizacja z rejestru, nie zgadywana.** Miejscowość z portalu jest dopasowywana do rejestru TERYT z uwzględnieniem polskiej odmiany („w Starogardzie Gdańskim”). Punkt od portalu wskazuje województwo, a współrzędne potwierdza rejestr adresowy GUGiK.
- **Okazja to nie tylko niska cena.** Porównujemy cenę za metr z medianą ofert tego samego rodzaju i transakcji w tej samej miejscowości. Gdy ofert jest za mało, bierzemy powiat, ale nigdy całe województwo. Oferty nieporównywalne, jak udziały, miejsca postojowe czy ruiny, odpadają.
- **Dwa tempa zbierania.** Co kwadrans najnowsze strony każdego źródła, a raz na dobę pełne przejście do ostatniej strony wyników.

---

## API

```http
GET /api/listings?city=Kraków&property_type=mieszkanie&price_max=700000
GET /api/listings?voivodeship=pomorskie&deal_max=0.85&sort=okazje
GET /api/listings/{id}              # oferta + historia cen + kopie w innych serwisach
GET /api/listings/{id}/podobne      # podobne oferty
GET /api/listings/{id}/dzialka      # działka ewidencyjna (GUGiK)
GET /api/listings/{id}/okolica      # szkoły, sklepy, przystanki (OpenStreetMap)
GET /api/geojson?kind=licytacja     # punkty na mapę
GET /api/market-report?city=Kraków&days=90
GET /api/stats · /api/sources · /api/agencies
```

Dokumentacja interaktywna: [bot.wisnia.dev/docs](https://bot.wisnia.dev/docs).

---

## Uruchomienie u siebie

```bash
git clone https://github.com/wisniabobo/ogloszenia.git && cd ogloszenia
make install
.venv/bin/python -m metruj.cli init-db
.venv/bin/python -m metruj.cli scan --region opolskie   # albo bez --region: cała Polska
.venv/bin/python -m metruj.cli geocode
.venv/bin/python -m metruj.cli web                      # http://127.0.0.1:8000
```

Najważniejsze komendy:

```bash
metruj scan [--deep] [--region …]   # zbieranie (--deep: do ostatniej strony wyników)
metruj geocode                      # współrzędne i przynależność z rejestru adresowego
metruj kontakty                     # numery telefonu z kart ofert
metruj napraw                       # przelicza dane zebrane starszym kodem
metruj okazje · search · phone · agencies · stats · export
```

Konfiguracja przez `.env` (wzór: [`.env.example`](.env.example)). Wdrożenie na
serwer, czyli systemd, nginx i kopie zapasowe, opisuje [`deploy/README.md`](deploy/README.md).

---

## Budowa

```
metruj/
  api.py          FastAPI: strona + REST API
  query.py        wspólne filtry i sortowania (strona = API = alerty = mapa), podobne oferty
  scrapers/       24 moduły; ogólny scraper HTML sterowany selektorami z YAML-a
  pipeline/       normalizacja, lokalizacja, sklejanie powtórek, geokodowanie, mediany, naprawa danych
  geo/            rejestr TERYT, odmiana nazw, wykrywanie lokalizacji w tekście
  apis/           GUGiK, Nominatim, Overpass
  web/            szablony i styl
config/           źródła, regiony, rejestr TERYT
scripts/audit.py  przegląd serwisu na żywej bazie
```

Python 3.10+, FastAPI, SQLAlchemy 2, SQLite (WAL), httpx, selectolax, Leaflet.

## Testy

```bash
make dev && make test               # 170 testów
.venv/bin/python scripts/audit.py   # każdy widok, filtr i sortowanie na żywej bazie
```

Testy obejmują:
- polskie liczby i daty, w tym daty ISO i strefy czasowe;
- ary i hektary;
- rozpoznawanie lokalizacji na przypadkach z produkcji;
- sklejanie powtórek, podobne oferty;
- scrapery na zamrożonych odpowiedziach;
- parser robots.txt.

`audit.py` sprawdza, czy sortowanie naprawdę sortuje, a filtr naprawdę zawęża.

## Licencja

MIT. Projekt nie jest powiązany z żadnym z monitorowanych serwisów.

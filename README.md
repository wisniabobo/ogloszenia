<div align="center">

# metruj

### Otwarty monitor rynku nieruchomości — cała Polska

**Wszystkie portale. Licytacje komornicze i skarbowe. Przetargi gmin.
Cena porównana z medianą okolicy.**

Za darmo, bez konta, bez limitów zapytań.

**▶ [bot.wisnia.dev](https://bot.wisnia.dev)**

[![testy](https://github.com/wisniabobo/ogloszenia/actions/workflows/ci.yml/badge.svg)](https://github.com/wisniabobo/ogloszenia/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.10%2B-3776ab)
![licencja](https://img.shields.io/badge/licencja-MIT-green)
![zasięg](https://img.shields.io/badge/zasi%C4%99g-16%20wojew%C3%B3dztw-0a84c4)

</div>

---

## Po co to jest

Ta sama nieruchomość potrafi wisieć na ośmiu portalach, w trzech biurach i pod
czterema różnymi cenami. Kupujący nie ma jak sprawdzić, czy „nowa oferta" to
faktycznie nowa oferta, czy ta sama kawalerka, która stoi od ośmiu miesięcy
i właśnie potaniała o 40 tysięcy. Ani czy 450 000 zł za 50 m² to okazja, czy
przepłacenie — bo to zależy od tego, ile się płaci **w tej okolicy**.

Płatne narzędzia, które to pokazują, kosztują kilkaset złotych miesięcznie
i są skierowane do pośredników.

**To narzędzie robi to samo i jest darmowe.** Zbiera oferty z portali,
obwieszczenia komornicze, licytacje urzędów skarbowych, sprzedaż z mas
upadłości i przetargi gmin, sprowadza wszystko do jednego formatu,
**wykrywa kopie**, liczy **jak długo oferta stoi**, pamięta **każdą zmianę
ceny** i **porównuje cenę za metr z medianą okolicy**.

```bash
git clone https://github.com/wisniabobo/ogloszenia.git && cd ogloszenia
make install
.venv/bin/python -m metruj.cli init-db
.venv/bin/python -m metruj.cli scan
.venv/bin/python -m metruj.cli geocode
.venv/bin/python -m metruj.cli web        # http://127.0.0.1:8000
```

---

## Co robi

| | |
|---|---|
| 🇵🇱 **Cała Polska** | 16 województw, 380 powiatów, 2477 gmin — bez wpisywania regionu na sztywno |
| 💸 **Okazje** | cena za m² wobec **mediany** ofert tego samego rodzaju w tej samej miejscowości |
| 🗺️ **Mapa** | wszystkie oferty na jednej mapie, pinezki w kolorze ceny za m², klastrowanie, szukanie w promieniu |
| 🔁 **Oryginał vs kopia** | ta sama nieruchomość z sześciu portali pokazana **raz**, z licznikiem kopii i porównaniem cen |
| ⏱️ **Ile oferta stoi** | licznik dni od publikacji; oferta wisząca pół roku to inna sytuacja negocjacyjna niż wczorajsza |
| 📉 **Historia ceny** | każda obniżka zapisana z datą i procentem |
| ⚖️ **Licytacje** | komornicze (cena wywoławcza, oszacowanie, rękojmia, termin), skarbowe, syndyczne |
| 🏢 **Rejestr biur** | 13 047 biur i deweloperów z całego kraju, z telefonem i liczbą deklarowanych ofert |
| 📞 **Kontakt** | numer z karty oferty, z treści ogłoszenia albo centrala biura — zawsze z etykietą, skąd pochodzi |
| 🔎 **Wyszukiwanie po telefonie** | czy ta „prywatna" oferta to nie kolejne ogłoszenie tego samego biura |
| 🔔 **Alerty** | zapisane filtry → Telegram / e-mail / webhook, raz na ofertę |
| 🔌 **Otwarte API** | bez kluczy, bez limitów, CORS dla wszystkich |

### Skala

Kontrolny przebieg zwykłego (nie głębokiego) skanu:

```
pobrane 7 976  ·  nowe 7 377  ·  kopie wykryte 323  ·  błędy 0
16 województw  ·  wszystkie typy nieruchomości od mieszkań po hale
```

Sam Otodom deklaruje **150 862 mieszkania na sprzedaż** na 2096 stronach
wyników i wszystkie te strony da się przejść — pełne pokrycie jest kwestią
czasu przebiegu, nie możliwości.

### Dwa tempa zbierania

| | kiedy | co robi |
|---|---|---|
| `metruj scan` | co 15 minut | najnowsze strony każdej sekcji w każdym województwie — nowa oferta trafia do bazy w kilka minut |
| `metruj scan --deep` | raz na dobę | przechodzi wyniki **do ostatniej strony**, jaką portal deklaruje |
| `metruj kontakty` | co godzinę | dociąga karty ofert po numery telefonu |
| `metruj geocode` | po każdym skanie | współrzędne i potwierdzenie regionu z rejestru adresowego |

Bycie pierwszym i posiadanie kompletu to dwa różne zadania i mają różne koszty —
dlatego są to osobne przebiegi, a nie jeden kompromis.

---

## Skąd bierze dane

**57 źródeł** w rejestrze, **26 włączonych**, plus dowolnie wiele stron biur
dokładanych komendą `metruj add-site`. Nic tu nie jest wpisane „z pamięci" —
każdy adres i każde API zostało odpytane, a wyniki (łącznie z porażkami)
zapisane w `config/sources.yaml`.

| Źródło | Co daje | Jak |
|---|---|---|
| **OLX.pl** | oferty prywatne, najszybciej | `/api/v1/offers/` — jedyna ścieżka API, którą OLX sam dopuszcza w robots.txt; 16 regionów × 10 kategorii |
| **Otodom.pl** | oferty biur i deweloperów | `__NEXT_DATA__`, wyszukiwanie „cała Polska", pełna hierarchia administracyjna przy każdej ofercie |
| **Domiporta, Gratka, Morizon, GetHome** | oferty biur | HTML + JSON-LD, adresy z `{region}` rozwijanym na 16 województw |
| **licytacje.komornik.pl** | licytacje komornicze | cena wywoławcza, oszacowanie, rękojmia, termin, adres |
| **eLicytacje KAS** | licytacje urzędów skarbowych | publiczne API; także **etap przed licytacją** |
| **Monitor Sądowy i Gospodarczy** | sprzedaż z mas upadłości | publiczne API wyszukiwarki |
| **BIP gmin i powiatów** | przetargi i wykazy nieruchomości komunalnych | cena wywoławcza czytana z załączonego PDF-u |
| **AMW, PKP, KOWR, ZUS** | mienie instytucji publicznych | listy wyników na stronach instytucji |
| **Katalog biur Otodom** | 13 047 biur i deweloperów | `__NEXT_DATA__` katalogu, stronicowanie po `hasNext` |
| **Strony biur** | oferty, które nie trafiają na portale | sitemap + dane strukturalne |

BIP-y nie są tu przypadkiem: gminy mają **ustawowy obowiązek** publikować wykazy
nieruchomości przeznaczonych do sprzedaży (art. 35 ustawy o gospodarce
nieruchomościami). Tych ogłoszeń nie ma na żadnym portalu.

```bash
metruj sources              # co jest skonfigurowane i w jakim stanie
metruj check-sources        # odpyta każdy adres i pokaże, co odpowiada
```

---

## Lokalizacja: rejestr zamiast zgadywania

To była najdroższa część tego projektu i warto powiedzieć wprost dlaczego.

Poprzednia wersja miała ręcznie spisany słownik 444 miejscowości jednego
województwa i wybierała z tekstu ogłoszenia nazwę, która najlepiej wypadła
w punktacji. Efekty widać było w bazie:

> **„Miejsce postojowe w garażu podziemnym w Krakowie"** → wieś **Miejsce**,
> powiat namysłowski. Bo „Miejsce" to nazwa wsi i stało w tytule wcześniej
> niż Kraków.

Teraz podstawą jest **rejestr TERYT** pobrany z GUS BDL (`config/teryt.json`):
16 województw, 380 powiatów, 2477 gmin, 1022 miasta. Obowiązują trzy zasady:

1. **Pole portalu bije tekst.** OLX podaje województwo w `location.region`,
   Otodom całą hierarchię w `reverseGeocoding` — od województwa po dzielnicę.
   Nie ma czego zgadywać przy 95% zasobu.
2. **Nazwa niebędąca miastem wymaga wskazówki** — „w miejscowości X", „gm. X",
   „pow. X" — albo zgodności z rozpoznanym powiatem.
3. **Nazwy będące zwykłymi słowami wymagają wskazówki zawsze.** W Polsce jest
   gmina Dobra, gmina Nowe i gmina Pokój. Wielka litera niczego nie dowodzi,
   bo tak zaczyna się każde zdanie. Lista jest w `config/nazwy_wieloznaczne.yaml`.

Nad tym stoi **geokoder GUGiK**, który przy każdym adresie oddaje pole
`jednostka` w postaci `{Polska,małopolskie,Kraków,Kraków}` oraz kod TERYT
gminy. To jest mocniejsze niż cokolwiek, co da się wyczytać z tytułu, więc
geokodowanie nie tylko stawia pinezkę — **poprawia województwo, powiat
i gminę** z rejestru adresowego.

Wszystkie przypadki z powyższej listy mają testy w `tests/test_geo.py`, wraz
z polską odmianą: „w **Opolu**", „w **Nysie**", „w **Kędzierzynie-Koźlu**",
„w **Strzelcach Opolskich**".

---

## Okazje: co to znaczy i czego nie znaczy

Cena sama w sobie nic nie mówi. Dlatego dla każdej oferty liczymy stosunek jej
**ceny za metr** do **mediany** ofert tego samego rodzaju i tej samej
transakcji w tej samej miejscowości. Mediana, nie średnia — jedna kamienica
za 40 milionów nie może przestawiać całego miasta.

Poniżej ośmiu ofert w miejscowości mediana jest przypadkiem, nie odniesieniem;
wtedy schodzimy na powiat.

**Czego na liście okazji nie ma i dlaczego:**

- **Porównań do mediany województwa.** Mieszkanie we wsi zestawione z medianą
  województwa zawsze wygląda na okazję życia i nigdy nią nie jest.
- **Altan ROD, kontenerów, pawilonów i kwater pracowniczych.** Pierwsza wersja
  tej listy pokazywała „dom w Warszawie 89% poniżej mediany" — był to kontener
  biurowy. To nie jest tani dom, tylko coś innego wrzuconego do tej samej
  kategorii portalu.
- **Ofert poniżej 35% mediany.** Prawie zawsze jest to udział w nieruchomości,
  ruina, cena „od" albo pomyłka w metrażu.

Każda oferta ma przy sobie napisane, **z czym** jest porównywana: „63% taniej
niż w powiecie" to inna informacja niż „63% taniej niż w województwie".

I rzecz najważniejsza: to jest cena **ofertowa**, nie transakcyjna. Niska cena
bywa skutkiem stanu technicznego, statusu prawnego albo lokalizacji, której
mediana nie widzi. **Lista wskazuje, czemu warto się przyjrzeć, a nie co kupić.**

---

## Darmowe API, na których to stoi

Cały projekt opiera się na publicznych usługach **bez kluczy i bez opłat**.

| Usługa | Do czego | Klucz |
|---|---|---|
| **GUS BDL** | rejestr TERYT: województwa, powiaty, gminy | nie |
| **GUGiK UUG** | geokodowanie polskich adresów + przynależność administracyjna | nie |
| **GUGiK ULDK** | działka ewidencyjna po współrzędnych, z geometrią | nie |
| **Nominatim (OSM)** | zapasowy geokoder, ramki województw | nie |
| **Overpass (OSM)** | co jest w okolicy: szkoły, sklepy, przystanki | nie |
| **BIP gmin** | przetargi i wykazy nieruchomości | nie |
| **eLicytacje KAS · MSiG** | licytacje skarbowe, obwieszczenia syndyków | nie |
| **OpenStreetMap** | kafelki mapy | nie |
| Apify | *opcjonalnie* — gotowe scrapery dla portali z JS | tak, darmowy pakiet |

Geokodowanie idzie kaskadą **adres → ulica → dzielnica → miejscowość**
i wszystko przechodzi przez cache, więc ten sam adres pytamy **raz w życiu**.
Trzy portale z tą samą kamienicą to jedno zapytanie, nie trzy.

> GUS BDL dopuszcza 100 wywołań na kwadrans i mówi to wprost nagłówkiem
> `Retry-After`. Skrypt budujący rejestr czeka dokładnie tyle, ile każe,
> i trzyma pobrane strony na dysku — dlatego rejestr jest w repozytorium,
> a nie pobierany przy starcie.

---

## Telefony: co się da, a czego nie

Stu procent nie będzie i warto powiedzieć wprost dlaczego.

Numer bierzemy z czterech jawnych źródeł, w tej kolejności:

1. **Z karty oferty.** Otodom podaje w `__NEXT_DATA__` osobno numer agenta
   i centralę biura. Na liście wyników numeru nie ma w ogóle, więc jest to
   osobny przebieg: `metruj kontakty` bierze oferty bez kontaktu, najnowsze
   najpierw. Na kontrolnych 40 ofertach numer doszedł przy **39**.
2. **Z treści ogłoszenia**, jeśli sprzedający go tam zostawił.
3. **Z katalogu biur**, w którym pośrednicy sami publikują swój numer.
4. **Z bliźniaczego ogłoszenia** tej samej nieruchomości na innym portalu.

Każdy numer niesie **etykietę pochodzenia** — „z ogłoszenia", „centrala biura"
albo „z karty oferty". Bez tego podsuwalibyśmy numer, sugerując, że stoi
w tym konkretnym ogłoszeniu.

**OLX** ma `/api/v1/offers/{id}/limited-phones/`, ale bez tokenu konta zwraca
400. Bot nie obchodzi tego zabezpieczenia — przy ofertach OLX numer bierzemy
z treści, a gdy go tam nie ma, odsyłamy do „pokaż numer" na samym OLX.

**Facebook Marketplace zostaje poza serwisem.** Regulamin Meta zakazuje
automatycznego pobierania treści bez pisemnej zgody, `robots.txt` Facebooka
zabrania tych ścieżek wprost, a Marketplace wymaga zalogowanego konta.
Zbieranie stamtąd oznaczałoby użycie czyjegoś konta wbrew regulaminowi
i ryzyko spadłoby na właściciela instancji. Dlatego scrapera Marketplace tu
nie ma i nie będzie.

Jeśli masz **własny, legalnie uzyskany** eksport takich ofert (np. pobrany
ręcznie ze swojego konta), wczytasz go bez pisania kodu:

```bash
metruj add-site twoja-strona.pl     # dowolne źródło z sitemapą i danymi strukturalnymi
```

---

## Otwarte API

Bez kluczy, bez rejestracji, bez limitów. CORS otwarty.

```bash
GET /api/listings?city=Kraków&price_max=600000&property_type=mieszkanie
GET /api/listings?deal_max=0.85&deal_level=miasto     # tylko wyraźne okazje
GET /api/listings/{id}              # + historia cen + kopie na innych portalach
GET /api/listings/{id}/okolica      # szkoły, sklepy, przystanki (OpenStreetMap)
GET /api/listings/{id}/dzialka      # działka ewidencyjna z rejestru GUGiK
GET /api/geojson?kind=licytacja     # punkty na mapę
GET /api/phone-lookup?number=537…   # wszystkie oferty spod numeru
GET /api/market-report?city=Kraków&days=90
GET /api/stats  /api/sources  /api/agencies  /api/runs
```

Dokumentacja interaktywna: **`/docs`**.

---

## Jak rozpoznaje kopie

Sygnały, od najmocniejszego:

1. **telefon** — ten sam numer i zbliżony metraż,
2. **odcisk parametrów** — miejscowość + metraż + pokoje + typ + transakcja,
3. **shingle opisu** — odporny na przestawienie zdań; liczony od 12 słów w górę,
4. **licytacje osobno** — łączone tylko po sygnaturze akt albo identycznej cenie
   wywoławczej i terminie.

Nad wszystkim stoi warunek zgodności: **brak danych nie jest sprzecznością, ale
dwie różne znane wartości już tak.** Inna ulica, inne piętro albo cena
rozjeżdżająca się o ponad 25% wykluczają połączenie.

Oryginałem zostaje oferta **najwcześniejsza**; przy remisie prywatna przed biurem.

---

## Zgodność z robots.txt

Bot ma **własny parser robots.txt zgodny z RFC 9309**, bo `urllib.robotparser`
stosuje regułę „pierwsze dopasowanie wygrywa", a standard wymaga **najdłuższego
dopasowania**. Różnica jest praktyczna:

```
# robots.txt OLX
Disallow: /api/
Allow: /api/v1/offers/
```

Zgodnie ze standardem `/api/v1/offers/` jest **dozwolone** — i tylko z tej
ścieżki korzystamy.

**Osobna sprawa: zadeklarowane API.** Nominatim ma w robots.txt
`Disallow: /search`, a jednocześnie w polityce użycia wprost dopuszcza
zapytania API do 1/s z identyfikującym się User-Agentem. Tak samo GUGiK,
Overpass i GUS BDL. Dlatego w kodzie jest jawna lista takich usług
(`DECLARED_APIS` w `utils/http.py`), a każda ma **wpisany na sztywno limit
tempa z własnego regulaminu**. Przeglądanie portali ogłoszeniowych podlega
robots.txt w całości i bez wyjątków.

---

## Numery telefonów a RODO

Numer z ogłoszenia to dana osobowa. Ustawienia domyślne są ostrożne:

- na listach numery są **maskowane** (`537 *** ***`),
- pełny numer wymaga osobnego żądania (`/api/listings/{id}/phone`),
- `METRUJ_STORE_PHONE_HASH_ONLY=true` zapisuje **wyłącznie skrót** —
  deduplikacja nadal działa, a numerów w bazie nie ma w ogóle,
- eksport do CSV/JSON zawiera numery zamaskowane,
- `metruj prune` czyści stare, nieaktywne oferty (domyślnie po 540 dniach).

Administratorem danych zebranych przez instancję jest ten, kto ją uruchamia.

---

## Komendy

```bash
metruj init-db                           # baza + rejestr źródeł
metruj sources · check-sources           # stan źródeł, odpytanie adresów
metruj scan [-s olx] [--region opolskie] # skan (domyślnie: cała Polska)
       [--pages N] [--limit N] [--deep] [--notify]
metruj geocode [--limit N]               # współrzędne + region z rejestru
metruj kontakty [--limit N]              # numery telefonu z kart ofert
metruj okazje [--limit N]                # największe okazje wobec mediany
metruj napraw                            # przelicza pola policzone starym kodem
metruj watch                             # ciągły monitoring
metruj web [--host] [--port]             # interfejs + API
metruj search --city Kraków --price-max 500000
metruj phone 537214908 · agencies · stats · export · prune · searches
```

Stara nazwa `ogl` działa dalej — mają ją skrypty i przyzwyczajenie.

---

## Zawężenie do wybranych województw

Domyślnie serwis zbiera z całego kraju. Własna, mała instancja może to zawęzić:

```bash
METRUJ_VOIVODESHIPS="opolskie,dolnośląskie" metruj scan
metruj scan --region opolskie
```

Zasięg jest parametrem, nie założeniem — i to jest cała różnica wobec
poprzedniej wersji.

---

## Wdrożenie

Jedna komenda, uruchamiana **na serwerze**:

```bash
curl -fsSL https://raw.githubusercontent.com/wisniabobo/ogloszenia/main/deploy/bootstrap.sh \
  | sudo bash -s -- twoja.domena.pl twoj@email.pl
```

Skrypt instaluje zależności, klonuje repozytorium, losuje sól do haszowania
numerów, uruchamia aplikację **na 127.0.0.1:8000** i — jeśli na serwerze jest
nginx — dokłada vhosta tylko dla podanej domeny oraz wystawia certyfikat.
Porty 80 i 443 zostają nietknięte, więc inne strony na tym samym serwerze
działają dalej.

Aktualizacja działającej instalacji: `./deploy/deploy.sh root@twoj.serwer`.
Przejście ze starej instalacji „ogloszenia" na „metruj" (przenosi katalog,
bazę, `.env` i jednostki systemd): `sudo bash deploy/migrate-to-metruj.sh`.

Szczegóły, kopie zapasowe i skalowanie: [`deploy/README.md`](deploy/README.md).

---

## Architektura

```
metruj/
  api.py               FastAPI: widoki + otwarte REST API (gzip, CORS, cache)
  query.py             wspólny builder filtrów — API = interfejs = alerty = mapa
  models.py            model danych
  cli.py               komendy
  geo/
    teryt.py           krajowy rejestr TERYT (16/380/2477)
    gazetteer.py       nazwa -> jednostka, odporna na polską odmianę
    detect.py          lokalizacja z tekstu, ze wskazówkami i wieloznacznościami
    streets.py         ulica i numer domu
  apis/                GUGiK (geokoder + działki), Nominatim, Overpass, Apify
  utils/
    robots.py          parser robots.txt wg RFC 9309
    http.py            limity per host, backoff, jitter, zadeklarowane API
    text.py            polskie liczby, daty, ary i hektary, parametry z opisu
    phones.py          wykrywanie, maskowanie, haszowanie numerów
  scrapers/            18 scraperów; generic_html sterowany selektorami z YAML-a
  pipeline/
    location.py        skąd bierze się lokalizacja oferty (portal > rejestr > tekst)
    normalize.py       parametry z opisu, odsiew, działki
    dedup.py           oryginał vs kopia
    enrich.py          typ oferenta + rejestr biur
    details.py         karty ofert -> numery telefonu
    geocode.py         współrzędne + poprawka regionu z rejestru adresowego
    market.py          mediany i ocena okazyjności
    repair.py          naprawa danych sprzed poprawek
    runner.py          orkiestracja, historia cen, wygaszanie ofert
config/
  teryt.json               rejestr TERYT z GUS BDL
  regions.yaml             parametry portali dla 16 województw
  sources.yaml             57 źródeł
  nazwy_wieloznaczne.yaml  nazwy będące zwykłymi słowami
  agencies.yaml            ziarno rejestru biur
scripts/
  build_teryt.py       odświeżenie rejestru TERYT
  audit.py             przegląd wszystkich widoków, filtrów i sortowań
deploy/                systemd, nginx, skrypty wdrożeniowe i migracyjne
```

---

## Testy

```bash
make dev && make test
.venv/bin/python scripts/audit.py     # przegląd serwisu na żywej bazie
```

**129 testów**: polskie liczby i daty, ary i hektary, telefony, rejestr TERYT,
rozpoznawanie lokalizacji (z osobną klasą `TestBledyZProdukcji` — każdy
przypadek to ogłoszenie, które naprawdę trafiło do bazy pod złym adresem),
normalizacja, deduplikacja, scrapery na zamrożonych odpowiedziach oraz
12 testów parsera robots.txt na regułach z prawdziwych plików portali.

`scripts/audit.py` idzie dalej: odpytuje każdy widok i każdy endpoint na żywej
bazie i sprawdza, czy sortowanie **naprawdę sortuje**, a filtr **naprawdę
zawęża**. Dzięki temu „nie działa filtrowanie" da się sprawdzić jedną komendą.

---

## Współpraca

Najbardziej przydają się: **nowe źródła** (zwłaszcza BIP-y gmin), **poprawki
selektorów**, gdy portal przemebluje front, oraz **przypadki błędnej
lokalizacji** — najlepiej jako test w `tests/test_geo.py`.

Przed zgłoszeniem: `make lint && make test`, a dla nowego źródła
`metruj check-sources --only twoj_klucz`.

---

## Licencja i zastrzeżenia

MIT. Projekt nie jest powiązany z żadnym z monitorowanych serwisów.

Narzędzie zbiera **publicznie dostępne ogłoszenia** i respektuje `robots.txt`.
Odpowiedzialność za zgodność konkretnej instalacji z prawem i regulaminami
serwisów spoczywa na tym, kto ją uruchamia. Dane mają charakter informacyjny —
przed decyzją zakupową zawsze sprawdź ofertę u źródła.

<div align="center">

# ogłoszenia

### Otwarty monitor rynku nieruchomości

**Wszystkie portale. Licytacje komornicze i skarbowe. Przetargi. Na jednej mapie.**

Za darmo, bez konta, bez limitów zapytań.

**▶ [bot.wisnia.dev](https://bot.wisnia.dev)**

[![testy](https://github.com/wisniabobo/ogloszenia/actions/workflows/ci.yml/badge.svg)](https://github.com/wisniabobo/ogloszenia/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.10%2B-3776ab)
![licencja](https://img.shields.io/badge/licencja-MIT-green)
![region](https://img.shields.io/badge/start-woj.%20opolskie-0a84c4)

</div>

---

## Po co to jest

Ta sama nieruchomość potrafi wisieć na ośmiu portalach, w trzech biurach i pod
czterema różnymi cenami. Kupujący nie ma jak sprawdzić, czy „nowa oferta" to
faktycznie nowa oferta, czy ta sama kawalerka, która stoi od ośmiu miesięcy
i właśnie potaniała o 40 tysięcy. Płatne narzędzia, które to pokazują, kosztują
kilkaset złotych miesięcznie i są skierowane do pośredników.

**To narzędzie robi to samo i jest darmowe.** Zbiera oferty z portali, obwieszczenia
komornicze, licytacje urzędów skarbowych, sprzedaż z mas upadłości i przetargi
instytucji publicznych, sprowadza wszystko do jednego formatu, **wykrywa kopie**,
liczy **jak długo oferta stoi** i pamięta **każdą zmianę ceny**.

```bash
git clone https://github.com/wisniabobo/ogloszenia.git && cd ogloszenia
make install
.venv/bin/python -m ogloszenia.cli init-db
.venv/bin/python -m ogloszenia.cli scan
.venv/bin/python -m ogloszenia.cli geocode
.venv/bin/python -m ogloszenia.cli web        # http://127.0.0.1:8000
```

---

## Co robi

| | |
|---|---|
| 🗺️ **Mapa** | wszystkie oferty na jednej mapie, pinezki w kolorze ceny za m², klastrowanie, szukanie w promieniu, mediana cen w widocznym obszarze |
| 🔁 **Oryginał vs kopia** | ta sama nieruchomość z sześciu portali pokazana **raz**, z licznikiem kopii i porównaniem cen między portalami |
| ⏱️ **Ile oferta stoi** | licznik dni od publikacji; oferta wisząca pół roku to inna sytuacja negocjacyjna niż wczorajsza |
| 📉 **Historia ceny** | każda obniżka zapisana z datą i procentem |
| ⚖️ **Licytacje** | komornicze (cena wywoławcza, suma oszacowania, rękojmia, termin), skarbowe, syndyczne — łącznie z etapem **przed licytacją** |
| 🏢 **Rejestr biur** | z katalogu, w którym pośrednicy sami się rejestrują: nazwa, adres, telefon i **ile ofert deklarują** |
| 📊 **Pokrycie** | ile ofert danego biura faktycznie mamy wobec liczby, którą samo podaje — widać, czy zbieranie jest kompletne |
| 📞 **Kontakt** | numer z ogłoszenia, a gdy portal go chowa — centrala biura z katalogu, zawsze z etykietą, skąd pochodzi |
| 🔎 **Wyszukiwanie po telefonie** | czy ta „prywatna" oferta to nie kolejne ogłoszenie tego samego biura |
| 🔔 **Alerty** | zapisane filtry → Telegram / e-mail / webhook, raz na ofertę |
| 🔌 **Otwarte API** | bez kluczy, bez limitów, CORS dla wszystkich — buduj na tym własne rzeczy |

### Skala

Kontrolny przebieg `ogl scan --deep` na woj. opolskim:

```
pobrane 21 693  ·  nowe oferty 5 903  ·  kopie wykryte 1 139  ·  błędy 0
```

W bazie: **5 888 aktywnych ofert**, w tym 923 rozpoznane kopie, **101 licytacji**
i **505 biur** w rejestrze.

### Dwa tempa zbierania

| | kiedy | co robi |
|---|---|---|
| `ogl scan` | co 10–15 minut | najnowsze strony każdej sekcji — nowa oferta trafia do bazy w kilka minut |
| `ogl scan --deep` | raz na dobę | przechodzi wyniki **do końca**, aż strony przestaną wnosić nowe pozycje |

Bycie pierwszym i posiadanie kompletu to dwa różne zadania i mają różne koszty —
dlatego są to dwa tryby, a nie jeden kompromis.

---

## Skąd bierze dane

**57 źródeł** w rejestrze, **29 zweryfikowanych realnym zapytaniem** (19.09.2026),
plus dowolnie wiele stron biur dokładanych komendą `ogl add-site`.
Nic tu nie jest wpisane „z pamięci" — każdy adres i każde API zostało odpytane,
a wyniki (łącznie z porażkami) zapisane w `config/sources.yaml`.

### Działają i wnoszą dane

| Źródło | Co daje | Jak |
|---|---|---|
| **OLX.pl** | oferty prywatne, najszybciej | `/api/v1/offers/` — jedyna ścieżka API, którą OLX sam dopuszcza w robots.txt |
| **Otodom.pl** | oferty biur i deweloperów | dane z `__NEXT_DATA__` |
| **Domiporta.pl** | oferty biur | HTML + JSON-LD |
| **Morizon.pl** | oferty biur | paginacja tylko przez `page=` (reszta zabroniona w robots) |
| **Gratka.pl** | oferty biur | robots dopuszcza wyłącznie `page=2`…`page=10` |
| **licytacje.komornik.pl** | licytacje komornicze | cena wywoławcza, oszacowanie, rękojmia, termin, adres |
| **eLicytacje KAS** | licytacje urzędów skarbowych | publiczne API; także **etap przed licytacją** (opis i oszacowanie) |
| **Monitor Sądowy i Gospodarczy** | sprzedaż z mas upadłości | publiczne API wyszukiwarki MSiG |
| **BIP gmin i powiatów** | przetargi i wykazy nieruchomości komunalnych | 10 biuletynów; cena wywoławcza czytana z załączonego PDF-u |
| **Agencja Mienia Wojskowego** | mieszkania, lokale i grunty po wojsku | jedna lista wyników, województwo podane wprost na karcie |
| **PKP S.A.** | dworce, grunty kolejowe | `pkp.pl/pl/sprzedaz` |
| **Katalog biur** | 505 pośredników z telefonami i liczbą ofert | `__NEXT_DATA__` katalogu Otodom |
| **Strony biur** | oferty, które nie trafiają na portale | sitemap + dane strukturalne |

### Wymagają przeglądarki

Kilka serwisów buduje listę wyników skryptem — w HTML nie ma ani jednej oferty.
Są oznaczone `requires_js: true`, wyłączone, a skan mówi o tym wprost, zamiast
zwracać ciche zero: **Nieruchomosci-online**, **Adresowo**, **KOWR**.
Scrapery czekają gotowe; brakuje tylko renderera (patrz `ogl apify-actors`).

### Pełna mapa źródeł

```
portale ogólnopolskie (17)  OLX · Otodom · Gratka · Morizon · Domiporta · Adresowo
                            Nieruchomosci-online · Oferty.net · Szybko · GetHome
                            RynekPierwotny · TabelaOfert · KRN · Domy.pl · Nportal
                            Facebook Marketplace (wyłączony) · Gumtree (nie istnieje)

portale lokalne (6)         NTO · Opole NaszeMiasto · KedzierzynKozle.info
                            Nysa.info · Brzeg24 · StrzelceOpolskie

licytacje (8)               licytacje.komornik.pl · e-Licytacje · eLicytacje KAS
                            MSiG · KRZ · iMSiG · portale syndyków · KAS

instytucje (8)              KOWR · AMW · ZUS · PKP · Lasy Państwowe · KZN
                            Poczta Polska · KAS

BIP gmin i powiatów (10)    Nysa · Kluczbork · Prudnik · Strzelce Opolskie
                            Krapkowice · Głubczyce · Olesno · Kędzierzyn-Koźle
                            powiat opolski · Urząd Marszałkowski
```

BIP-y nie są tu przypadkiem: gminy mają **ustawowy obowiązek** publikować wykazy
nieruchomości przeznaczonych do sprzedaży (art. 35 ustawy o gospodarce
nieruchomościami). Tych ogłoszeń nie ma na żadnym portalu.

Każdy biuletyn stoi na innym silniku i żaden nie ma API, więc scraper rozpoznaje
ogłoszenia po treści: odsiewa rozstrzygnięcia przetargów, protokoły, druki do
wypełnienia i pozycje nawigacji. Warunki przetargu urzędy publikują w PDF-ie,
nie na stronie — czytamy więc załączniki i wyciągamy z nich cenę wywoławczą.
Przy skanach bez warstwy tekstowej się nie da i wtedy pozycja zostaje bez ceny,
z odnośnikiem do oryginału.

Sekcje biuletynów mieszają bieżące ogłoszenia z archiwum sięgającym 2014 roku.
Datę bierzemy ze stopki redakcyjnej, a gdy jej nie ma — z roku w sygnaturze
sprawy, i pomijamy ogłoszenia starsze niż dwa lata.

**Czego tu nie ma i dlaczego.** e-Zamówienia (BZP) wypadły z serwisu: to
platforma zamówień publicznych, na której gminy ogłaszają, co chcą *kupić*,
a nie co sprzedają. Na 200 sprawdzonych ogłoszeniach z kraju nie było ani
jednego z CPV 70 (usługi w zakresie nieruchomości), a z opolskiego przychodziły
remonty dróg i ubezpieczenie szpitala. Sprzedaż mienia komunalnego ogłasza się
w BIP-ie gminy i stamtąd ją bierzemy.

BIP-y Opola i Brzegu są wyłączone — oba stoją na silniku, który listę ogłoszeń
dociąga skryptem, i w HTML-u zostaje samo „Proszę czekać".

```bash
ogl sources              # co jest skonfigurowane i w jakim stanie
ogl check-sources        # odpyta każdy adres i pokaże, co odpowiada
```

---

## Darmowe API, na których to stoi

Cały projekt opiera się na publicznych usługach **bez kluczy i bez opłat**.
Każda sprawdzona na żywo 19.09.2026.

| Usługa | Do czego | Klucz |
|---|---|---|
| **GUGiK UUG** | geokodowanie polskich adresów — punkty adresowe z ewidencji, razem z kodami TERYT/SIMC/ULIC | nie |
| **GUGiK ULDK** | działka ewidencyjna po współrzędnych lub identyfikatorze, z geometrią | nie |
| **Nominatim (OSM)** | zapasowy geokoder | nie |
| **Overpass (OSM)** | co jest w okolicy: szkoły, sklepy, przystanki, parki | nie |
| **BIP gmin** | przetargi i wykazy nieruchomości | nie |
| **eLicytacje KAS** | licytacje skarbowe | nie |
| **MSiG** | obwieszczenia syndyków | nie |
| **OpenStreetMap** | kafelki mapy | nie |
| Apify | *opcjonalnie* — gotowe scrapery dla portali z JS | tak, darmowy pakiet |

Geokodowanie idzie kaskadą **adres → ulica → dzielnica → miejscowość** i wszystko
przechodzi przez cache, więc ten sam adres pytamy **raz w życiu**. Trzy portale
z tą samą kamienicą to jedno zapytanie, nie trzy.

> **Dlaczego to ma znaczenie:** „Opole" istnieje w Polsce kilka razy. Pierwsza
> wersja geokodera wysłała opolskie mieszkania do **Opola Lubelskiego**, 300 km
> dalej. Teraz wynik jest sprawdzany po kodzie TERYT województwa, a ramka
> współrzędnych stanowi drugą linię obrony. Na kontrolnym przebiegu: **0 punktów
> poza regionem**.

---

## Mapa

Sercem interfejsu jest mapa (`/mapa`):

- **pinezka = cena**, kolor = cena za m² (zielony tani → czerwony drogi),
  licytacje i przetargi mają własne kolory,
- **klastrowanie** — kilka tysięcy ofert nie zamula przeglądarki,
- **szukanie w promieniu** — klikasz punkt, dostajesz wszystko w okolicy,
- **mediana cen w widoku** — jednym kliknięciem wiesz, ile się płaci w tej okolicy,
- oferty bez podanej ulicy są **rozsunięte wokół środka miejscowości**
  i dymek mówi wprost, że to przybliżenie — mapa nie udaje precyzji, której nie ma.

Dane pod mapę idą przez `/api/geojson` — tylko pola potrzebne do narysowania
dymka, spakowane gzipem. 2000 ofert to ~86 kB i kilka milisekund po stronie serwera.

---

## Telefony: co się da, a czego nie

Portale coraz mocniej chowają numery. OLX na zapytanie o telefon odpowiada
wprost `Disallowed for this user` — bez zalogowanego konta numeru nie wyda
i nie ma na to obejścia. W treści ogłoszeń numer podaje mniej niż 5% ofert.

Jest jednak druga, całkowicie jawna droga: **katalog biur, w którym pośrednicy
sami publikują swój numer**. Jeśli ofertę wystawiło biuro, którego numer znamy,
to jest to numer kontaktowy do tej oferty.

Dlatego każdy numer niesie **etykietę pochodzenia** — „z ogłoszenia" albo
„centrala biura". Bez tego rozróżnienia podsuwalibyśmy numer, sugerując, że
stoi w ogłoszeniu. Efekt: kontakt jest dostępny przy **1 867 z 4 965** ofert
zamiast przy 284.

---

## Otwarte API

Bez kluczy, bez rejestracji, bez limitów. CORS otwarty, żeby dało się tego
używać z cudzych stron i skryptów.

```bash
GET /api/listings?city=Opole&price_max=600000&property_type=mieszkanie
GET /api/listings/{id}              # + historia cen + kopie na innych portalach
GET /api/listings/{id}/okolica      # szkoły, sklepy, przystanki (OpenStreetMap)
GET /api/geojson?kind=licytacja     # punkty na mapę
GET /api/phone-lookup?number=537…   # wszystkie oferty spod numeru
GET /api/market-report?city=Opole&days=90
GET /api/stats  /api/sources  /api/agencies  /api/runs
```

Dokumentacja interaktywna: **`/docs`**.

---

## Jak rozpoznaje kopie

Sygnały, od najmocniejszego:

1. **telefon** — ten sam numer i zbliżony metraż,
2. **odcisk parametrów** — miejscowość + metraż + pokoje + typ + transakcja,
   czyli wyłącznie cechy, które podaje *każdy* portal,
3. **shingle opisu** — odporny na przestawienie zdań; liczony od 12 słów w górę,
4. **licytacje osobno** — łączone tylko po sygnaturze akt albo identycznej cenie
   wywoławczej i terminie.

Nad wszystkim stoi warunek zgodności: **brak danych nie jest sprzecznością, ale
dwie różne znane wartości już tak.** Inna ulica, inne piętro albo cena
rozjeżdżająca się o ponad 25% wykluczają połączenie.

Oryginałem zostaje oferta **najwcześniejsza**; przy remisie prywatna przed biurem.

> Obie zasady wzięły się z prawdziwych pomyłek. Obwieszczenia komornicze mają
> tytuły w rodzaju „nieruchomość gruntowa zabudowana" — pierwsza wersja łączyła
> w jedno działki z dwóch różnych powiatów. W drugą stronę: ten sam apartament
> z OLX i Otodom nie był rozpoznawany, bo jeden portal znał piętro, a drugi ulicę.

---

## Rejestr biur nieruchomości

Lista pochodzi z dwóch źródeł, które się uzupełniają:

1. **katalog biur**, w którym pośrednicy sami się rejestrują — stamtąd mamy nazwę,
   adres z kodem pocztowym, telefon i **liczbę aktywnych ofert, którą biuro deklaruje**,
2. **treść ogłoszeń** — biura, których w katalogu nie ma, rozpoznajemy po nazwie
   oferenta; warianty zapisu tej samej firmy scala porównanie rozmyte.

Z pierwszego punktu bierze się rzecz, której płatne narzędzia nie pokazują:
**metryka pokrycia**. Skoro biuro deklaruje 182 oferty, a my mamy 40, to znaczy,
że zbieranie jest niekompletne — i widać to czarno na białym, zamiast zgadywać.

```bash
ogl agencies --min-offers 3
ogl agencies --export config/agencies_opolskie.yaml
```

W repozytorium nie ma wymyślonych nazw ani numerów — biura powstają i znikają,
a lista przepisana z pamięci byłaby fikcją.

---

## Skąd się bierze „300 portali"

Krótko: to w przeważającej części **strony własne biur nieruchomości**, a nie
serwisy ogłoszeniowe. Serwisów z prawdziwego zdarzenia jest w Polsce kilkanaście;
biur z własną stroną — tysiące.

Utrzymywanie selektorów dla tysiąca witryn jest niewykonalne. Ale te strony mają
dwie wspólne cechy, które wystarczą:

1. **`sitemap.xml`** — generuje ją niemal każdy CMS,
2. **dane strukturalne** — JSON-LD `schema.org`, microdata albo OpenGraph,
   wstawiane automatycznie przez wtyczki SEO, więc siedzą tam nawet na stronach,
   których nikt świadomie pod to nie przygotował.

Dlatego jeden scraper obsługuje dowolną liczbę witryn bez kodu per strona:

```bash
ogl add-site investdom.pl
#  adresów wyglądających na oferty: 676
#  ┌─────────┬───────┬────────┬────────────┬────────────┬──────────────────────────┐
#  │ 295 000 │ 220.0 │ 5      │ dom        │ sprzedaz   │ Dom na sprzedaż Walidrogi│
#  └─────────┴───────┴────────┴────────────┴────────────┴──────────────────────────┘
#  Dodano investdom_pl do config/sources.yaml
```

Komenda najpierw **sprawdza**, czy ze strony da się cokolwiek wyciągnąć, pokazuje
próbkę i dopiero wtedy dopisuje wpis. Jeśli nie da rady — mówi to wprost i nic
nie zapisuje, zamiast dokładać martwe źródło do rejestru.

---

## Zgodność z robots.txt

Bot ma **własny parser robots.txt zgodny z RFC 9309**, bo `urllib.robotparser`
z biblioteki standardowej stosuje regułę „pierwsze dopasowanie wygrywa", a
standard wymaga **najdłuższego dopasowania**. Różnica jest praktyczna:

```
# robots.txt OLX
Disallow: /api/
Allow: /api/v1/offers/
```

Zgodnie ze standardem `/api/v1/offers/` jest **dozwolone** — i tylko z tej ścieżki
korzystamy. `urllib` uznawał ją za zabronioną, więc bot nie pobierał niczego
z serwisu, który sam wskazał, co udostępnia.

**Osobna sprawa: zadeklarowane API.** Nominatim ma w robots.txt `Disallow: /search`,
bo nie chce, żeby wyszukiwarki indeksowały dynamiczne wyniki — a jednocześnie
w swojej polityce użycia wprost dopuszcza zapytania API do 1/s z identyfikującym
się User-Agentem. Tak samo GUGiK i Overpass. Dlatego w kodzie jest jawna,
krótka lista takich usług (`DECLARED_APIS` w `utils/http.py`), a każda z nich ma
**wpisany na sztywno limit tempa z własnego regulaminu**. Przeglądanie portali
ogłoszeniowych podlega robots.txt w całości i bez wyjątków.

Poza tym klient HTTP trzyma limit równoległości per host, odstęp między
żądaniami, wykładniczy backoff z jitterem i honoruje `Retry-After`.

---

## Numery telefonów a RODO

Numer z ogłoszenia to dana osobowa. Ustawienia domyślne są ostrożne:

- na listach numery są **maskowane** (`537 *** ***`),
- pełny numer wymaga osobnego żądania (`/api/listings/{id}/phone`) — nie da się
  jednym zapytaniem pobrać całej bazy numerów,
- `OGL_STORE_PHONE_HASH_ONLY=true` zapisuje **wyłącznie skrót** — deduplikacja
  nadal działa, a numerów w bazie nie ma w ogóle,
- eksport do CSV/JSON zawiera numery zamaskowane,
- `ogl prune` czyści stare, nieaktywne oferty (domyślnie po 540 dniach).

Administratorem danych zebranych przez instancję jest ten, kto ją uruchamia.

---

## Komendy

```bash
ogl init-db                              # baza + rejestr źródeł
ogl sources [--enabled] [--category X]   # stan źródeł
ogl check-sources [--only klucz]         # czy adresy odpowiadają
ogl scan [-s olx] [-c licytacje]         # jednorazowy przebieg
         [--pages N] [--limit N] [--no-details] [--all] [--notify]
ogl geocode [--limit N]                  # nadaj współrzędne (GUGiK + OSM)
ogl watch                                # ciągły monitoring
ogl web [--host] [--port]                # interfejs + API
ogl search --city Opole --price-max 500000
ogl phone 537214908                      # wszystkie oferty spod numeru
ogl agencies [--min-offers N] [--export plik.yaml]
ogl apify-actors "nieruchomosci"         # gotowe scrapery dla portali z JS
ogl stats · ogl export plik.csv · ogl prune · ogl searches
```

---

## Wdrożenie

Jedna komenda, uruchamiana **na serwerze**:

```bash
curl -fsSL https://raw.githubusercontent.com/wisniabobo/ogloszenia/main/deploy/bootstrap.sh \
  | sudo bash -s -- twoja.domena.pl twoj@email.pl
```

Skrypt instaluje Dockera, klonuje repozytorium, losuje sól do haszowania
numerów, uruchamia aplikację **na 127.0.0.1:8000** i — jeśli na serwerze jest
nginx — dokłada vhosta tylko dla podanej domeny oraz wystawia certyfikat.
Porty 80 i 443 zostają nietknięte, więc inne strony na tym samym serwerze
działają dalej. Tą samą komendą się aktualizuje.

Dwa kontenery: **web** (uvicorn, 2 procesy) i **worker** (zbieranie ofert osobno,
żeby wolny portal nigdy nie spowolnił strony). Na czystym serwerze bez nginxa
jest jeszcze profil `caddy`, który sam robi HTTPS.

Szczegóły, kopie zapasowe, wariant systemd i skalowanie:
[`deploy/README.md`](deploy/README.md).

---

## Architektura

```
ogloszenia/
  api.py               FastAPI: widoki + otwarte REST API (gzip, CORS, cache)
  query.py             wspólny builder filtrów — API = interfejs = alerty = mapa
  models.py            model danych
  cli.py               komendy
  scheduler.py         każde źródło we własnym tempie
  alerts.py            Telegram / e-mail / webhook
  apis/                darmowe API: GUGiK (geokoder + działki), Nominatim,
                       Overpass (POI), Apify (opcjonalnie)
  utils/
    robots.py          parser robots.txt wg RFC 9309
    http.py            limity per host, backoff, jitter, zadeklarowane API
    text.py            polskie liczby, daty, parametry z opisu
    phones.py          wykrywanie, maskowanie, haszowanie numerów
    geo.py             słownik woj. opolskiego + polska odmiana nazw
  scrapers/            18 scraperów; generic_html sterowany selektorami z YAML-a
  pipeline/
    normalize.py       parametry z opisu, lokalizacja, odsiew
    dedup.py           oryginał vs kopia
    enrich.py          typ oferenta + rejestr biur
    geocode.py         współrzędne z cache'em i walidacją regionu
    runner.py          orkiestracja, historia cen, wygaszanie ofert
config/
  sources.yaml             55 źródeł, 27 zweryfikowanych na żywo
  regions_opolskie.yaml    12 powiatów, 444 miejscowości, dzielnice Opola
  agencies_opolskie.yaml   ziarno rejestru biur
deploy/                Docker Compose, Caddy, systemd, skrypt wdrożeniowy
```

Dopisanie portalu to zwykle wpis w `sources.yaml` (gdy wystarczą selektory) albo
40–80 linii nowej klasy. Selektory siedzą w konfiguracji właśnie dlatego, że
portale przemeblowują front — zmiana szaty graficznej nie powinna wymagać
zmian w kodzie.

---

## Polska odmiana

Osobny akapit, bo to w praktyce największe źródło błędów przy polskich danych.
Wszystkie przypadki mają testy:

- „w **Opolu**", „na **Zaodrzu**", „w **Kędzierzynie-Koźlu**", „w **Strzelcach Opolskich**",
- „na **parterze**", „budynek **parterowy**",
- `1 240 m2` to 1240 m², ale `3 pokoje, 49 m2` to 49 m², a nie 349,
- „mieszkanie 3 **pokoje**" **nie** jest ofertą z gminy **Pokój** — nazwy
  wieloznaczne (Pokój, Dzielnica, Dobra, Sucha, Rogi…) liczą się tylko pisane
  wielką literą,
- „ul. Leona Powolnego**. Kontakt 537…**" — nazwa ulicy kończy się na kropce zdania.

---

## Testy

```bash
make dev && make test
```

84 testy: parsowanie polskich liczb i dat, telefony, słownik geograficzny
z odmianą, normalizacja, deduplikacja (w tym przypadki z prawdziwych obwieszczeń
komorniczych), scrapery na zamrożonych odpowiedziach oraz 12 testów parsera
robots.txt na regułach z prawdziwych plików portali.

---

## Inne województwa

Region jest parametrem, nie założeniem:

```bash
ogl scan --region dolnoslaskie
ogl geocode --region dolnoslaskie
```

Trzeba dodać słownik administracyjny (`config/regions_<woj>.yaml`) i poprawić
parametry regionu w `sources.yaml`. Mapy kodów (OLX, TERYT) mają
już wszystkie 16 województw.

---

## Współpraca

Najbardziej przydają się: **nowe źródła** (zwłaszcza BIP-y gmin), **poprawki
selektorów**, gdy portal przemebluje front, oraz **słowniki innych województw**.

Przed zgłoszeniem: `make lint && make test`, a dla nowego źródła
`ogl check-sources --only twoj_klucz`.

---

## Licencja i zastrzeżenia

MIT. Projekt nie jest powiązany z żadnym z monitorowanych serwisów.

Narzędzie zbiera **publicznie dostępne ogłoszenia** i respektuje `robots.txt`.
Odpowiedzialność za zgodność konkretnej instalacji z prawem i regulaminami
serwisów spoczywa na tym, kto ją uruchamia. Dane mają charakter informacyjny —
przed decyzją zakupową zawsze sprawdź ofertę u źródła.

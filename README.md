# ogłoszenia — monitor rynku nieruchomości, licytacji i przetargów

Bot, który pilnuje rynku **województwa opolskiego**: co kilka minut obchodzi portale
ogłoszeniowe, obwieszczenia komornicze i ogłoszenia instytucji, sprowadza wszystko do
jednego formatu, **wykrywa kopie tej samej nieruchomości**, liczy jak długo oferta stoi,
zapamiętuje zmiany ceny i wysyła powiadomienie, gdy pojawi się coś pasującego do
zapisanych filtrów.

```
ogl scan          # jednorazowy przebieg
ogl watch         # ciągły monitoring wg harmonogramu
ogl web           # interfejs + API na http://127.0.0.1:8000
```

---

## Co to właściwie robi

| | |
|---|---|
| **Zbiera** | portale ogłoszeniowe, licytacje komornicze, przetargi, wykazy urzędowe |
| **Odróżnia** | ofertę **oryginalną** od **kopii** (ta sama nieruchomość na sześciu portalach) |
| **Rozpoznaje** | pośrednik / prywatna / deweloper / komornik / syndyk / urząd |
| **Liczy** | cenę za m², **ile dni oferta stoi**, historię obniżek, liczbę kopii |
| **Buduje** | rejestr biur nieruchomości — z danych, nie z przepisanej listy |
| **Powiadamia** | Telegram, e-mail, webhook — raz na ofertę, tylko o oryginałach |

---

## Instalacja

```bash
git clone https://github.com/wisniabobo/ogloszenia.git
cd ogloszenia
make install          # tworzy .venv i instaluje zależności
cp .env.example .env  # (opcjonalnie) tokeny powiadomień, proxy, baza
.venv/bin/python -m ogloszenia.cli init-db
```

Chcesz najpierw zobaczyć interfejs, bez ruszania sieci:

```bash
.venv/bin/python scripts/seed_demo.py
.venv/bin/python -m ogloszenia.cli web
```

---

## Stan źródeł — sprawdzony na żywo 19.09.2026

Rejestr ma **52 źródła**. To nie jest lista życzeń — każde zostało odpytane komendą
`ogl check-sources`, a te działające przepuszczone przez pełny pipeline.

### Działają i wnoszą dane

| Źródło | Rodzaj | Uwagi |
|---|---|---|
| **OLX.pl** | portal | `/api/v1/offers/` — jedyna ścieżka API, którą OLX sam dopuszcza w robots.txt |
| **Otodom.pl** | portal | dane z `__NEXT_DATA__` |
| **Domiporta.pl** | portal | HTML + JSON-LD |
| **Morizon.pl** | portal | paginacja tylko przez `page=` (reszta parametrów zabroniona) |
| **Gratka.pl** | portal | robots dopuszcza wyłącznie `page=2` … `page=10` |
| **licytacje.komornik.pl** | licytacje | cena wywołania, suma oszacowania, termin, adres |

Kontrolny przebieg (`--pages 2 --limit 60`) dał **228 ofert z woj. opolskiego** i zero błędów.

### Źródła wymagające przeglądarki

Kilka serwisów renderuje listę wyników dopiero skryptem — w HTML-u nie ma ani jednej
oferty. Są oznaczone `requires_js: true`, domyślnie wyłączone, a skan mówi o tym wprost
zamiast zwracać ciche zero:

- **Nieruchomosci-online.pl** — strona odpowiada, ale bez JS nie ma linków do ofert
- **Adresowo.pl** — karta w HTML ma tylko lokalizację; cena i metraż dochodzą skryptem
- **KOWR** (`nieruchomoscikowr.gov.pl`) — lista ofert ładowana skryptem

Żeby je uruchomić, trzeba podpiąć renderer (np. Playwright) i nadpisać
`BaseScraper.html()` tak, by zwracał drzewo po wykonaniu JS. Reszta pipeline'u jest
gotowa — scrapery czekają ze skonfigurowanymi selektorami.

### Do weryfikacji przed włączeniem

Pozostałe wpisy (portale lokalne, BIP-y gmin i powiatów, PKP, KZN, Lasy Państwowe,
KAS, iMSiG) mają wpisane adresy i selektory, ale wymagają dostrojenia. Kilka rzeczy
ustalonych przy sprawdzaniu i zapisanych w `config/sources.yaml`:

- `bip.nysa.pl`, `bip.kluczbork.pl` — działają **bez** `www`
- `olesno.biuletyn.info.pl` → przekierowuje na `bip.olesno.pl` (certyfikat z niepełnym łańcuchem)
- `nieruchomosci.pkp.pl`, `kzn.gov.pl`, `syndyk.pl`, `kedzierzynkozle.info` — nie odpowiadają
- `tabelaofert.pl`, `nto.pl` — odrzucają automaty (HTTP 403)
- **Facebook Marketplace** — świadomie wyłączony: wymaga zalogowanego konta, a regulamin
  Meta zakazuje automatycznego pobierania. Możliwy wyłącznie przez oficjalne API partnerskie.

Zanim włączysz cokolwiek nowego:

```bash
ogl check-sources              # odpyta każdy adres i pokaże, co odpowiada
ogl check-sources --only kowr  # albo pojedyncze źródło
```

### Pełna lista kategorii

```
portale ogólnopolskie (17)   OLX, Otodom, Gratka, Morizon, Nieruchomosci-online,
                             Domiporta, Adresowo, Oferty.net, Szybko, GetHome,
                             RynekPierwotny, TabelaOfert, KRN, Domy.pl, Nportal,
                             Facebook Marketplace (wyłączony), Gumtree (nie istnieje)
portale lokalne (6)          NTO, Opole NaszeMiasto, KedzierzynKozle.info,
                             Nysa.info, Brzeg24, StrzelceOpolskie
licytacje (6)                licytacje.komornik.pl, e-Licytacje, KRZ (syndycy),
                             iMSiG, KAS, portale syndyków
przetargi (2)                e-Zamówienia, BZP
instytucje (7)               KOWR, AMW, ZUS, PKP, Lasy Państwowe, KZN, Poczta Polska
BIP gmin i powiatów (14)     Opole (miasto i powiat), Nysa, Kędzierzyn-Koźle, Brzeg,
                             Kluczbork, Prudnik, Strzelce Opolskie, Krapkowice,
                             Namysłów, Głubczyce, Olesno, Urząd Marszałkowski, OUW
```

BIP-y są tu nieprzypadkowo: gminy mają ustawowy obowiązek publikować wykazy
nieruchomości przeznaczonych do sprzedaży (art. 35 ustawy o gospodarce nieruchomościami).
Tych ogłoszeń nie ma na żadnym portalu.

---

## Lista biur nieruchomości

Pytanie „z których biur ściągasz oferty" ma tylko jedną uczciwą odpowiedź: **z tych,
które faktycznie wystawiają oferty** — a to widać dopiero po skanie. Dlatego rejestr biur
powstaje z danych:

1. każda oferta oznaczona jako pośrednik dokłada nazwę, miasto i telefon,
2. warianty zapisu tej samej firmy („ABC Nieruchomości", „ABC NIERUCHOMOSCI Sp. z o.o.")
   są scalane porównaniem rozmytym,
3. rejestr rośnie przy każdym przebiegu.

```bash
ogl agencies                                       # co już wiemy
ogl agencies --min-offers 3                        # tylko aktywne biura
ogl agencies --export config/agencies_opolskie.yaml
```

`config/agencies_opolskie.yaml` zawiera **ziarno**: wzorce rozpoznawania pośrednika oraz
sieci franczyzowe, po których marce rozpoznajemy oddział. Nie ma tam wymyślonych nazw
i numerów — biura powstają i znikają, a lista przepisana z pamięci byłaby fikcją.

---

## Jak działa rozpoznawanie kopii

Ta sama nieruchomość potrafi wisieć na ośmiu portalach, w trzech biurach i pod czterema
cenami. Bot pokazuje ją **raz**, z licznikiem kopii i porównaniem cen.

Sygnały, od najmocniejszego:

1. **telefon** — ten sam numer + zbliżony metraż,
2. **odcisk parametrów** — miasto + ulica + metraż + pokoje + piętro
   (wymagane min. 3 cechy naprawdę identyfikujące),
3. **shingle opisu** — odporny na przestawienie zdań; liczony dopiero od 12 słów,
4. **licytacje osobno** — łączone wyłącznie po sygnaturze akt albo identycznej cenie
   wywołania i terminie.

Oryginałem zostaje oferta **najwcześniejsza**; przy remisie prywatna przed biurem.

> Ostatni punkt wziął się z prawdziwego błędu: obwieszczenia komornicze mają tytuły
> w rodzaju „nieruchomość gruntowa zabudowana" i pierwsza wersja łączyła w jedno
> działki z dwóch różnych powiatów.

---

## Zgodność z robots.txt

Bot domyślnie respektuje `robots.txt` i ma **własny parser zgodny z RFC 9309**, bo
`urllib.robotparser` z biblioteki standardowej stosuje regułę „pierwsze dopasowanie
wygrywa", a standard wymaga **najdłuższego dopasowania**. Różnica jest praktyczna:

```
# robots.txt OLX
Disallow: /api/
Allow: /api/v1/offers/
```

Zgodnie ze standardem `/api/v1/offers/` jest dozwolone — i to jedyna ścieżka API,
z której korzystamy. `urllib` uznawał ją za zabronioną, więc bot nie pobierał niczego
z serwisu, który sam wskazał, co udostępnia.

Parser obsługuje `*` i `$`, scala grupy o tej samej nazwie user-agenta i czyta
`Crawl-delay`. Poza tym klient HTTP trzyma limit równoległości **per host**, odstęp
między żądaniami, wykładniczy backoff z jitterem i honoruje `Retry-After`.

`OGL_RESPECT_ROBOTS=false` wyłącza sprawdzanie — to świadoma decyzja operatora,
nie domyślne zachowanie.

---

## Numery telefonów a RODO

Numer z ogłoszenia to dana osobowa. Domyślne ustawienia są ostrożne:

- na liście numery są **maskowane** (`537 *** ***`) — tak jak w podglądzie wyników,
- pełny numer wymaga osobnego żądania (`GET /api/listings/{id}/phone`), więc nie da się
  jednym zapytaniem pobrać całej bazy,
- `OGL_STORE_PHONE_HASH_ONLY=true` zapisuje **wyłącznie skrót** numeru — deduplikacja
  nadal działa, a numerów w bazie nie ma,
- eksport do CSV/JSON zawiera numery zamaskowane,
- `ogl prune` czyści stare, nieaktywne oferty (domyślnie po 540 dniach).

Odpowiedzialność za zgodność z prawem i regulaminami serwisów spoczywa na operatorze
bota. Dane osobowe zebrane w ten sposób mają swojego administratora — i jest nim ten,
kto uruchamia bota.

---

## Komendy

```bash
ogl init-db                              # baza + rejestr źródeł
ogl sources [--enabled] [--category X]   # co jest skonfigurowane i w jakim stanie
ogl check-sources [--only klucz]         # czy adresy odpowiadają
ogl scan [-s olx] [-c licytacje]         # jednorazowy przebieg
         [--pages N] [--limit N] [--no-details] [--all] [--notify]
ogl watch                                # ciągły monitoring
ogl web [--host] [--port]                # interfejs + API
ogl search --city Opole --price-max 500000
ogl phone 537214908                      # wszystkie oferty spod numeru
ogl agencies [--min-offers N] [--export plik.yaml]
ogl stats
ogl export wyniki.csv [--city Opole]
ogl prune [--days N]
```

`ogl phone` odpowiada na pytanie, które w tej branży zadaje się najczęściej: czy ta
„prywatna" oferta to nie przypadkiem kolejne ogłoszenie tego samego biura.

---

## Interfejs

| Widok | Co pokazuje |
|---|---|
| **Pulpit** | liczby, oferty wg źródła i miejscowości, dziennik przebiegów |
| **Nieruchomości** | lista z pełnym panelem filtrów (kategoria, lokalizacja, ulica, cena, cena za m², powierzchnia, pokoje, piętro, oferent, źródło, okres dodania, jak długo stoi) |
| **Licytacje** | termin, cena wywołania, suma oszacowania, rękojmia, sygnatura |
| **Przetargi i wykazy** | instytucje, gminy, terminy składania ofert |
| **Karta oferty** | parametry, historia ceny, **ta sama nieruchomość na innych portalach** |
| **Biura** | rejestr zbudowany z ofert |
| **Poszukiwania** | zapisane filtry z powiadomieniami |
| **Źródła** | stan każdego źródła, ostatni przebieg, błędy |

Interfejs działa na telefonie i ma tryb ciemny. Bez build-stepu — czysty CSS i kilkadziesiąt
linii JavaScriptu.

### API

```
GET  /api/listings?city=Opole&price_max=600000&property_type=mieszkanie
GET  /api/listings/{id}            # + historia cen + lista kopii
GET  /api/listings/{id}/phone      # pełny numer (świadome żądanie)
GET  /api/phone-lookup?number=537
GET  /api/stats  /api/sources  /api/agencies  /api/runs
GET  /api/market-report?city=Opole&days=90
POST /api/searches                 # zapisane poszukiwanie z alertem
```

Dokumentacja interaktywna: `/docs`.

---

## Powiadomienia

```bash
# .env
OGL_TELEGRAM_BOT_TOKEN=...
OGL_TELEGRAM_CHAT_ID=...
```

Poszukiwanie zapisujesz przyciskiem „Zapisz jako poszukiwanie" na liście — zapisuje
dokładnie te filtry, które widzisz. Alert leci raz na parę (poszukiwanie, oferta),
domyślnie tylko dla ofert oryginalnych.

---

## Architektura

```
ogloszenia/
  settings.py          konfiguracja (ENV + YAML)
  models.py            model danych
  query.py             wspólny builder filtrów (API = interfejs = alerty)
  api.py               FastAPI: widoki + REST
  cli.py               komendy
  scheduler.py         każde źródło we własnym tempie
  alerts.py            Telegram / e-mail / webhook
  utils/
    robots.py          parser robots.txt wg RFC 9309
    http.py            limity per host, backoff, jitter
    text.py            polskie liczby, daty, parametry z opisu
    phones.py          wykrywanie, maskowanie, haszowanie numerów
    geo.py             słownik woj. opolskiego + polska odmiana nazw
  scrapers/
    base.py            interfejs: zwróć strumień RawListing
    generic_html.py    JSON-LD -> __NEXT_DATA__ -> selektory z YAML-a
    olx.py otodom.py gratka.py morizon.py domiporta.py adresowo.py
    nieruchomosci_online.py komornik.py krz.py ezamowienia.py kowr.py amw.py zus.py
  pipeline/
    normalize.py       uzupełnianie parametrów, geokodowanie, odsiew
    dedup.py           oryginał vs kopia
    enrich.py          typ oferenta + rejestr biur
    runner.py          orkiestracja, historia cen, wygaszanie ofert
config/
  sources.yaml             52 źródła
  regions_opolskie.yaml    12 powiatów, 444 miejscowości, dzielnice Opola
  agencies_opolskie.yaml   ziarno rejestru biur
```

Dopisanie portalu to zwykle wpis w `sources.yaml` (gdy wystarczą selektory) albo
40–80 linii nowej klasy. Selektory siedzą w konfiguracji właśnie dlatego, że portale
przemeblowują front — zmiana szaty graficznej nie powinna wymagać zmian w kodzie.

---

## Polska odmiana

Osobny akapit, bo to w praktyce największe źródło błędów przy polskich danych.
Wszystkie te przypadki mają testy:

- „w **Opolu**", „na **Zaodrzu**", „w **Kędzierzynie-Koźlu**", „w **Strzelcach Opolskich**"
  — dopasowanie po rdzeniu, z limitem długości końcówki,
- „na **parterze**", „budynek **parterowy**",
- `1 240 m2` to 1240 m², ale `3 pokoje, 49 m2` to 49 m², a nie 349,
- „mieszkanie 3 **pokoje**" **nie** jest ofertą z gminy **Pokój** — nazwy wieloznaczne
  (Pokój, Dzielnica, Dobra, Sucha, Rogi…) liczą się tylko pisane wielką literą,
- „ul. Leona Powolnego**. Kontakt 537…**" — nazwa ulicy kończy się na kropce zdania.

---

## Testy

```bash
make dev && make test
```

78 testów: parsowanie polskich liczb i dat, wykrywanie i maskowanie telefonów,
słownik geograficzny z odmianą, normalizacja, deduplikacja (w tym przypadki
z prawdziwych obwieszczeń komorniczych), scrapery na zamrożonych odpowiedziach
oraz 12 testów parsera robots.txt na regułach z prawdziwych plików portali.

---

## Rozszerzenie na inne województwa

Region jest parametrem, nie założeniem:

```bash
ogl scan --region dolnoslaskie
```

Trzeba dodać słownik administracyjny (`config/regions_<woj>.yaml`) i poprawić parametry
regionu w `sources.yaml` — mapa `REGIONS` w scraperze OLX zawiera już wszystkie 16
województw.

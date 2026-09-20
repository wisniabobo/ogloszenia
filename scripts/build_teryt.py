"""Buduje krajowy rejestr podziału administracyjnego (config/teryt.json).

Źródło: **GUS BDL API** (`bdl.stat.gov.pl/api/v1/units`) — publiczne, bez klucza.
Zwraca pełną hierarchię jednostek terytorialnych wraz z identyfikatorami, z
których da się odtworzyć kod TERYT: województwo (2 cyfry), powiat (2), gmina (2)
i rodzaj gminy (1).

Uruchamiane ręcznie, a wynik trafia do repozytorium — podział administracyjny
zmienia się raz na rok, więc pobieranie go przy każdym starcie byłoby
odpytywaniem cudzego serwera bez powodu.

    python scripts/build_teryt.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "config" / "teryt.json"
API = "https://bdl.stat.gov.pl/api/v1/units"

#: BDL dopuszcza 100 wywołań na 15 minut i mówi to wprost nagłówkiem
#: `Retry-After`. Największa strona, jaką oddaje, ma 100 pozycji, więc pełny
#: rejestr to ~45 zapytań — mieści się w limicie, o ile nie ponawiamy go w kółko.
PAGE_SIZE = 100

#: Identyfikator BDL ma 12 znaków: 0 M WW RR PPP GGT
#: (makroregion, województwo, podregion, powiat, gmina + rodzaj).
W, P, G = slice(2, 4), slice(6, 9), slice(9, 12)


CACHE = Path(__file__).resolve().parent.parent / "data" / "bdl-cache"


def fetch(level: int) -> list[dict]:
    """Pobiera wszystkie jednostki danego poziomu, stronicując.

    BDL przy szybkim stronicowaniu potrafi odpowiedzieć 429 albo pustą stroną.
    Cicha przerwa dawała niepełny rejestr (np. 100 powiatów zamiast 382), więc
    każda strona ma trzy podejścia, a niekompletny wynik kończy się błędem
    zamiast zapisem obciętego pliku.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    page = 0
    total: int | None = None
    with httpx.Client(timeout=60, headers={"Accept": "application/json"}) as http:
        while True:
            # Strony trzymamy na dysku: limit BDL to 100 wywołań na kwadrans,
            # a pełny rejestr to ich 45. Bez cache'u każda poprawka w skrypcie
            # kosztowała kwadrans czekania i połowę zapytań szła w błoto.
            cached = CACHE / f"units-{level}-{page}.json"
            if cached.exists():
                payload = json.loads(cached.read_text(encoding="utf-8"))
                rows = payload.get("results") or []
                total = int(payload.get("totalRecords") or 0) if total is None else total
                out.extend(rows)
                if not rows or len(out) >= total:
                    break
                page += 1
                continue
            payload = None
            for attempt in range(4):
                response = http.get(
                    API,
                    params={"level": level, "format": "json",
                            "page-size": PAGE_SIZE, "page": page},
                )
                if response.status_code == 200:
                    payload = response.json()
                    break
                if response.status_code == 429:
                    # serwis podaje, ile czekać („332 sek") — czekamy dokładnie tyle
                    wait = re.search(r"\d+", response.headers.get("retry-after", "")) 
                    delay = int(wait.group()) + 5 if wait else 60 * (attempt + 1)
                    print(f"  limit BDL — czekam {delay} s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                time.sleep(2 ** attempt)
            if payload is None:
                raise RuntimeError(f"BDL nie oddał strony {page} poziomu {level}")
            cached.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            rows = payload.get("results") or []
            total = int(payload.get("totalRecords") or 0) if total is None else total
            out.extend(rows)
            if not rows or len(out) >= total:
                break
            page += 1
            time.sleep(0.5)
    if total and len(out) < total:
        raise RuntimeError(f"poziom {level}: pobrano {len(out)} z {total} jednostek")
    return out


#: BDL nazywa powiaty opisowo („Powiat bocheński", „m. Kraków"), a miasta na
#: prawach powiatu dodatkowo w wariantach historycznych („m. Wałbrzych do 2002").
#: W adresie występuje sama nazwa, więc opis zdejmujemy.
COUNTY_PREFIX = re.compile(
    r"^(?:Powiat|powiat|Miasto na prawach powiatu|Miasto|M\.st\.|m\.st\.|st\.|m\.)\s*"
)
COUNTY_PERIOD = re.compile(r"\s*(?:do|od)\s+\d{4}$")

#: Jednostki zniesione przy reformach (gmina „Warszawa - Bemowo do 2001",
#: powiat „m. Wałbrzych do 2002"). BDL trzyma je dla ciągłości szeregów
#: statystycznych, ale w adresie nie występują i tylko psują dopasowania.
OBSOLETE = re.compile(r"\bdo\s+\d{4}$")

#: Gminy miejsko-wiejskie GUS rozbija na część miejską i wiejską.
SPLIT_SUFFIX = re.compile(r"\s+-\s+(?:miasto|obszar wiejski)$", re.I)


def clean_county(name: str) -> str:
    """„Powiat bocheński" -> „bocheński", „Powiat m. Wałbrzych do 2002" -> „Wałbrzych".

    Przedrostki się nakładają — miasta na prawach powiatu BDL nazywa
    „Powiat m. Kraków", więc jedno przejście zostawiało „m. Kraków".
    """
    out = COUNTY_PERIOD.sub("", name.strip()).strip()
    out = SPLIT_SUFFIX.sub("", out).strip()
    while True:
        stripped = COUNTY_PREFIX.sub("", out).strip()
        if stripped == out:
            return out
        out = stripped


#: Wiersze techniczne rejestru („GMINY-DZIELNICY WARSZAWY NIE USTALONO") —
#: nie są miejscem, tylko zaślepką dla danych bez przypisania.
PLACEHOLDER = re.compile(r"nie ustalono|nieustalon|bez przypisania|pozosta[łl]e", re.I)


def is_obsolete(name: str) -> bool:
    return bool(OBSOLETE.search(name.strip()) or PLACEHOLDER.search(name))


def main() -> int:
    print("Pobieram z GUS BDL…", file=sys.stderr)
    voivodeships = fetch(2)
    counties = fetch(5)
    communes = fetch(6)
    print(f"  województwa {len(voivodeships)} · powiaty {len(counties)} · gminy {len(communes)}",
          file=sys.stderr)

    woj: dict[str, dict] = {}
    for row in voivodeships:
        code = row["id"][W]
        woj[code] = {"code": code, "name": row["name"].lower(), "counties": {}}

    for row in counties:
        rid = row["id"]
        wcode, pcode = rid[W], rid[P][1:]
        if is_obsolete(row["name"]):
            continue
        name = clean_county(row["name"])
        parent = woj.get(wcode)
        if parent is None:
            continue
        # BDL trzyma też warianty historyczne („m. Wałbrzych do 2002"). Pod jednym
        # kodem powiatu zostaje ten bez adnotacji o latach — reszta to ta sama
        # jednostka w innym okresie i w rejestrze adresów nie występuje.
        previous = parent["counties"].get(pcode)
        if previous and len(previous["name"]) <= len(name):
            continue
        parent["counties"][pcode] = {"code": wcode + pcode, "name": name, "communes": {}}

    for row in communes:
        rid = row["id"]
        wcode, pcode, gcode = rid[W], rid[P][1:], rid[G]
        parent = woj.get(wcode, {}).get("counties", {}).get(pcode)
        if parent is None or is_obsolete(row["name"]):
            continue
        parent["communes"][gcode] = {
            "code": wcode + pcode + gcode,   # pełny TERYT gminy (7 znaków)
            "name": clean_county(row["name"]),
            "type": gcode[-1],               # 1 miasto · 2 gmina wiejska · 3 miejsko-wiejska …
        }

    payload = {
        "zrodlo": "GUS BDL API (bdl.stat.gov.pl/api/v1/units), licencja: dane publiczne",
        "pobrano": time.strftime("%Y-%m-%d"),
        "wojewodztwa": [
            {
                "kod": w["code"],
                "nazwa": w["name"],
                "powiaty": [
                    {
                        "kod": c["code"],
                        "nazwa": c["name"],
                        "gminy": [
                            {"kod": g["code"], "nazwa": g["name"], "rodzaj": g["type"]}
                            for g in sorted(c["communes"].values(), key=lambda x: x["code"])
                        ],
                    }
                    for c in sorted(w["counties"].values(), key=lambda x: x["code"])
                    if c["communes"]
                ],
            }
            for w in sorted(woj.values(), key=lambda x: x["code"])
        ],
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    n_pow = sum(len(w["powiaty"]) for w in payload["wojewodztwa"])
    n_gm = sum(len(c["gminy"]) for w in payload["wojewodztwa"] for c in w["powiaty"])
    print(f"Zapisano {OUT}: {len(payload['wojewodztwa'])} województw, "
          f"{n_pow} powiatów, {n_gm} gmin", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

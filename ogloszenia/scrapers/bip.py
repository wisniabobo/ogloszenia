"""Biuletyny Informacji Publicznej gmin i powiatów — przetargi na nieruchomości.

To jedyne miejsce, gdzie ukazują się ogłoszenia o sprzedaży i dzierżawie mienia
komunalnego: wykazy z art. 35 ustawy o gospodarce nieruchomościami oraz
ogłoszenia o przetargach ustnych. Na portalach ogłoszeniowych tego nie ma.

Każdy BIP stoi na innym CMS-ie i żaden nie udostępnia API. Wspólne jest tylko
to, że sekcja „nieruchomości" zawiera listę odnośników do ogłoszeń, a tytuł
ogłoszenia niesie komplet tego, co potrzebne do rozpoznania oferty: tryb
(sprzedaż / dzierżawa), rodzaj (działka, lokal, budynek) i miejscowość.
Dlatego konfiguracja źródła podaje wyłącznie adresy sekcji, a resztę
rozpoznajemy z treści.

Ceny wywoławcze prawie zawsze leżą w załączonym PDF-ie, nie w HTML-u. Jeśli
kwota jest w treści strony — bierzemy ją. Jeśli nie, zapisujemy ogłoszenie bez
ceny i z odnośnikiem do oryginału, zamiast zmyślać liczbę.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from ..models import OfferKind, PropertyType, SellerType, TransactionType, utcnow
from ..utils.text import clean, parse_datetime, parse_number
from .base import BaseScraper, RawListing, ScrapeContext

#: Kontenery treści używane przez popularne silniki BIP-ów.
CONTENT_SELECTORS = ("#tresc", ".tresc", "#content", ".content", "main", "article", "#srodek")

#: Tytuł ogłoszenia o nieruchomości.
OFFER = re.compile(
    r"przetarg|nieruchomo|działk|dzialk|lokal|grunt|budynek|garaż|garaz|"
    r"sprzeda|dzierżaw|dzierzaw|najem|najm|użytkowani|uzytkowani",
    re.I,
)

#: Dokumenty proceduralne wokół przetargu: rozstrzygnięcia, protokoły, listy
#: uczestników. Opisują zakończone postępowanie, więc nie są ofertą.
RESULT = re.compile(
    r"rozstrzygni|wynik\w*\b|unieważni|uniewazni|odwoła|odwola|zakończeni|"
    r"zakonczeni|nie doszedł|nie doszedl|protok[oó][łl]|lista os[oó]b|"
    r"zakwalifikowan|sprostowani|informacja o zamiarze",
    re.I,
)

#: Druki i formularze dołączane do ogłoszeń — to nie są oferty.
FORM = re.compile(
    r"^(?:oświadczeni|oswiadczeni|formularz|regulamin|klauzul|wz[oó]r\b|druk\b|"
    r"wniosek|zgłoszeni|zgloszeni|pełnomocnictw|pelnomocnictw|instrukcj)",
    re.I,
)

#: Widżety list BIP-owych doklejają do tytułu „więcej »" i nawiasy.
LIST_PREFIX = re.compile(r"^\s*(?:więcej|wiecej|czytaj więcej|zobacz)\s*[»>:]*\s*", re.I)

#: Stopka redakcyjna BIP-u. Dwukropek bywa, ale nie musi — bez tego luzu
#: gubiliśmy daty w Kluczborku i przez to trzymaliśmy ogłoszenia z 2020 roku.
PUBLISHED = re.compile(
    r"Data\s+(?:publikacji|wytworzenia|ogłoszenia|ogloszenia)\s*:?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}[.-]\d{1,2}[.-]\d{4})",
    re.I,
)

#: Data doklejona do tytułu: „Wykaz nieruchomości do zbycia - 15.10.2025".
ANY_DATE = re.compile(r"\b(\d{1,2}[.-]\d{1,2}[.-]\d{4}|\d{4}-\d{2}-\d{2})\b")

#: Rok z sygnatury sprawy: „GNP.6840.14.2023.JK", „GG.6840.5.2014.JK".
#: Ostatnia deska ratunku, gdy sekcja BIP-u trzyma archiwum bez dat przy
#: wpisach — a tak jest w Kluczborku, gdzie jedna lista sięga 2014 roku.
CASE_YEAR = re.compile(r"\b\d{3,5}\.\d{1,4}\.(20[0-3]\d)\b")

#: „Ogłoszenie z dnia 06 sierpnia 2026 r. o przetargu…"
TITLE_DATE = re.compile(
    r"z\s+dnia\s+(\d{1,2}\s+\w+\s+\d{4}|\d{1,2}[.-]\d{1,2}[.-]\d{4})", re.I
)

#: Rozszerzenia załączników — nazwa pliku to nie tytuł ogłoszenia.
FILE_HREF = re.compile(r"\.(pdf|docx?|odt|xlsx?|ods|zip|rtf|jpe?g|png|tiff?)(\?|$)", re.I)

#: „(PDF | 114,38KB)" doklejane do nazwy załącznika.
FILE_SUFFIX = re.compile(r"\s*\((?:pdf|docx?|odt|xlsx?|zip|rtf)\s*\|[^)]*\)\s*$", re.I)

#: Elementy nawigacji BIP-u, które wyglądają jak odnośniki do ogłoszeń.
CHROME = re.compile(
    r"ALT ?\+|Przejdź do|Przejdz do|Jak korzystać|deklaracja dostępności|"
    r"mapa (biuletynu|strony|serwisu)|rejestr zmian|historia zmian|drukuj|"
    r"metadane|statystyk|instrukcja|redakcj|archiwum|^\s*(następna|poprzednia|"
    r"ostatnia|pierwsza)\b|^\d+$|wstecz|powrót|powrot|strona główna|rss|"
    r"^wydział|^referat|^biuro\b|katalog usług|katalog uslug|pracownic|"
    r"^regulamin|^zarządzeni|^uchwał|deklaracj",
    re.I,
)

#: Odnośnik do załącznika: albo wprost plik, albo pobieranie z CMS-u.
ATTACHMENT = re.compile(r"\.pdf(\?|$)|action=save|action=show|pobierz|download", re.I)

#: „cena wywoławcza ... 125 000,00 zł"
PRICE = re.compile(
    r"cena\s+wywoławcz\w*[^\d]{0,80}?([\d][\d  .,]{2,18})\s*(?:zł|pln)", re.I
)

#: „dz. nr 820/10", „działki nr 571/62, 571/78"
PARCEL = re.compile(r"dz(?:iał\w*|\.)\s*(?:nr\s*)?([\d]+(?:/\d+)?(?:\s*,\s*\d+(?:/\d+)?)*)", re.I)

#: „Przetarg odbędzie się w dniu 17 listopada 2026 r."
AUCTION_DATE = re.compile(
    r"(?:przetarg|licytacj\w*)[^.]{0,80}?(?:odbędzie się|odbedzie sie|w dniu|dnia)\s*"
    r"(?:w\s+dniu\s*)?(\d{1,2}\s+\w+\s+\d{4}|\d{1,2}[.-]\d{1,2}[.-]\d{4})",
    re.I,
)

#: Kolejność jest istotna. „Nieruchomość gruntowa niezabudowana" musi wpaść do
#: działek, zanim reguła od budynków zobaczy w niej cząstkę „zabudowan", a
#: „zabudowa mieszkaniowa" to przeznaczenie w planie, nie mieszkanie na sprzedaż.
TYPE_RULES = (
    (re.compile(r"niezabudowan|działk|dzialk|\bgrunt|gruntow|rolne|orne", re.I),
     PropertyType.DZIALKA),
    (re.compile(r"lokal\w*\s+mieszkal|\bmieszkani[ae]\b|\bmieszkania\b", re.I),
     PropertyType.MIESZKANIE),
    (re.compile(r"lokal\w*\s+(użytkow|uzytkow|usługow|uslugow|handlow)", re.I),
     PropertyType.LOKAL),
    (re.compile(r"garaż|garaz", re.I), PropertyType.GARAZ),
    (re.compile(r"budynk|budynek|\bdom\b|zabudowan", re.I), PropertyType.DOM),
    (re.compile(r"lokal", re.I), PropertyType.LOKAL),
)

#: Zbycie ma pierwszeństwo: zbiorcze wykazy gmin wymieniają w jednym tytule
#: i sprzedaż, i dzierżawę, a szukający mieszkania pyta przede wszystkim o kupno.
SALE = re.compile(r"sprzeda|zbyci|zbyć|zbyc", re.I)
RENT = re.compile(r"dzierżaw|dzierzaw|najem|najm|wynajm|użytkowani|uzytkowani", re.I)


def _pdf_text(blob: bytes) -> str:
    """Warstwa tekstowa PDF-u. Skany bez tekstu zwracają pusty łańcuch."""
    if not blob.startswith(b"%PDF"):
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(blob))
        pages = reader.pages[:6]  # ogłoszenie mieści się na pierwszych stronach
        return re.sub(r"[ \t\u00a0]+", " ", "\n".join((p.extract_text() or "") for p in pages))
    except Exception:
        return ""


def _content(tree: HTMLParser) -> HTMLParser | object:
    """Zawęża drzewo do właściwej treści, odrzucając nawigację BIP-u."""
    for node in tree.css("script,style,noscript,nav,header,footer"):
        node.decompose()
    for selector in CONTENT_SELECTORS:
        found = tree.css_first(selector)
        if found is not None and len(found.text() or "") > 200:
            return found
    return tree.body or tree


def _page_text(tree: HTMLParser) -> str:
    node = _content(tree)
    return re.sub(r"[ \t ]+", " ", node.text() or "")


class BipScraper(BaseScraper):
    key = "bip"
    name = "Biuletyn Informacji Publicznej"
    kind = OfferKind.PRZETARG
    coverage = "gmina"

    def __init__(self, client, config: dict | None = None, *, source_key: str | None = None,
                 name: str | None = None, kind: OfferKind | None = None) -> None:
        super().__init__(client, config)
        if source_key:
            self.key = source_key
        if name:
            self.name = name
        if kind:
            self.kind = kind
        #: Po ilu dniach ogłoszenie uznajemy za archiwalne. Dwa lata to zapas
        #: na wykazy z art. 35, które potrafią czekać na przetarg kilka miesięcy.
        self.max_age_days = int(self.config.get("max_age_days", 730))

    async def run(self, ctx: ScrapeContext) -> AsyncIterator[RawListing]:
        sections = self.config.get("sections") or self.config.get("urls") or []
        if not sections:
            return
        commune = self.config.get("commune")
        authority = self.config.get("authority") or self.name
        seen: set[str] = set()
        produced = 0

        for section in sections:
            try:
                tree = await self.html(section)
            except Exception:
                continue
            for title, href in self._offer_links(tree, section):
                if href in seen:
                    continue
                seen.add(href)
                item = await self._build(href, title, commune, authority, ctx)
                if item is None:
                    continue
                yield item
                produced += 1
                if produced >= ctx.max_items:
                    return

    # ------------------------------------------------------------------ #
    def _offer_links(self, tree: HTMLParser, section: str) -> list[tuple[str, str]]:
        """Odnośniki do ogłoszeń, z pominięciem menu i rozstrzygnięć."""
        node = _content(tree)
        host = urlparse(section).netloc
        out: list[tuple[str, str]] = []
        for a in node.css("a[href]"):
            raw_href = a.attributes.get("href") or ""
            if FILE_HREF.search(raw_href):
                continue  # załącznik, nie strona ogłoszenia
            title = clean(FILE_SUFFIX.sub("", re.sub(r"\s+", " ", a.text() or "")))
            title = clean(LIST_PREFIX.sub("", title).strip("()").strip())
            href = urljoin(section, raw_href)
            if len(title) < 20 or not OFFER.search(title) or CHROME.search(title):
                continue
            if RESULT.search(title) or FORM.search(title):
                continue  # rozstrzygnięcie albo druk do wypełnienia, nie oferta
            if urlparse(href).netloc != host or href.rstrip("/") == section.rstrip("/"):
                continue
            out.append((title, href))
        return out

    def _attachments(self, tree: HTMLParser, page_url: str) -> list[str]:
        node = _content(tree) if tree.css_first("#tresc") else tree
        out: list[str] = []
        for a in node.css("a[href]"):
            href = a.attributes.get("href") or ""
            if href and ATTACHMENT.search(href):
                full = urljoin(page_url, href)
                if full not in out:
                    out.append(full)
        return out[:3]

    async def _attachment_text(self, urls: list[str]) -> str:
        """Treść pierwszego załącznika, w którym da się znaleźć cenę."""
        collected = ""
        for url in urls:
            try:
                blob = await self.client.get_bytes(url)
            except Exception:
                continue
            text = _pdf_text(blob)
            if not text:
                continue
            collected += " " + text
            if PRICE.search(text):
                break
        return collected

    async def _build(self, url: str, title: str, commune: str | None,
                     authority: str, ctx: ScrapeContext) -> RawListing | None:
        body, attachments = "", []
        if ctx.fetch_details:
            try:
                tree = await self.html(url)
                body = _page_text(tree)
                attachments = self._attachments(tree, url)
            except Exception:
                body = ""
        haystack = f"{title} {body}"

        # Gminy publikują warunki przetargu w załączniku, a na stronie zostawiają
        # sam tytuł. Cena wywoławcza jest w PDF-ie i tylko tam.
        if attachments and not PRICE.search(haystack):
            haystack += " " + await self._attachment_text(attachments)

        if SALE.search(title):
            transaction = TransactionType.SPRZEDAZ
        elif RENT.search(title):
            transaction = TransactionType.WYNAJEM
        else:
            transaction = TransactionType.SPRZEDAZ
        property_type = PropertyType.INNE
        for pattern, kind in TYPE_RULES:
            if pattern.search(title):
                property_type = kind
                break

        price_match = PRICE.search(haystack)
        price = parse_number(price_match.group(1)) if price_match else None

        event_date = None
        date_match = AUCTION_DATE.search(haystack)
        if date_match:
            event_date = parse_datetime(date_match.group(1))

        published = None
        pub_match = PUBLISHED.search(haystack)
        if pub_match:
            published = parse_datetime(pub_match.group(1))
        if published is None:
            title_date = TITLE_DATE.search(title)
            if title_date:
                published = parse_datetime(title_date.group(1))
        if published is None:
            loose = ANY_DATE.search(title)
            if loose:
                published = parse_datetime(loose.group(1))
        if published is None:
            case_year = CASE_YEAR.search(title)
            if case_year:
                # reszta projektu trzyma czasy bez strefy — trzymamy się tego
                published = datetime(int(case_year.group(1)), 12, 31)

        # Sekcje BIP-ów trzymają razem bieżące ogłoszenia i archiwum sprzed lat.
        # Przetarg sprzed czterech lat nie jest ofertą, więc go nie zapisujemy.
        newest = event_date or published
        if newest is not None and (utcnow() - newest).days > self.max_age_days:
            return None

        parcels = PARCEL.search(haystack)

        return RawListing(
            external_id=url,
            url=url,
            title=title,
            source_key=self.key,
            kind=self.kind,
            transaction=transaction,
            property_type=property_type,
            description=clean(body)[:4000] or title,
            price=price,
            opening_price=price,
            commune=commune,
            location_text=commune,
            seller_type=SellerType.INSTYTUCJA,
            seller_name=authority,
            authority=authority,
            event_date=event_date,
            published_at=published,
            extra={"dzialki": clean(parcels.group(1))} if parcels else {},
            # BIP gminy z Opolskiego publikuje wyłącznie swoje nieruchomości.
            region_assured=True,
        )

"""Model danych.

Jedna tabela `listings` obsługuje wszystkie rodzaje ogłoszeń (nieruchomości,
licytacje komornicze/syndyczne, przetargi, wykazy urzędowe). Pola specyficzne
dla licytacji i przetargów są pierwszoklasowe tam, gdzie się po nich filtruje,
a reszta trafia do JSON-owego `extra`.
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    # `timezone.utc`, a nie `datetime.UTC` — to drugie istnieje dopiero od
    # Pythona 3.11, a projekt ma działać też na Ubuntu 22.04 LTS (3.10).
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Słowniki
# --------------------------------------------------------------------------- #
class OfferKind(str, enum.Enum):
    NIERUCHOMOSC = "nieruchomosc"
    LICYTACJA = "licytacja"
    PRZETARG = "przetarg"
    WYKAZ = "wykaz"          # urzędowy wykaz nieruchomości do sprzedaży/dzierżawy
    INNE = "inne"


class TransactionType(str, enum.Enum):
    SPRZEDAZ = "sprzedaz"
    WYNAJEM = "wynajem"
    DZIERZAWA = "dzierzawa"
    ZAMIANA = "zamiana"
    NIEZNANY = "nieznany"


class PropertyType(str, enum.Enum):
    MIESZKANIE = "mieszkanie"
    DOM = "dom"
    DZIALKA = "dzialka"
    LOKAL = "lokal"
    BIURO = "biuro"
    MAGAZYN = "magazyn"
    HALA = "hala"
    GARAZ = "garaz"
    GOSPODARSTWO = "gospodarstwo"
    KAMIENICA = "kamienica"
    POKOJ = "pokoj"
    INNE = "inne"


class SellerType(str, enum.Enum):
    PRYWATNA = "prywatna"
    POSREDNIK = "posrednik"
    DEWELOPER = "deweloper"
    KOMORNIK = "komornik"
    SYNDYK = "syndyk"
    URZAD = "urzad"
    INSTYTUCJA = "instytucja"
    NIEZNANY = "nieznany"


class ListingStatus(str, enum.Enum):
    AKTYWNA = "aktywna"
    NIEAKTYWNA = "nieaktywna"
    ARCHIWALNA = "archiwalna"


# --------------------------------------------------------------------------- #
# Źródła
# --------------------------------------------------------------------------- #
class Source(Base):
    """Rejestr źródeł (portale, instytucje, BIP-y) synchronizowany z config/sources.yaml."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[OfferKind] = mapped_column(Enum(OfferKind), default=OfferKind.NIERUCHOMOSC)
    category: Mapped[str] = mapped_column(String(64), default="portal")
    base_url: Mapped[str | None] = mapped_column(String(400))
    scraper: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    coverage: Mapped[str] = mapped_column(String(32), default="krajowy")  # krajowy/regionalny/lokalny
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    notes: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    # statystyki runtime
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    total_listings: Mapped[int] = mapped_column(Integer, default=0)

    listings: Mapped[list[Listing]] = relationship(back_populates="source")


# --------------------------------------------------------------------------- #
# Biura nieruchomości / oferenci
# --------------------------------------------------------------------------- #
class Agency(Base):
    """Biuro nieruchomości — rejestr obejmuje całą Polskę.

    Rekordy powstają z katalogu biur Otodom (pełna lista z sitemapy portalu)
    oraz są dopisywane automatycznie na podstawie nazw oferentów z ofert.
    """

    __tablename__ = "agencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    county: Mapped[str | None] = mapped_column(String(120))
    voivodeship: Mapped[str | None] = mapped_column(String(64))
    website: Mapped[str | None] = mapped_column(String(400))
    email: Mapped[str | None] = mapped_column(String(200))
    phones: Mapped[list] = mapped_column(JSON, default=list)
    source_hint: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300))
    postal_code: Mapped[str | None] = mapped_column(String(8))
    profile_url: Mapped[str | None] = mapped_column(String(500))
    #: ile ofert biuro deklaruje w katalogu portalu — pozwala sprawdzić,
    #: czy nasz skan faktycznie zebrał wszystko
    listings_expected: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered: Mapped[bool] = mapped_column(Boolean, default=False)  # wykryte automatycznie
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    listings_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)

    listings: Mapped[list[Listing]] = relationship(back_populates="agency")


# --------------------------------------------------------------------------- #
# Ogłoszenia
# --------------------------------------------------------------------------- #
class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source_key", "external_id", name="uq_listing_source_external"),
        Index("ix_listing_geo", "voivodeship", "city", "district"),
        # Filtr „miejscowość" bez województwa jest najczęstszym zapytaniem
        # w całym serwisie, a indeks złożony zaczynający się od województwa
        # nic w nim nie pomaga.
        Index("ix_listing_city", "city"),
        Index("ix_listing_county", "county"),
        Index("ix_listing_kind_status", "kind", "status"),
        Index("ix_listing_seen", "first_seen_at", "last_seen_at"),
        Index("ix_listing_fp", "fingerprint"),
        Index("ix_listing_price", "price"),
        # mapa pyta o prostokąt widoku — bez tego indeksu każde przesunięcie
        # mapy skanowałoby całą tabelę
        Index("ix_listing_latlon", "lat", "lon"),
        Index("ix_listing_map", "status", "is_original", "lat"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- pochodzenie ---
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(800))

    # --- klasyfikacja ---
    kind: Mapped[OfferKind] = mapped_column(Enum(OfferKind), default=OfferKind.NIERUCHOMOSC, index=True)
    transaction: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType), default=TransactionType.SPRZEDAZ
    )
    property_type: Mapped[PropertyType] = mapped_column(
        Enum(PropertyType), default=PropertyType.INNE, index=True
    )
    status: Mapped[ListingStatus] = mapped_column(Enum(ListingStatus), default=ListingStatus.AKTYWNA)

    # --- treść ---
    title: Mapped[str] = mapped_column(String(600))
    description: Mapped[str | None] = mapped_column(Text)
    images: Mapped[list] = mapped_column(JSON, default=list)

    # --- parametry ---
    price: Mapped[float | None] = mapped_column(Float)
    initial_price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="PLN")
    price_per_m2: Mapped[float | None] = mapped_column(Float)
    area: Mapped[float | None] = mapped_column(Float)
    plot_area: Mapped[float | None] = mapped_column(Float)
    rooms: Mapped[int | None] = mapped_column(Integer)
    floor: Mapped[int | None] = mapped_column(Integer)
    floors_total: Mapped[int | None] = mapped_column(Integer)
    year_built: Mapped[int | None] = mapped_column(Integer)
    building_type: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str | None] = mapped_column(String(16))  # pierwotny / wtorny

    # --- lokalizacja ---
    voivodeship: Mapped[str | None] = mapped_column(String(64), default="opolskie")
    county: Mapped[str | None] = mapped_column(String(120))
    commune: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(160), index=True)
    district: Mapped[str | None] = mapped_column(String(160))
    street: Mapped[str | None] = mapped_column(String(200))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    geo_precision: Mapped[str | None] = mapped_column(String(16))   # address/street/city
    geo_source: Mapped[str | None] = mapped_column(String(24))      # gugik/nominatim/portal
    teryt: Mapped[str | None] = mapped_column(String(16), index=True)
    #: Cena za metr tej oferty podzielona przez medianę rynkową dla tej samej
    #: miejscowości, typu i transakcji. 1,00 to dokładnie mediana; 0,70 znaczy
    #: „trzydzieści procent poniżej tego, co się tu płaci". Puste = nie ma
    #: z czym porównać (za mało ofert w okolicy albo nietypowy przedmiot).
    deal_ratio: Mapped[float | None] = mapped_column(Float, index=True)
    #: Na jakim poziomie liczone jest odniesienie: miasto, powiat, województwo.
    #: Uczciwość wobec czytającego: „30% poniżej mediany województwa" to inna
    #: informacja niż „30% poniżej mediany dzielnicy".
    deal_level: Mapped[str | None] = mapped_column(String(16))
    #: Kiedy dociągnęliśmy kartę oferty. Pusty znaczy „jeszcze nie próbowano" —
    #: dzięki temu przebieg po numery telefonu nie wraca w kółko do tych samych
    #: ofert, które kartę mają, ale numeru w niej nie było.
    detail_fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    simc: Mapped[str | None] = mapped_column(String(16))
    postal_code: Mapped[str | None] = mapped_column(String(8))
    poi: Mapped[dict] = mapped_column(JSON, default=dict)           # odległości do udogodnień

    # --- oferent ---
    seller_type: Mapped[SellerType] = mapped_column(
        Enum(SellerType), default=SellerType.NIEZNANY, index=True
    )
    seller_name: Mapped[str | None] = mapped_column(String(300))
    agency_id: Mapped[int | None] = mapped_column(ForeignKey("agencies.id"))
    contact_email: Mapped[str | None] = mapped_column(String(200))

    # --- deduplikacja ---
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    phone_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    text_shingle: Mapped[str | None] = mapped_column(String(64))
    is_original: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )
    copies_count: Mapped[int] = mapped_column(Integer, default=0)

    # --- czas ---
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    #: Kiedy oferta **pojawiła się na rynku**: data wystawienia z portalu,
    #: a gdy portal jej nie podaje — chwila, w której ją zobaczyliśmy.
    #:
    #: To po tej dacie sortuje „od najnowszych" i liczy „na rynku od". Wcześniej
    #: sortowanie szło po `first_seen_at`, czyli po dacie **naszego** pierwszego
    #: spotkania — po każdym pełnym przejściu przez portal na górę listy
    #: wskakiwały dziesiątki tysięcy ogłoszeń sprzed miesięcy, a „dodane dziś"
    #: pokazywało 52 tysiące ofert. Osobna, indeksowana kolumna zamiast
    #: `coalesce()` w zapytaniu, bo to najczęstsze sortowanie w całym serwisie.
    listed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # --- licytacje / przetargi ---
    event_date: Mapped[datetime | None] = mapped_column(DateTime)   # termin licytacji / otwarcia ofert
    deadline: Mapped[datetime | None] = mapped_column(DateTime)     # termin składania ofert / wpłaty wadium
    opening_price: Mapped[float | None] = mapped_column(Float)      # cena wywoławcza
    estimate_value: Mapped[float | None] = mapped_column(Float)     # suma oszacowania
    deposit: Mapped[float | None] = mapped_column(Float)            # wadium / rękojmia
    case_number: Mapped[str | None] = mapped_column(String(120))    # sygnatura / nr sprawy
    authority: Mapped[str | None] = mapped_column(String(300))      # komornik / syndyk / zamawiający
    share: Mapped[str | None] = mapped_column(String(32))           # udział, np. 1/2

    # --- reszta ---
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    source: Mapped[Source | None] = relationship(back_populates="listings")
    agency: Mapped[Agency | None] = relationship(back_populates="listings")
    phones: Mapped[list[Phone]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    price_history: Mapped[list[PriceHistory]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", order_by="PriceHistory.changed_at"
    )

    # ------------------------------------------------------------------ #
    @property
    def market_since(self) -> datetime:
        """Data wystawienia — z portalu, a w jej braku z naszego pierwszego spotkania."""
        return self.listed_at or self.published_at or self.first_seen_at

    @property
    def concluded(self) -> bool:
        """Licytacja albo przetarg, których termin minął — to już nie oferta.

        Komornicy, BIP-y i KAS nie zdejmują ogłoszeń od razu po licytacji,
        więc zbieracz przy każdym przejściu przywracał je do aktywnych.
        Liczy się najpóźniejszy termin (koniec przyjmowania ofert albo sama
        licytacja), z dniem zapasu na przesunięcia.
        """
        if self.kind not in (OfferKind.LICYTACJA, OfferKind.PRZETARG):
            return False
        terms = [moment for moment in (self.event_date, self.deadline) if moment]
        if not terms:
            return False
        from .utils.text import to_polish_time

        return max(terms) < to_polish_time(utcnow()) - timedelta(days=1)

    @property
    def offer_window(self) -> bool:
        """Sprzedaż z oknem na oferty: od `event_date` do `deadline`.

        e-Licytacje KAS przy sprzedaży przez zbieranie ofert podają początek
        i koniec przyjmowania ofert, bez jednego terminu licytacji. Pokazane
        jako „termin 26.11, oferty do 14.01" wyglądało, jakby oferty składało
        się po licytacji.
        """
        return bool(self.event_date and self.deadline and self.deadline > self.event_date)

    @property
    def days_on_market(self) -> int:
        end = self.removed_at or utcnow()
        return max(0, (end - self.market_since).days)

    @property
    def is_fresh(self) -> bool:
        """„NOWA" = wystawiona w ciągu ostatniej doby.

        Liczyła się godzina od naszego pierwszego spotkania — przez co po
        pełnym przejściu przez portal znaczek „NOWA" dostawało kilkadziesiąt
        tysięcy ogłoszeń wiszących od miesięcy.
        """
        return (utcnow() - self.market_since).total_seconds() < 86400


class Phone(Base):
    __tablename__ = "phones"
    __table_args__ = (UniqueConstraint("listing_id", "e164", name="uq_phone_listing"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    e164: Mapped[str] = mapped_column(String(32), index=True)
    national: Mapped[str | None] = mapped_column(String(32))
    masked: Mapped[str | None] = mapped_column(String(32))
    hashed: Mapped[str | None] = mapped_column(String(64), index=True)
    origin: Mapped[str] = mapped_column(String(32), default="opis")  # opis/api/strona/kontakt
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="phones")


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    price: Mapped[float | None] = mapped_column(Float)
    previous_price: Mapped[float | None] = mapped_column(Float)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="price_history")

    @property
    def delta_pct(self) -> float | None:
        if not self.previous_price or not self.price:
            return None
        return round((self.price - self.previous_price) / self.previous_price * 100, 2)


class DuplicateLink(Base):
    """Powiązanie kopii z ofertą oryginalną (z oceną pewności)."""

    __tablename__ = "duplicate_links"
    __table_args__ = (UniqueConstraint("original_id", "copy_id", name="uq_dup_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    original_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    copy_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(32))  # phone/fingerprint/text/image
    score: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- #
# Poszukiwania i alerty
# --------------------------------------------------------------------------- #
class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    query: Mapped[dict] = mapped_column(JSON, default=dict)
    channels: Mapped[list] = mapped_column(JSON, default=list)  # telegram/email/webhook
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    only_original: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime)
    hits: Mapped[int] = mapped_column(Integer, default=0)


class AlertLog(Base):
    __tablename__ = "alert_log"
    __table_args__ = (UniqueConstraint("search_id", "listing_id", name="uq_alert_once"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("saved_searches.id", ondelete="CASCADE"))
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)


class Favorite(Base):
    """Schowek."""

    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("listing_id", "folder", name="uq_fav"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    folder: Mapped[str] = mapped_column(String(120), default="domyslny")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScanRun(Base):
    """Dziennik przebiegów crawlera — do diagnostyki i wykresu 'zdrowia' źródeł."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str | None] = mapped_column(Text)


class Region(Base):
    """Słownik miejscowości/powiatów (woj. opolskie) — do normalizacji lokalizacji."""

    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    voivodeship: Mapped[str | None] = mapped_column(String(64))
    county: Mapped[str] = mapped_column(String(120), index=True)
    commune: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(160), index=True)
    normalized: Mapped[str] = mapped_column(String(160), index=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    teryt: Mapped[str | None] = mapped_column(String(16))


class AppState(Base):
    """Stan aplikacji: przede wszystkim znaczniki jednorazowych migracji danych.

    Niektórych napraw nie wolno wykonać dwa razy. Odwrócenie zamienionego
    dnia z miesiącem jest poprawne na danych zapisanych starym kodem, ale na
    danych już poprawionych zamieniłoby je z powrotem.
    """

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MarketStat(Base):
    """Mediana ceny za metr dla zakresu (miasto/powiat/województwo + typ).

    Liczona po skanie i zapisywana, bo odniesienie rynkowe ma być tanie do
    odczytania: bez niego każda lista „okazji" musiałaby liczyć medianę
    w locie dla każdej oferty z osobna.
    """

    __tablename__ = "market_stats"
    __table_args__ = (
        UniqueConstraint("level", "scope", "property_type", "transaction",
                         name="uq_market_scope"),
        Index("ix_market_scope", "level", "scope"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))       # miasto | powiat | wojewodztwo
    scope: Mapped[str] = mapped_column(String(160))
    property_type: Mapped[str] = mapped_column(String(32))
    transaction: Mapped[str] = mapped_column(String(32))
    median_price_m2: Mapped[float] = mapped_column(Float)
    sample: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GeocodeCache(Base):
    """Zapamiętane wyniki geokodowania.

    Darmowe usługi mają limity (Nominatim: 1 zapytanie na sekundę), a adresy
    powtarzają się między portalami. Cache sprawia, że ten sam adres pytamy raz
    w życiu — dzięki temu skan tysięcy ofert nie obciąża cudzych serwerów.
    """

    __tablename__ = "geocode_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    query: Mapped[str] = mapped_column(String(400))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    precision: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(String(24))
    teryt: Mapped[str | None] = mapped_column(String(16))
    simc: Mapped[str | None] = mapped_column(String(16))
    #: Hierarchia administracyjna z odpowiedzi GUGiK („{Polska,opolskie,nyski,
    #: Nysa}"). Trzymamy ją w cache'u, bo to ona pozwala poprawić województwo
    #: i powiat oferty bez ponownego odpytywania rejestru.
    jednostka: Mapped[str | None] = mapped_column(String(200))
    postal_code: Mapped[str | None] = mapped_column(String(8))
    city: Mapped[str | None] = mapped_column(String(160))
    street: Mapped[str | None] = mapped_column(String(200))
    hits: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def found(self) -> bool:
        return self.lat is not None and self.lon is not None

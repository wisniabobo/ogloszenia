"""Warstwa dostępu do bazy (SQLAlchemy 2.0, domyślnie SQLite w trybie WAL)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    # Skan zapisuje partiami, ale przy równoległych zadaniach warto dać
    # zapisowi czas na doczekanie swojej kolei zamiast wywalać się od razu.
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    url = get_settings().database_url
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, future=True, pool_pre_ping=True)
        event.listen(_engine, "connect", _configure_sqlite)
    else:
        _engine = create_engine(url, future=True, pool_pre_ping=True, pool_size=10, max_overflow=20)

    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sesja z automatycznym commit/rollback."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _add_missing_columns(engine: Engine, metadata) -> list[str]:
    """Dopisuje do istniejących tabel kolumny, których w nich jeszcze nie ma.

    `create_all` tworzy brakujące **tabele**, ale nie rusza tych, które już są.
    Przy wdrożeniu na działającą bazę (a taka stoi na serwerze z kilkudziesięcioma
    tysiącami ofert) nowe pole modelu kończyło się błędem „no such column"
    w pierwszym zapytaniu po restarcie.

    Alembic byłby tu armatą na wróbla: wszystkie zmiany schematu w tym projekcie
    to dokładanie kolumn, a `ALTER TABLE ADD COLUMN` jest w SQLite operacją
    natychmiastową i bezpieczną. Kolumn nie usuwamy i nie zmieniamy ich typów —
    gdyby kiedyś zaszła taka potrzeba, będzie to świadoma, osobna migracja.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = CreateColumn(column).compile(engine).string
                # SQLite nie przyjmuje NOT NULL bez wartości domyślnej przy
                # dokładaniu kolumny do niepustej tabeli — a wszystkie nasze
                # nowe pola i tak są opcjonalne.
                ddl = ddl.replace(" NOT NULL", "")
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
                added.append(f"{table.name}.{column.name}")
    return added


def _add_missing_indexes(engine: Engine, metadata) -> list[str]:
    """Zakłada indeksy dopisane do modelu po utworzeniu tabeli.

    `create_all` pomija tabele, które już istnieją — razem z ich indeksami.
    Indeks na miejscowości dopisany do modelu nigdy więc nie powstał na
    serwerze i filtr „miasto" dalej czytał całą tabelę.
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    created: list[str] = []
    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {index["name"] for index in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name and index.name not in have:
                index.create(engine)
                created.append(index.name)
    return created


def _relax_not_null(engine: Engine, metadata) -> list[str]:
    """Zdejmuje `NOT NULL` z kolumn, które w modelu są już opcjonalne.

    SQLite nie umie zmienić więzów kolumny — trzeba przebudować tabelę.
    Powód, dla którego to tu jest, wyszedł na produkcji: rejestr biur objął
    cały kraj, więc pole „województwo" przestało być obowiązkowe (katalog
    portalu nie zawsze je podaje). Tabela założona wcześniej wciąż wymagała
    wartości i **co drugi zapis biura kończył się błędem**, a razem z nim
    przepadała cała oferta, przy której to biuro rozpoznaliśmy.

    Przebudowa jest bezpieczna: nowa tabela powstaje z modelu, dane przenosimy
    po nazwach wspólnych kolumn, stara znika dopiero po udanym przepisaniu.
    """
    from sqlalchemy import inspect

    if not engine.url.get_backend_name().startswith("sqlite"):
        return []

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    fixed: list[str] = []

    for table in metadata.sorted_tables:
        if table.name not in existing:
            continue
        in_db = {col["name"]: col for col in inspector.get_columns(table.name)}
        stale = [
            column.name
            for column in table.columns
            if column.nullable
            and column.name in in_db
            and not in_db[column.name]["nullable"]
            and not column.primary_key
        ]
        if not stale:
            continue

        target, source, fills = _copy_expression(table, in_db)
        _rebuild_table(engine, inspector, table, target, source, fills)
        fixed.append(f"{table.name}({', '.join(stale)})")
    return fixed


def _copy_expression(table, in_db) -> tuple[str, str, dict]:
    """Lista kolumn do przepisania i wyrażenie źródłowe.

    Stara tabela może mieć puste wartości tam, gdzie nowa ich wymaga — kolumna
    dostała w modelu wartość domyślną już po jej założeniu. Przy przepisywaniu
    podstawiamy tę wartość, zamiast wywracać migrację na jednym wierszu
    sprzed poprawki.
    """
    shared = [name for name in in_db if name in table.columns]
    fills: dict[str, object] = {}
    source_parts: list[str] = []
    for name in shared:
        column = table.columns[name]
        fill = _column_default(column)
        if not column.nullable and not column.primary_key and fill is not None:
            fills[f"fill_{name}"] = fill
            source_parts.append(f'COALESCE("{name}", :fill_{name})')
        else:
            source_parts.append(f'"{name}"')
    return ", ".join(f'"{name}"' for name in shared), ", ".join(source_parts), fills


def _rebuild_table(engine: Engine, inspector, table, target: str, source: str,
                   fills: dict) -> None:
    """Zakłada tabelę od nowa z modelu i przepisuje do niej dane.

    Jedna rzecz jest tu nieoczywista i kosztowała produkcję: nowoczesne SQLite
    przy `ALTER TABLE … RENAME` **przepisuje odwołania w innych tabelach**.
    Zmiana nazwy `agencies` na roboczą sprawiała więc, że klucz obcy w tabeli
    ofert zaczynał wskazywać tabelę roboczą — a po jej skasowaniu wskazywał
    w próżnię i każdy zapis oferty kończył się „no such table".

    Dlatego na czas przebudowy włączamy `legacy_alter_table`, czyli dokładnie
    to, co zaleca procedura zmiany schematu z dokumentacji SQLite.
    """
    from sqlalchemy import text

    backup = f"{table.name}__stare"
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text("PRAGMA legacy_alter_table=ON"))
        try:
            for index in inspector.get_indexes(table.name):
                if index.get("name"):
                    conn.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
            conn.execute(text(f'DROP TABLE IF EXISTS "{backup}"'))
            conn.execute(text(f'ALTER TABLE "{table.name}" RENAME TO "{backup}"'))
            table.create(conn)
            conn.execute(
                text(f'INSERT INTO "{table.name}" ({target}) SELECT {source} FROM "{backup}"'),
                fills,
            )
            conn.execute(text(f'DROP TABLE "{backup}"'))
        finally:
            conn.execute(text("PRAGMA legacy_alter_table=OFF"))
            conn.execute(text("PRAGMA foreign_keys=ON"))


def _repair_dangling_references(engine: Engine, metadata) -> list[str]:
    """Odtwarza tabele, których klucze obce wskazują tabelę roboczą.

    Ślad po nieudanej przebudowie: w definicji tabeli zostaje odwołanie do
    `…__stare`, której już nie ma. Zapis do takiej tabeli kończy się błędem
    „no such table", choć sama tabela wygląda na zdrową.
    """
    from sqlalchemy import inspect, text

    if not engine.url.get_backend_name().startswith("sqlite"):
        return []

    with engine.connect() as conn:
        broken = [
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%__stare%'")
            )
        ]
    if not broken:
        return []

    inspector = inspect(engine)
    repaired: list[str] = []
    for table in metadata.sorted_tables:
        if table.name not in broken:
            continue
        in_db = [col["name"] for col in inspector.get_columns(table.name)]
        target, source, fills = _copy_expression(table, in_db)
        _rebuild_table(engine, inspector, table, target, source, fills)
        repaired.append(table.name)
    return repaired


def _column_default(column) -> object | None:
    """Wartość domyślna kolumny w postaci nadającej się do zapisu w SQLite."""
    import enum
    import json

    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False) and not callable(
        getattr(default, "arg", None)
    ):
        value = getattr(default, "arg", None) if default is not None else None
    else:
        value = default.arg
    if callable(value):
        try:
            value = value(None)
        except TypeError:
            value = value()
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        # Kolumny wyliczeniowe trzymają w SQLite **nazwę** składowej
        # („NIERUCHOMOSC"), a nie jej wartość („nieruchomosc"). Zapisanie
        # wartości dałoby wiersz, którego model nie potrafi potem odczytać.
        return value.name
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def init_db() -> None:
    import logging

    from . import models  # noqa: F401  (rejestracja mapperów)

    log = logging.getLogger("metruj.db")
    engine = get_engine()
    models.Base.metadata.create_all(engine)
    added = _add_missing_columns(engine, models.Base.metadata)
    if added:
        log.info("Uzupełniono schemat bazy o kolumny: %s", ", ".join(added))
    relaxed = _relax_not_null(engine, models.Base.metadata)
    if relaxed:
        log.info("Zdjęto wymóg wartości z kolumn: %s", ", ".join(relaxed))
    created = _add_missing_indexes(engine, models.Base.metadata)
    if created:
        log.info("Założono brakujące indeksy: %s", ", ".join(created))
    repaired = _repair_dangling_references(engine, models.Base.metadata)
    if repaired:
        log.info("Odtworzono tabele z uszkodzonymi kluczami obcymi: %s", ", ".join(repaired))

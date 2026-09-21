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
    from sqlalchemy import inspect, text

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

        shared = [name for name in in_db if name in table.columns]
        target = ", ".join(f'"{name}"' for name in shared)

        # Stara tabela mogła mieć puste wartości tam, gdzie nowa ich wymaga
        # (kolumna dostała w modelu wartość domyślną już po jej założeniu).
        # Przy przepisywaniu podstawiamy tę wartość domyślną, zamiast wywracać
        # migrację na jednym wierszu sprzed poprawki.
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
        source = ", ".join(source_parts)
        backup = f"{table.name}__stare"

        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            for index in inspector.get_indexes(table.name):
                if index.get("name"):
                    conn.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
            conn.execute(text(f'ALTER TABLE "{table.name}" RENAME TO "{backup}"'))
            table.create(conn)
            conn.execute(
                text(f'INSERT INTO "{table.name}" ({target}) SELECT {source} FROM "{backup}"'),
                fills,
            )
            conn.execute(text(f'DROP TABLE "{backup}"'))
            conn.execute(text("PRAGMA foreign_keys=ON"))
        fixed.append(f"{table.name}({', '.join(stale)})")
    return fixed


def _column_default(column) -> object | None:
    """Wartość domyślna kolumny w postaci nadającej się do zapisu w SQLite."""
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

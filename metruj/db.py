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


def init_db() -> None:
    from . import models  # noqa: F401  (rejestracja mapperów)

    engine = get_engine()
    models.Base.metadata.create_all(engine)
    added = _add_missing_columns(engine, models.Base.metadata)
    if added:
        import logging

        logging.getLogger("metruj.db").info(
            "Uzupełniono schemat bazy o kolumny: %s", ", ".join(added)
        )

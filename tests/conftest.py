from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True, scope="session")
def _isolated_db(tmp_path_factory):
    """Każdy przebieg testów dostaje własną bazę."""
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["OGL_DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["OGL_PHONE_HASH_SALT"] = "test-salt"
    os.environ["OGL_RESPECT_ROBOTS"] = "false"

    from ogloszenia import db as db_module
    from ogloszenia.settings import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._SessionFactory = None
    db_module.init_db()
    yield


@pytest.fixture
def session():
    from ogloszenia.db import get_session_factory

    s = get_session_factory()()
    try:
        yield s
        s.commit()
    finally:
        s.close()

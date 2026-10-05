"""Test fixtures.

The database URL is redirected to a throwaway SQLite file *before* the app is
imported, because ``Settings`` is cached at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="parkwise-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["ENVIRONMENT"] = "test"
os.environ["AUTO_SEED"] = "true"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["JWT_SECRET"] = "test-jwt-secret"

from sqlalchemy.orm import Session  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.db.seed import seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    init_db()
    with SessionLocal() as db:
        seed(db)


@pytest.fixture
def db_session() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.rollback()
    finally:
        db.close()

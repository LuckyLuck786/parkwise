"""Database engine, session, the UTC-aware column type, and app-state helpers.

Two details matter for the deployment and are easy to get wrong:

1. **SQLite strips timezone info.** A naive/aware comparison then raises
   ``TypeError: can't compare offset-naive and offset-aware datetimes`` deep
   inside a query. ``UTCDateTime`` normalises to aware UTC on both dialects.
2. **Serverless instances are short-lived and concurrent.** ``NullPool`` avoids
   holding connections that outlive a lambda, and nothing is cached in-process
   that must survive a request.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

logger = logging.getLogger("parkwise.db")

BACKEND_DIR = Path(__file__).resolve().parents[2]
BACKEND_DB_PATH = BACKEND_DIR / "parkwise.db"


def _normalise_url(url: str) -> str:
    """Neon hands out ``postgres://``; SQLAlchemy 2.x wants an explicit driver."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _resolve_url() -> str:
    if settings.database_url:
        return _normalise_url(settings.database_url)
    return f"sqlite:///{BACKEND_DB_PATH}"


class UTCDateTime(TypeDecorator):
    """A datetime that is always timezone-aware UTC on read.

    Postgres keeps the offset; SQLite drops it. Normalising on the way in and
    out means the rest of the codebase can assume aware UTC and never has to
    guess which dialect it is running on.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


_URL = _resolve_url()
_IS_SQLITE = _URL.startswith("sqlite")

if _IS_SQLITE:
    # One shared connection file. Concurrency safety for "two scans at once" is
    # enforced by the engine's transaction and by the unique constraint on
    # active bay occupancy, not by the pool.
    engine = create_engine(
        _URL, future=True, connect_args={"check_same_thread": False, "timeout": 30}
    )
else:
    # NullPool: a serverless lambda must never hold a connection open.
    engine = create_engine(_URL, future=True, poolclass=None)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def get_db() -> Session:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(*, wait_seconds: int = 30) -> None:
    """Create tables, retrying while a cold Neon wakes up."""
    from app.db import models  # noqa: F401  (import registers the mappers)

    deadline = time.monotonic() + wait_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            Base.metadata.create_all(bind=engine)
            return
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            logger.warning("database not ready (%s); retrying", exc)
            time.sleep(2)
    raise RuntimeError(f"database unavailable after {wait_seconds}s: {last_error}")


def _state_module():
    from app.db import models

    return models


def get_state(db: Session, key: str, default: Any = None) -> Any:
    """Read a key from the tiny app-state table."""
    models = _state_module()
    row = db.scalar(select(models.AppState).where(models.AppState.key == key))
    return row.value if row is not None else default


def set_state(db: Session, key: str, value: Any) -> Any:
    """Upsert a key in the tiny app-state table."""
    models = _state_module()
    row = db.scalar(select(models.AppState).where(models.AppState.key == key))
    if row is None:
        row = models.AppState(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()
    return row


def database_kind() -> str:
    return "postgresql" if engine.dialect.name.startswith("postgres") else engine.dialect.name


def ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False

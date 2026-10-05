"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.database import SessionLocal


def get_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


#: The metadata MUST be Depends(...), not the bare function, or FastAPI tries to
#: treat the session as a request field.
DbSession = Annotated[Session, Depends(get_session)]

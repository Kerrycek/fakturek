from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from fakturek.settings import get_settings


class Base(DeclarativeBase):
    """SQLAlchemy declarative base.

    Models will be introduced in FÁZE 2.
    """


def create_db_engine() -> Engine:
    """Create SQLAlchemy Engine.

    We use sync SQLAlchemy for simplicity/minimalism.
    """

    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=3600,
    )


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session."""

    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_ping() -> None:
    """Lightweight DB check: SELECT 1."""

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

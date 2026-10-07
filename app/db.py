"""Database engine and session handling (SQLAlchemy 2.0)."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    # SQLite connections are bound to the thread that created them by default.
    # FastAPI runs sync endpoints in a thread pool, so that check is turned off;
    # each request still gets its own session.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Create the SQLite folder if needed, then any missing tables."""
    from app import models  # noqa: F401 - registers the tables on Base.metadata

    url = make_url(settings.database_url)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with SessionLocal() as session:
        yield session


def get_session_factory() -> sessionmaker:
    """FastAPI dependency for work that outlives the request and needs its own sessions."""
    return SessionLocal

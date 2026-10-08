"""Database engine and session handling (SQLAlchemy 2.0)."""

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
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


def init_db(target: Engine | None = None) -> None:
    """Create the SQLite folder if needed, then any missing tables and columns."""
    from app import models  # noqa: F401 - registers the tables on Base.metadata

    target = target or engine
    url = target.url
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(target)
    add_missing_columns(target)


def add_missing_columns(target: Engine) -> list[str]:
    """Bring a database made by an older version of the code up to date.

    ``create_all`` creates missing tables but never alters existing ones, so a
    column added to a model later would make every insert fail. Missing
    nullable columns are added; a missing NOT NULL column cannot be filled for
    existing rows, so startup stops with a clear message instead.
    """
    inspector = inspect(target)
    quote = target.dialect.identifier_preparer.quote
    added = []
    with target.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable:
                    raise RuntimeError(
                        f"The database at {target.url} predates the required column "
                        f"{table.name}.{column.name}. Delete it (or point GEO_DATABASE_URL elsewhere) and restart."
                    )
                column_type = column.type.compile(dialect=target.dialect)
                conn.execute(text(f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(column.name)} {column_type}"))
                added.append(f"{table.name}.{column.name}")
    if added:
        logging.getLogger(__name__).info("Added columns to an older database: %s", ", ".join(added))
    return added


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with SessionLocal() as session:
        yield session


def get_session_factory() -> sessionmaker:
    """FastAPI dependency for work that outlives the request and needs its own sessions."""
    return SessionLocal

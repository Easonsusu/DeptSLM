"""Synchronous PostgreSQL engine and request-scoped session lifecycle."""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str) -> Engine:
    """Create a lazy SQLAlchemy engine without connecting or creating schema."""

    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db_session(request: Request) -> Generator[Session, None, None]:
    """Commit successful requests and roll back all failures."""

    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DatabaseSession = Annotated[Session, Depends(get_db_session)]

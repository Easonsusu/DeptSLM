"""Narrow PostgreSQL-only command boundary for Phase 8 feedback purge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.rag_feedback_services import PurgeResult, purge_feedback_batch


class FeedbackPurgeConfigurationError(RuntimeError):
    """A content-free purge configuration failure."""


@dataclass(frozen=True, slots=True)
class FeedbackPurgeSettings:
    """The only process setting required by the PostgreSQL purge command."""

    database_url: str

    @classmethod
    def from_environment(cls) -> FeedbackPurgeSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise FeedbackPurgeConfigurationError("DATABASE_URL is required for feedback purge.")
        try:
            parsed = make_url(database_url)
        except ArgumentError:
            raise FeedbackPurgeConfigurationError(
                "DATABASE_URL is invalid for feedback purge."
            ) from None
        if parsed.drivername != "postgresql+psycopg" or not parsed.database:
            raise FeedbackPurgeConfigurationError(
                "DATABASE_URL must use the postgresql+psycopg driver for feedback purge."
            )
        return cls(database_url=database_url)


def purge_rag_feedback(
    settings: FeedbackPurgeSettings,
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int,
    apply: bool,
) -> PurgeResult:
    """Authorize and process one deterministic PostgreSQL-only feedback batch."""

    if not actor_issuer.strip() or not actor_subject.strip():
        raise FeedbackPurgeConfigurationError("Actor issuer and subject must be non-empty.")
    try:
        engine = create_database_engine(settings.database_url)
    except SQLAlchemyError as error:
        raise FeedbackPurgeConfigurationError("Feedback purge database unavailable.") from error
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            return purge_feedback_batch(
                session,
                AuthenticatedPrincipal(actor_subject, actor_issuer),
                DepartmentRequestScope(DepartmentScope(department_id)),
                limit=limit,
                apply=apply,
            )
    except SQLAlchemyError as error:
        raise FeedbackPurgeConfigurationError("Feedback purge database unavailable.") from error
    finally:
        engine.dispose()

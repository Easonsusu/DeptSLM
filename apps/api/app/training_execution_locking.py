"""Transaction-scoped serialization for Phase 14 execution mutations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _advisory_key(serialization_id: UUID) -> tuple[int, int]:
    """Derive the reviewed two-int PostgreSQL key from one execution UUID."""

    raw = serialization_id.bytes
    return (
        int.from_bytes(raw[:4], "big", signed=True),
        int.from_bytes(raw[4:8], "big", signed=True),
    )


def acquire_training_execution_serialization(session: Session, serialization_id: UUID) -> None:
    """Block on the exact transaction-scoped execution advisory fence.

    Callers must invoke this before taking the corresponding TrainingJob row
    lock. PostgreSQL releases the transaction-scoped lock at commit or
    rollback; no manual unlock is valid or required.
    """

    first, second = _advisory_key(serialization_id)
    session.execute(select(func.pg_advisory_xact_lock(first, second)))


__all__ = ["acquire_training_execution_serialization"]

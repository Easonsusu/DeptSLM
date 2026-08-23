"""Retention/archive fences owned by active Phase 14.1 executions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TrainingExecution

ACTIVE_EXECUTION_STATUSES = ("queued", "running", "cancel_requested")


def has_active_training_execution(
    session: Session, department_id: UUID, training_job_id: UUID, *, lock: bool = False
) -> bool:
    query = select(TrainingExecution.id).where(
        TrainingExecution.department_id == department_id,
        TrainingExecution.training_job_id == training_job_id,
        TrainingExecution.status.in_(ACTIVE_EXECUTION_STATUSES),
    )
    if lock:
        query = query.with_for_update()
    return session.scalar(query) is not None

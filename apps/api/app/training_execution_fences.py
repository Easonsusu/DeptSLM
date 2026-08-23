"""Retention/archive fences owned by active Phase 14.1 executions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TrainingExecution, TrainingExecutionAttempt

ACTIVE_EXECUTION_STATUSES = ("queued", "running", "cancel_requested")
RETAINED_REAL_OUTPUT_STATUSES = ("succeeded",)


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


def has_training_execution_retention_fence(
    session: Session, department_id: UUID, training_job_id: UUID, *, lock: bool = False
) -> bool:
    """Return true while execution or retained real output owns Phase 11 bytes."""

    query = select(TrainingExecution.id).where(
        TrainingExecution.department_id == department_id,
        TrainingExecution.training_job_id == training_job_id,
        (
            TrainingExecution.status.in_(ACTIVE_EXECUTION_STATUSES)
            | (
                TrainingExecution.status.in_(RETAINED_REAL_OUTPUT_STATUSES)
                & select(TrainingExecutionAttempt.id)
                .where(
                    TrainingExecutionAttempt.execution_id == TrainingExecution.id,
                    TrainingExecutionAttempt.department_id == department_id,
                    TrainingExecutionAttempt.runtime_kind == "real",
                    TrainingExecutionAttempt.output_retained_at.is_not(None),
                    TrainingExecutionAttempt.output_purged_at.is_(None),
                )
                .exists()
            )
        ),
    )
    if lock:
        query = query.with_for_update()
    return session.scalar(query) is not None

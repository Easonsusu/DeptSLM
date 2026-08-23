"""PostgreSQL 16 coverage for the Phase 14.1 execution control plane."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker
from test_phase11_postgres import _succeeded_training_job

from alembic import command
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine
from app.models import TrainingExecution, TrainingExecutionAttempt, TrainingJob
from app.schemas import TrainingExecutionCreateRequest
from app.services import ServiceError
from app.training_execution_services import (
    cancel_training_execution,
    enqueue_training_execution,
    retry_training_execution,
)
from app.training_job_services import review_training_job

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped in CI")
    pytest.skip("PostgreSQL integration database is unavailable")


@pytest.fixture(scope="module")
def engine():
    value = create_database_engine(_database_url())
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    yield value
    value.dispose()


def test_phase14_1_migration_cycle_and_schema_contract(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0017_phase12_adapter_runtime_routing")
    command.upgrade(config, "0018_phase14_training_execution_control_plane")
    command.downgrade(config, "0017_phase12_adapter_runtime_routing")
    command.upgrade(config, "0018_phase14_training_execution_control_plane")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0018_phase14_training_execution_control_plane"
        )
    inspector = inspect(engine)
    execution_columns = {item["name"] for item in inspector.get_columns("training_executions")}
    attempt_columns = {
        item["name"] for item in inspector.get_columns("training_execution_attempts")
    }
    assert {
        "training_job_id",
        "training_job_publication_manifest",
        "dataset_build_id",
        "dataset_manifest_sha256",
        "execution_code_revision",
        "authority_fingerprint",
        "status",
        "current_attempt_id",
        "claim_token",
    }.issubset(execution_columns)
    assert {
        "execution_id",
        "attempt_number",
        "worker_id",
        "claim_token",
        "input_snapshot_fingerprint",
        "runtime_fingerprint",
        "result_classification",
    }.issubset(attempt_columns)
    assert any(
        index["name"] == "uq_training_execution_active_job_profile" and index["unique"]
        for index in inspector.get_indexes("training_executions")
    )
    checks = {item["name"] for item in inspector.get_check_constraints("training_executions")}
    assert {
        "ck_training_execution_status",
        "ck_training_execution_source_lifecycle",
        "ck_training_execution_lifecycle",
        "ck_training_execution_model_contract",
        "ck_training_execution_error_code",
    }.issubset(checks)
    attempt_checks = {
        item["name"] for item in inspector.get_check_constraints("training_execution_attempts")
    }
    assert "ck_training_execution_attempt_success_contract" in attempt_checks


def _approved_execution(
    engine, tmp_path: Path
) -> tuple[sessionmaker, UUID, str, str, TrainingExecution]:
    factory = sessionmaker(engine)
    department_id, issuer, subject, job_id = _succeeded_training_job(factory, tmp_path / "runtime")
    principal = AuthenticatedPrincipal(subject, issuer)
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        review_training_job(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            job.id,
            action="approve",
            expected_version=job.version,
        )
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        execution = enqueue_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            TrainingExecutionCreateRequest(
                training_job_id=job.id,
                expected_training_job_version=job.version,
            ),
            code_revision=job.code_revision,
        )
        execution_id = execution.id
    with factory() as session:
        row = session.get(TrainingExecution, execution_id)
        assert row is not None
        return factory, department_id, issuer, subject, row


def test_phase14_1_captures_immutable_authority_and_enforces_active_uniqueness(
    engine, tmp_path: Path
) -> None:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    assert execution.status == "queued"
    assert execution.training_job_publication_manifest
    assert execution.authority_fingerprint and len(execution.authority_fingerprint) == 64
    principal = AuthenticatedPrincipal(subject, issuer)
    with factory.begin() as session:
        job = session.get(TrainingJob, execution.training_job_id)
        assert job is not None
        with pytest.raises(ServiceError, match="Training execution conflict"):
            enqueue_training_execution(
                session,
                principal,
                DepartmentRequestScope(DepartmentScope(department_id)),
                TrainingExecutionCreateRequest(
                    training_job_id=job.id,
                    expected_training_job_version=job.version,
                ),
                code_revision=job.code_revision,
            )


def test_phase14_1_cancel_then_retry_creates_a_new_attempt_surface(engine, tmp_path: Path) -> None:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    principal = AuthenticatedPrincipal(subject, issuer)
    execution_id = execution.id
    with factory.begin() as session:
        cancelled = cancel_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            execution_id,
            expected_version=execution.version,
        )
        assert cancelled.status == "cancelled"
        cancelled_version = cancelled.version
    with factory.begin() as session:
        retried = retry_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            execution_id,
            expected_version=cancelled_version,
        )
        assert retried.status == "queued"
        assert retried.current_attempt_number == 2
    with factory() as session:
        attempts = session.scalars(
            select(TrainingExecutionAttempt).where(
                TrainingExecutionAttempt.execution_id == execution_id
            )
        ).all()
        assert attempts == []

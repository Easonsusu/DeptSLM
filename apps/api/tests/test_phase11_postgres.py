"""PostgreSQL 16 coverage for Phase 11 metadata-only job persistence."""

from __future__ import annotations

import os

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from app.database import create_database_engine
from app.models import Base

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
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


def test_phase11_migration_cycle_reaches_exact_head(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0008_phase10_sft_dataset_builder")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0009_phase11_training_jobs"
        )


def test_phase11_metadata_schema_is_content_free_and_registered(engine) -> None:
    inspector = inspect(engine)
    job_columns = {column["name"] for column in inspector.get_columns("training_jobs")}
    attempt_columns = {column["name"] for column in inspector.get_columns("training_job_attempts")}
    operation_columns = {
        column["name"] for column in inspector.get_columns("training_job_artifact_operations")
    }
    forbidden = {
        "instruction",
        "response",
        "prompt",
        "answer",
        "text",
        "path",
        "filename",
        "training_yaml",
        "dataset_info",
        "model_output",
    }
    assert {
        "dataset_build_id",
        "profile_id",
        "base_model_revision",
        "publication_manifest",
        "result_manifest_sha256",
    }.issubset(job_columns)
    assert {"publication_attempt_id", "ownership_manifest", "cleanup_confirmed_at"}.issubset(
        attempt_columns
    )
    assert {"operation_type", "limit_value", "status"}.issubset(operation_columns)
    assert forbidden.isdisjoint(job_columns | attempt_columns | operation_columns)
    assert set(Base.metadata.tables).issuperset(
        {
            "training_jobs",
            "training_job_attempts",
            "training_job_artifact_operations",
            "training_job_artifact_operation_items",
        }
    )


def test_phase11_schema_has_scoped_lifecycle_and_model_contract_checks(engine) -> None:
    checks = {check["name"] for check in inspect(engine).get_check_constraints("training_jobs")}
    assert {
        "ck_training_job_status",
        "ck_training_job_review_status",
        "ck_training_job_model_contract",
        "ck_training_job_artifact_contracts",
        "ck_training_job_dataset_contracts",
        "ck_training_job_queued_lifecycle",
        "ck_training_job_running_lifecycle",
        "ck_training_job_succeeded_lifecycle",
    }.issubset(checks)
    foreign_keys = inspect(engine).get_foreign_keys("training_jobs")
    assert any(
        item["constrained_columns"] == ["dataset_build_id", "department_id"]
        and item["referred_table"] == "sft_dataset_builds"
        for item in foreign_keys
    )

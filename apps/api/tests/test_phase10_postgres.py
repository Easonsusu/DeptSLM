"""PostgreSQL 16 coverage for Phase 10 SFT metadata-only persistence."""

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


def test_phase10_migration_cycle_reaches_exact_head(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0007_phase9_evaluation_runner")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0008_phase10_sft_dataset_builder"
        )


def test_phase10_metadata_schema_is_content_free_and_orm_synchronized(engine) -> None:
    inspector = inspect(engine)
    source_columns = {column["name"] for column in inspector.get_columns("sft_source_bundles")}
    attempt_columns = {
        column["name"] for column in inspector.get_columns("sft_source_import_attempts")
    }
    build_columns = {column["name"] for column in inspector.get_columns("sft_dataset_builds")}
    item_columns = {
        column["name"]
        for column in inspector.get_columns("sft_artifact_reconciliation_operation_items")
    }
    forbidden = {"instruction", "response", "prompt", "answer", "text", "path", "filename"}
    assert "authority_snapshot_sha256" in source_columns
    assert "authority_snapshot_sha256" in attempt_columns
    assert {"publication_manifest", "artifact_cleanup_confirmed_at"}.issubset(build_columns)
    assert {"attempt_id", "ownership_manifest"}.issubset(item_columns)
    assert forbidden.isdisjoint(source_columns | attempt_columns | build_columns)
    assert set(Base.metadata.tables).issuperset(
        {
            "sft_source_bundles",
            "sft_source_import_attempts",
            "sft_dataset_builds",
            "sft_artifact_reconciliation_operations",
            "sft_artifact_reconciliation_operation_items",
        }
    )


def test_phase10_operation_item_constraints_are_exact(engine) -> None:
    inspector = inspect(engine)
    checks = {
        check["name"]
        for check in inspector.get_check_constraints("sft_artifact_reconciliation_operation_items")
    }
    assert {
        "ck_sft_reconciliation_item_lifecycle",
        "ck_sft_reconciliation_item_reason",
        "ck_sft_reconciliation_item_resource_type",
    }.issubset(checks)

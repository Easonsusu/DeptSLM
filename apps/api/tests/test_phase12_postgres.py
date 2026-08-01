"""PostgreSQL 16 schema coverage for Phase 12.1B source intake."""

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


def test_phase12_migration_upgrade_downgrade_upgrade_reaches_head(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0009_phase11_training_jobs")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0010_phase12_adapter_sources"
        )


def test_phase12_tables_are_content_free_and_orm_synchronized(engine) -> None:
    inspector = inspect(engine)
    source_columns = {column["name"] for column in inspector.get_columns("adapter_import_sources")}
    attempt_columns = {
        column["name"] for column in inspector.get_columns("adapter_import_attempts")
    }
    forbidden = {
        "adapter_config",
        "adapter_model",
        "tensor_values",
        "tensor_bytes",
        "path",
        "filename",
        "exception_text",
    }
    assert forbidden.isdisjoint(source_columns | attempt_columns)
    assert {
        "id",
        "department_id",
        "authoritative_attempt_id",
        "adapter_config_sha256",
        "adapter_model_sha256",
        "intake_manifest_sha256",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
    }.issubset(source_columns)
    assert {
        "source_bundle_id",
        "attempt_number",
        "publication_attempt_id",
        "ownership_manifest",
        "cleanup_confirmed_at",
    }.issubset(attempt_columns)
    assert {"adapter_import_sources", "adapter_import_attempts"}.issubset(Base.metadata.tables)


def test_phase12_constraints_and_active_index_are_present(engine) -> None:
    inspector = inspect(engine)
    source_checks = {
        item["name"] for item in inspector.get_check_constraints("adapter_import_sources")
    }
    attempt_checks = {
        item["name"] for item in inspector.get_check_constraints("adapter_import_attempts")
    }
    assert {
        "ck_adapter_import_source_status",
        "ck_adapter_import_source_contract",
        "ck_adapter_import_source_lifecycle",
        "ck_adapter_import_source_error_code",
    }.issubset(source_checks)
    assert {
        "ck_adapter_import_attempt_status",
        "ck_adapter_import_attempt_lifecycle",
        "ck_adapter_import_attempt_error_code",
    }.issubset(attempt_checks)
    indexes = {index["name"]: index for index in inspector.get_indexes("adapter_import_attempts")}
    assert indexes["uq_adapter_import_attempt_active"]["unique"] is True
    predicate = (
        indexes["uq_adapter_import_attempt_active"]
        .get("dialect_options", {})
        .get("postgresql_where", "")
    )
    assert "registered" in str(predicate)
    assert "published" in str(predicate)

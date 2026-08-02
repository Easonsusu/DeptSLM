"""PostgreSQL 16 schema checks for the Phase 12.1C registry authority."""

from __future__ import annotations

import os

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from app.database import create_database_engine

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped")
    pytest.skip("PostgreSQL integration database is unavailable")


@pytest.fixture(scope="module")
def engine():
    value = create_database_engine(_database_url())
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


def test_registry_schema_has_exact_snapshots_and_restrictive_fks(engine) -> None:
    inspector = inspect(engine)
    adapter_columns = {item["name"] for item in inspector.get_columns("adapters")}
    assert {
        "source_attempt_version",
        "training_job_attempt_version",
        "dataset_attempt_version",
        "training_job_manifest_byte_size",
        "training_job_config_sha256",
        "training_job_dataset_info_sha256",
        "training_job_train_sha256",
        "training_job_validation_sha256",
    }.issubset(adapter_columns)
    adapter_constraints = {item["name"] for item in inspector.get_check_constraints("adapters")}
    assert {
        "ck_adapter_upstream_contracts",
        "ck_adapter_exact_sizes",
        "ck_adapter_source_hashes",
        "ck_adapter_registry_hashes",
        "ck_adapter_lifecycle",
    }.issubset(adapter_constraints)
    foreign_keys = {item["name"]: item for item in inspector.get_foreign_keys("adapters")}
    assert {
        "fk_adapter_source_attempt_exact",
        "fk_adapter_training_attempt_exact",
        "fk_adapter_dataset_attempt_exact",
    }.issubset(foreign_keys)
    assert all(
        item.get("options", {}).get("ondelete") == "RESTRICT" for item in foreign_keys.values()
    )
    assert inspector.get_unique_constraints("adapter_import_attempts")
    assert inspector.get_unique_constraints("training_job_attempts")
    assert inspector.get_unique_constraints("sft_dataset_build_attempts")


def test_registry_tables_do_not_store_adapter_bytes_or_paths(engine) -> None:
    with engine.connect() as connection:
        names = connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ("
                "'adapters','adapter_registry_attempts','adapter_upstream_dependencies')"
            )
        ).all()
    forbidden = {
        "adapter_bytes",
        "tensor_values",
        "tensor_bytes",
        "path",
        "filename",
        "config_json",
        "model_weights",
    }
    assert forbidden.isdisjoint({column for _table, column in names})

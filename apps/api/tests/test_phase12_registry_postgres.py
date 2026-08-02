"""PostgreSQL 16 schema checks for the Phase 12.1C registry authority."""

from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

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


def test_existing_0010_committed_source_backfills_manifest_size(engine) -> None:
    """0011 must derive the exact byte size from a real 0010 authority row."""

    config = Config("alembic.ini")
    command.downgrade(config, "0010_phase12_adapter_sources")
    department_id = uuid4()
    user_id = uuid4()
    source_id = uuid4()
    attempt_id = uuid4()
    publication_attempt_id = uuid4()
    code_revision = "a" * 40
    manifest = {
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "department_id": str(department_id),
        "source_bundle_id": str(source_id),
        "import_attempt_id": str(attempt_id),
        "publication_attempt_id": str(publication_attempt_id),
        "attempt_number": 1,
        "imported_by_user_id": str(user_id),
        "code_revision": code_revision,
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": "F16",
        "tensor_count": 392,
        "tensor_element_count": 10092544,
        "tensor_payload_byte_size": 20185088,
        "files": {
            "adapter_config.json": {"sha256": "a" * 64, "byte_size": 1},
            "adapter_model.safetensors": {"sha256": "b" * 64, "byte_size": 2},
        },
    }
    raw_manifest = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_identities "
                "(id, issuer, subject, status) VALUES (:id, :issuer, :subject, 'active')"
            ),
            {"id": user_id, "issuer": "https://migration.invalid", "subject": str(user_id)},
        )
        connection.execute(
            text(
                "INSERT INTO departments "
                "(id, slug, display_name, status, version) "
                "VALUES (:id, :slug, 'Migration test', 'active', 1)"
            ),
            {"id": department_id, "slug": f"migration-{department_id.hex[:12]}"},
        )
        connection.execute(
            text(
                "INSERT INTO adapter_import_sources "
                "(id, department_id, imported_by_user_id, status, source_contract_version, "
                "intake_contract_version, config_contract_version, tensor_contract_version, "
                "base_model_id, base_model_revision, base_model_license, peft_version, "
                "safetensors_format, code_revision, version) VALUES "
                "(:id, :department_id, :user_id, 'staging', "
                "'phase12-adapter-source-v1', 'phase12-adapter-intake-v1', "
                "'phase12-adapter-config-v1', 'phase12-adapter-tensors-v1', "
                "'Qwen/Qwen3-0.6B', 'c1899de289a04d12100db370d81485cdf75e47ca', "
                "'Apache-2.0', '0.18.1', '0.7.0', :code_revision, 1)"
            ),
            {
                "id": source_id,
                "department_id": department_id,
                "user_id": user_id,
                "code_revision": code_revision,
            },
        )
        connection.execute(
            text(
                "INSERT INTO adapter_import_attempts "
                "(id, department_id, source_bundle_id, attempt_number, publication_attempt_id, "
                "status, code_revision, version) VALUES "
                "(:id, :department_id, :source_id, 1, :publication_attempt_id, "
                "'registered', :code_revision, 1)"
            ),
            {
                "id": attempt_id,
                "department_id": department_id,
                "source_id": source_id,
                "publication_attempt_id": publication_attempt_id,
                "code_revision": code_revision,
            },
        )
        connection.execute(
            text(
                "UPDATE adapter_import_attempts SET status = 'committed', "
                "ownership_manifest = CAST(:manifest AS JSON), validated_at = now(), "
                "staged_at = now(), published_at = now(), committed_at = now(), "
                "finished_at = now(), version = 5 WHERE id = :id"
            ),
            {"id": attempt_id, "manifest": json.dumps(manifest)},
        )
        connection.execute(
            text(
                "UPDATE adapter_import_sources SET status = 'committed', "
                "authoritative_attempt_id = :attempt_id, adapter_config_sha256 = :config_sha, "
                "adapter_config_byte_size = 1, adapter_model_sha256 = :model_sha, "
                "adapter_model_byte_size = 2, intake_manifest_sha256 = :manifest_sha, "
                "tensor_dtype = 'F16', tensor_count = 392, tensor_element_count = 10092544, "
                "tensor_payload_byte_size = 20185088, committed_at = now(), version = 2 "
                "WHERE id = :id"
            ),
            {
                "id": source_id,
                "attempt_id": attempt_id,
                "config_sha": "a" * 64,
                "model_sha": "b" * 64,
                "manifest_sha": hashlib.sha256(raw_manifest).hexdigest(),
            },
        )
    try:
        command.upgrade(config, "0011_phase12_adapter_registry")
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT intake_manifest_byte_size FROM adapter_import_sources WHERE id = :id"),
                {"id": source_id},
            ) == len(raw_manifest)
    finally:
        command.upgrade(config, "head")

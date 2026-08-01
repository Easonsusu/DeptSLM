"""PostgreSQL 16 schema coverage for Phase 12.1B source intake."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from alembic import command
from app.adapter_contract import (
    EXPECTED_TENSOR_NAMES,
    EXPECTED_TENSOR_SHAPES,
    canonical_adapter_config_bytes,
)
from app.adapter_source_services import (
    AdapterSourceImportConfigurationError,
    AdapterSourceImportSettings,
    import_adapter_source,
)
from app.database import create_database_engine
from app.models import (
    AdapterImportAttempt,
    AdapterImportSource,
    Base,
    Department,
    Membership,
    UserIdentity,
)

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


def _seed_actor(
    engine,
    *,
    role: str = "department_admin",
    identity_status: str = "active",
    membership_status: str = "active",
):
    issuer = "https://phase12.issuer.invalid"
    subject = f"subject-{uuid4()}"
    with Session(engine) as session:
        identity = UserIdentity(issuer=issuer, subject=subject, status=identity_status)
        department = Department(slug=f"phase12-{uuid4().hex[:12]}", display_name="Phase 12")
        session.add_all((identity, department))
        session.flush()
        session.add(
            Membership(
                user_id=identity.id,
                department_id=department.id,
                role=role,
                status=membership_status,
                created_by_user_id=identity.id,
            )
        )
        session.commit()
        return issuer, subject, department.id


def _adapter_files(root: Path) -> tuple[Path, Path]:
    config = root / "adapter_config.json"
    model = root / "adapter_model.safetensors"
    config.write_bytes(canonical_adapter_config_bytes())
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    offset = 0
    for name in EXPECTED_TENSOR_NAMES:
        shape = EXPECTED_TENSOR_SHAPES[name]
        size = shape[0] * shape[1] * 2
        header[name] = {
            "dtype": "F16",
            "shape": list(shape),
            "data_offsets": [offset, offset + size],
        }
        offset += size
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    with model.open("wb") as handle:
        handle.write(len(raw).to_bytes(8, "little"))
        handle.write(raw)
        handle.truncate(8 + len(raw) + offset)
    os.chmod(config, 0o600)
    os.chmod(model, 0o600)
    return config, model


@pytest.mark.parametrize("role", ["system_admin", "department_admin"])
def test_same_department_admin_can_apply_source_import(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    issuer, subject, department_id = _seed_actor(engine, role=role)
    (tmp_path / "adapters").mkdir(mode=0o700)
    config, model = _adapter_files(tmp_path)
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    settings = AdapterSourceImportSettings(
        database_url=_database_url(), data_dir=tmp_path, code_revision="a" * 40
    )
    result = import_adapter_source(
        settings,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        adapter_config=config,
        adapter_model=model,
        apply=True,
    )
    assert result.status == "committed"
    with Session(engine) as session:
        source = session.get(AdapterImportSource, result.source_bundle_id)
        attempt = session.get(AdapterImportAttempt, result.import_attempt_id)
        assert source is not None and attempt is not None
        assert source.status == "committed"
        assert source.authoritative_attempt_id == attempt.id
        assert attempt.status == "committed"
        assert attempt.department_id == department_id
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'adapter.source.import' "
                    "AND resource_id = :resource_id"
                ),
                {"resource_id": str(source.id)},
            )
            == 1
        )


@pytest.mark.parametrize(
    ("role", "identity_status", "membership_status"),
    [
        ("instructor", "active", "active"),
        ("student", "active", "active"),
        ("viewer", "active", "active"),
        ("department_admin", "suspended", "active"),
        ("department_admin", "revoked", "active"),
        ("department_admin", "active", "suspended"),
    ],
)
def test_non_admin_or_inactive_actor_cannot_apply_source_import(
    engine,
    tmp_path: Path,
    role: str,
    identity_status: str,
    membership_status: str,
) -> None:
    issuer, subject, department_id = _seed_actor(
        engine,
        role=role,
        identity_status=identity_status,
        membership_status=membership_status,
    )
    (tmp_path / "adapters").mkdir(mode=0o700)
    config = tmp_path / "adapter_config.json"
    model = tmp_path / "adapter_model.safetensors"
    config.write_bytes(b"x")
    model.write_bytes(b"y")
    os.chmod(config, 0o600)
    os.chmod(model, 0o600)
    settings = AdapterSourceImportSettings(
        database_url=_database_url(), data_dir=tmp_path, code_revision="a" * 40
    )
    with pytest.raises(AdapterSourceImportConfigurationError) as error:
        import_adapter_source(
            settings,
            department_id=department_id,
            actor_issuer=issuer,
            actor_subject=subject,
            adapter_config=config,
            adapter_model=model,
            apply=True,
        )
    assert error.value.code == "requester_unauthorized"

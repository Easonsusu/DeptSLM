"""PostgreSQL 16 coverage for Phase 10 SFT metadata-only persistence."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.database import create_database_engine
from app.models import (
    Base,
    Department,
    SftDatasetBuild,
    SftDatasetBuildAttempt,
    SftSourceBundle,
    SftSourceImportAttempt,
    UserIdentity,
)
from app.sft_maintenance import _reconciliation_candidates
from app.sft_queue import claim_next

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
    build_attempt_columns = {
        column["name"] for column in inspector.get_columns("sft_dataset_build_attempts")
    }
    item_columns = {
        column["name"]
        for column in inspector.get_columns("sft_artifact_reconciliation_operation_items")
    }
    forbidden = {"instruction", "response", "prompt", "answer", "text", "path", "filename"}
    assert "authority_snapshot_sha256" in source_columns
    assert "authority_snapshot_sha256" in attempt_columns
    assert {"publication_manifest", "artifact_cleanup_confirmed_at"}.issubset(build_columns)
    assert {
        "publication_attempt_id",
        "ownership_manifest",
        "cleanup_confirmed_at",
    }.issubset(build_attempt_columns)
    assert {"attempt_id", "ownership_manifest"}.issubset(item_columns)
    assert forbidden.isdisjoint(source_columns | attempt_columns | build_columns)
    assert set(Base.metadata.tables).issuperset(
        {
            "sft_source_bundles",
            "sft_source_import_attempts",
            "sft_dataset_builds",
            "sft_dataset_build_attempts",
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
    unique_constraints = inspector.get_unique_constraints(
        "sft_artifact_reconciliation_operation_items"
    )
    assert any(
        constraint["column_names"] == ["operation_id", "resource_type", "resource_id", "attempt_id"]
        for constraint in unique_constraints
    )


def _phase10_source(session: Session) -> tuple[Department, UserIdentity, SftSourceBundle]:
    department = Department(slug=f"phase10-{uuid4().hex}", display_name="Phase 10", status="active")
    identity = UserIdentity(issuer="https://phase10.invalid", subject=uuid4().hex, status="active")
    session.add_all((department, identity))
    session.flush()
    source = SftSourceBundle(
        department_id=department.id,
        imported_by_user_id=identity.id,
        status="active",
        artifact_contract_version="phase10-sft-source-v1",
        normalization_version="phase10-sft-normalization-v1",
        example_contract_version="phase10-sft-example-v1",
        example_count=2,
        group_count=2,
        source_reference_count=2,
        manifest_sha256="a" * 64,
        examples_sha256="b" * 64,
        authority_snapshot_sha256="c" * 64,
        examples_byte_size=2,
    )
    session.add(source)
    session.flush()
    return department, identity, source


def test_phase10_failed_import_never_implies_cleanup_confirmation(engine) -> None:
    with Session(engine) as session:
        department, identity, source = _phase10_source(session)
        attempt = SftSourceImportAttempt(
            department_id=department.id,
            source_bundle_id=source.id,
            import_attempt_id=uuid4(),
            stage_id=uuid4(),
            imported_by_user_id=identity.id,
            status="failed",
            failed_at=datetime.now(UTC),
            artifact_manifest={"content_free": True},
        )
        session.add(attempt)
        session.commit()
        assert attempt.cleanup_confirmed_at is None


def test_phase10_reclaimed_build_attempt_is_durable_and_exact(engine) -> None:
    now = datetime.now(UTC)
    with Session(engine) as session:
        department, identity, source = _phase10_source(session)
        build = SftDatasetBuild(
            department_id=department.id,
            source_bundle_id=source.id,
            requested_by_user_id=identity.id,
            status="queued",
            review_status="not_ready",
            attempt_number=1,
            code_revision="a" * 40,
            artifact_contract_version="phase10-sft-dataset-v1",
            example_contract_version="phase10-sft-example-v1",
            normalization_version="phase10-sft-normalization-v1",
            split_version="phase10-sft-group-split-v1",
            validation_ratio="0.10",
            source_example_count=2,
            source_group_count=2,
            source_reference_count=2,
        )
        session.add(build)
        session.flush()
        stale = SftDatasetBuildAttempt(
            department_id=department.id,
            build_id=build.id,
            attempt_number=1,
            publication_attempt_id=uuid4(),
            code_revision=build.code_revision,
            status="reclaimed",
            ownership_manifest={"content_free": True},
            claimed_at=now,
            finished_at=now,
        )
        active = SftDatasetBuildAttempt(
            department_id=department.id,
            build_id=build.id,
            attempt_number=2,
            publication_attempt_id=uuid4(),
            code_revision=build.code_revision,
            status="running",
            claimed_at=now,
        )
        session.add_all((stale, active))
        session.commit()
        rows = session.scalars(
            select(SftDatasetBuildAttempt)
            .where(SftDatasetBuildAttempt.build_id == build.id)
            .order_by(SftDatasetBuildAttempt.attempt_number)
        ).all()
        assert [(row.attempt_number, row.status) for row in rows] == [
            (1, "reclaimed"),
            (2, "running"),
        ]
        assert rows[0].ownership_manifest == {"content_free": True}
        assert rows[0].cleanup_confirmed_at is None


def test_phase10_claim_reclaim_keeps_old_attempt_discoverable(engine) -> None:
    factory = sessionmaker(engine)
    code_revision = "d" * 40
    with Session(engine) as session:
        department, identity, source = _phase10_source(session)
        build = SftDatasetBuild(
            department_id=department.id,
            source_bundle_id=source.id,
            requested_by_user_id=identity.id,
            status="queued",
            review_status="not_ready",
            attempt_number=1,
            code_revision=code_revision,
            artifact_contract_version="phase10-sft-dataset-v1",
            example_contract_version="phase10-sft-example-v1",
            normalization_version="phase10-sft-normalization-v1",
            split_version="phase10-sft-group-split-v1",
            validation_ratio="0.10",
            source_example_count=2,
            source_group_count=2,
            source_reference_count=2,
        )
        session.add(build)
        session.commit()
        build_id, department_id = build.id, department.id

    first = claim_next(factory, uuid4(), lease_seconds=1, code_revision=code_revision)
    assert first is not None
    with Session(engine) as session:
        build = session.get(SftDatasetBuild, build_id)
        assert build is not None
        build.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    second = claim_next(factory, uuid4(), lease_seconds=1, code_revision=code_revision)
    assert second is not None
    assert second.publication_attempt_id != first.publication_attempt_id
    assert second.stale_publication_attempt_id == first.publication_attempt_id
    with Session(engine) as session:
        attempts = session.scalars(
            select(SftDatasetBuildAttempt)
            .where(
                SftDatasetBuildAttempt.department_id == department_id,
                SftDatasetBuildAttempt.build_id == build_id,
            )
            .order_by(SftDatasetBuildAttempt.attempt_number)
        ).all()
        assert [attempt.status for attempt in attempts] == ["reclaimed", "running"]
        assert attempts[0].publication_attempt_id == first.publication_attempt_id
        assert attempts[1].publication_attempt_id == second.publication_attempt_id


def test_phase10_claim_time_terminal_failure_also_terminalizes_attempt(engine) -> None:
    factory = sessionmaker(engine)
    code_revision = "e" * 40
    with Session(engine) as session:
        department, identity, source = _phase10_source(session)
        department.status = "archived"
        build = SftDatasetBuild(
            department_id=department.id,
            source_bundle_id=source.id,
            requested_by_user_id=identity.id,
            status="queued",
            review_status="not_ready",
            attempt_number=1,
            code_revision=code_revision,
            artifact_contract_version="phase10-sft-dataset-v1",
            example_contract_version="phase10-sft-example-v1",
            normalization_version="phase10-sft-normalization-v1",
            split_version="phase10-sft-group-split-v1",
            validation_ratio="0.10",
            source_example_count=2,
            source_group_count=2,
            source_reference_count=2,
        )
        session.add(build)
        session.commit()
        build_id = build.id

    assert claim_next(factory, uuid4(), lease_seconds=1, code_revision=code_revision) is None
    with Session(engine) as session:
        build = session.get(SftDatasetBuild, build_id)
        assert build is not None
        attempts = session.scalars(
            select(SftDatasetBuildAttempt)
            .where(SftDatasetBuildAttempt.build_id == build_id)
            .order_by(SftDatasetBuildAttempt.attempt_number)
        ).all()
        assert build.status == "failed"
        assert build.error_code == "department_unavailable"
        assert [(attempt.status, attempt.finished_at is not None) for attempt in attempts] == [
            ("failed", True)
        ]


def test_phase10_reconciliation_registers_every_possible_surface() -> None:
    attempt = SimpleNamespace(
        source_bundle_id=uuid4(),
        import_attempt_id=uuid4(),
        artifact_manifest={"manifest": "content-free"},
    )
    build_attempt = SimpleNamespace(
        build_id=uuid4(),
        publication_attempt_id=uuid4(),
        ownership_manifest={"manifest": "content-free"},
    )
    candidates = _reconciliation_candidates([attempt], [build_attempt])
    assert [candidate.resource_type for candidate in candidates] == [
        "source_stage",
        "source_final",
        "dataset_stage",
        "dataset_final",
    ]

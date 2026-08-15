"""PostgreSQL 16 coverage for Phase 10 SFT metadata-only persistence."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.database import create_database_engine
from app.models import (
    Base,
    Department,
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
    Membership,
    SftDatasetBuild,
    SftDatasetBuildAttempt,
    SftSourceBundle,
    SftSourceImportAttempt,
    UserIdentity,
)
from app.sft_domain import canonical_json_bytes
from app.sft_maintenance import _reconciliation_candidates
from app.sft_queue import claim_next
from app.sft_services import SftImportConfigurationError, SftImportSettings, import_sft_source
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    VECTOR_SCHEMA_VERSION,
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


def test_phase10_migration_cycle_reaches_exact_head(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0007_phase9_evaluation_runner")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0016_phase12_adapter_governance"
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


def _large_authority_source(session: Session, *, references: int = 1025) -> tuple:
    """Persist one real, authority-complete source set for import coverage."""

    department = Department(
        slug=f"phase10-import-{uuid4().hex}", display_name="Phase 10 import", status="active"
    )
    identity = UserIdentity(
        issuer="https://phase10-import.invalid", subject=uuid4().hex, status="active"
    )
    session.add_all((department, identity))
    session.flush()
    session.add(
        Membership(
            user_id=identity.id,
            department_id=department.id,
            role="department_admin",
            status="active",
            created_by_user_id=identity.id,
        )
    )
    payload = b"x" * references
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=identity.id,
        original_filename="authority.txt",
        media_type="text/plain",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    session.add(document)
    session.flush()
    now = datetime.now(UTC)
    extraction = DocumentExtraction(
        department_id=department.id,
        document_id=document.id,
        requested_by_user_id=identity.id,
        status="succeeded",
        pipeline_version="phase5-extraction-v1",
        parser_name="python-utf8",
        parser_version="3.12",
        normalization_version="phase5-normalization-v1",
        chunking_version="phase5-character-chunker-v1",
        source_sha256=document.sha256,
        source_byte_size=document.byte_size,
        normalized_sha256="a" * 64,
        normalized_byte_size=document.byte_size,
        output_byte_size=document.byte_size,
        chunk_count=references,
        worker_id=uuid4(),
        claim_token=uuid4(),
        claimed_at=now,
        started_at=now,
        finished_at=now,
    )
    session.add(extraction)
    session.flush()
    indexing = DocumentVectorIndexing(
        department_id=department.id,
        document_id=document.id,
        extraction_id=extraction.id,
        requested_by_user_id=identity.id,
        status="succeeded",
        embedding_pipeline_version=EMBEDDING_PIPELINE_VERSION,
        embedding_model_id=EMBEDDING_MODEL_ID,
        embedding_model_revision=EMBEDDING_MODEL_REVISION,
        embedding_dimension=EMBEDDING_DIMENSION,
        distance=EMBEDDING_DISTANCE,
        vector_schema_version=VECTOR_SCHEMA_VERSION,
        qdrant_collection=QDRANT_COLLECTION,
        expected_chunk_count=references,
        point_count=references,
        worker_id=uuid4(),
        claim_token=uuid4(),
        vector_attempt_id=uuid4(),
        claimed_at=now,
        started_at=now,
        finished_at=now,
    )
    session.add(indexing)
    session.flush()
    chunks = tuple(
        DocumentChunk(
            department_id=department.id,
            document_id=document.id,
            extraction_id=extraction.id,
            ordinal=ordinal,
            char_start=ordinal,
            char_end=ordinal + 1,
            byte_size=1,
            content_sha256=hashlib.sha256(f"{ordinal}".encode("ascii")).hexdigest(),
            provenance_kind="line",
            line_start=ordinal + 1,
            line_end=ordinal + 1,
        )
        for ordinal in range(references)
    )
    session.add_all(chunks)
    session.commit()
    return department.id, identity.issuer, identity.subject, tuple(chunk.id for chunk in chunks)


def _write_large_external_source(
    root: Path, department_id, chunk_ids: tuple
) -> tuple[Path, object]:
    """Write a real 1,025-reference source bundle outside runtime storage."""

    root.mkdir(mode=0o700)
    source_bundle_id = uuid4()
    import_attempt_id = uuid4()
    stage_id = uuid4()
    examples = []
    for index in range(0, len(chunk_ids), 8):
        examples.append(
            {
                "example_id": str(uuid4()),
                "group_id": str(uuid4()),
                "instruction": f"Question {index // 8}",
                "response": f"Answer {index // 8}",
                "source_chunk_ids": [str(value) for value in chunk_ids[index : index + 8]],
            }
        )
    examples_raw = b"".join(canonical_json_bytes(value) + b"\n" for value in examples)
    manifest = {
        "artifact_contract_version": "phase10-sft-source-v1",
        "department_id": str(department_id),
        "source_bundle_id": str(source_bundle_id),
        "import_attempt_id": str(import_attempt_id),
        "stage_id": str(stage_id),
        "normalization_version": "phase10-sft-normalization-v1",
        "example_contract_version": "phase10-sft-example-v1",
        "example_count": len(examples),
        "group_count": len(examples),
        "source_reference_count": len(chunk_ids),
        "files": {
            "examples.jsonl": {
                "sha256": hashlib.sha256(examples_raw).hexdigest(),
                "byte_size": len(examples_raw),
            }
        },
    }
    manifest_path = root / "manifest.json"
    examples_path = root / "examples.jsonl"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    examples_path.write_bytes(examples_raw)
    manifest_path.chmod(0o600)
    examples_path.chmod(0o600)
    return root, source_bundle_id


def test_large_source_import_separates_lock_free_capture_and_final_locking(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real 1,025-reference import never scans authority under auth locks."""

    with Session(engine) as session:
        department_id, actor_issuer, actor_subject, chunk_ids = _large_authority_source(session)
    external, source_bundle_id = _write_large_external_source(
        tmp_path / "external-source", department_id, chunk_ids
    )
    runtime = tmp_path / "runtime"
    (runtime / "training_datasets").mkdir(parents=True, mode=0o700)
    settings = SftImportSettings(database_url=_database_url(), data_dir=runtime)
    statements: list[tuple[str, object]] = []
    commits: list[int] = []
    created_engines = []

    from app import database

    create_engine = database.create_database_engine

    def tracked_engine(url: str):
        value = create_engine(url)
        event.listen(
            value,
            "before_cursor_execute",
            lambda _connection, _cursor, statement, parameters, _context, _many: statements.append(
                (statement, parameters)
            ),
        )
        event.listen(value, "commit", lambda _connection: commits.append(len(statements)))
        created_engines.append(value)
        return value

    monkeypatch.setattr(database, "create_database_engine", tracked_engine)
    dry_run = import_sft_source(
        settings,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        source_directory=external,
        apply=False,
    )
    assert dry_run.applied is False
    with Session(engine) as session:
        assert session.get(SftSourceBundle, source_bundle_id) is None
        assert (
            session.execute(
                select(SftSourceImportAttempt).where(
                    SftSourceImportAttempt.source_bundle_id == source_bundle_id
                )
            ).scalar_one_or_none()
            is None
        )
    dry_authority = [
        (statement, parameters)
        for statement, parameters in statements
        if "document_chunks" in statement and "SELECT" in statement
    ]

    def chunk_parameter_count(parameters: object) -> int:
        """Count only selector UUIDs; authority queries also bind fixed scope values."""

        assert isinstance(parameters, dict)
        return sum(value in chunk_ids for value in parameters.values())

    assert [chunk_parameter_count(parameters) for _statement, parameters in dry_authority] == [
        512,
        512,
        1,
    ]
    assert all("FOR UPDATE" not in statement for statement, _parameters in dry_authority)
    assert commits

    statements.clear()
    commits.clear()
    applied = import_sft_source(
        settings,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        source_directory=external,
        apply=True,
    )
    assert applied.applied is True
    authority_queries = [
        (statement, parameters)
        for statement, parameters in statements
        if "document_chunks" in statement and "SELECT" in statement
    ]
    assert [chunk_parameter_count(parameters) for _statement, parameters in authority_queries] == [
        512,
        512,
        1,
        512,
        512,
        1,
    ]
    assert all("FOR UPDATE" not in statement for statement, _parameters in authority_queries[:3])
    assert all("FOR UPDATE" in statement for statement, _parameters in authority_queries[3:])
    first_authority = statements.index(authority_queries[0])
    assert any(commit_index < first_authority for commit_index in commits)
    registration_index = next(
        index
        for index, (statement, _parameters) in enumerate(statements)
        if "INSERT INTO sft_source_import_attempts" in statement
    )
    assert registration_index > statements.index(authority_queries[2])
    assert all(
        chunk_parameter_count(parameters) <= 512 for _statement, parameters in authority_queries
    )
    with Session(engine) as session:
        source = session.get(SftSourceBundle, source_bundle_id)
        attempt = session.execute(
            select(SftSourceImportAttempt).where(
                SftSourceImportAttempt.source_bundle_id == source_bundle_id
            )
        ).scalar_one()
        assert source is not None
        assert attempt.status == "committed"
        assert source.authority_snapshot_sha256 == attempt.authority_snapshot_sha256
        assert source.source_reference_count == 1025
    assert created_engines


def test_large_source_import_rejects_authority_change_before_final_commit(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The final lock-taking validation rejects a mutation after capture."""

    with Session(engine) as session:
        department_id, actor_issuer, actor_subject, chunk_ids = _large_authority_source(session)
    external, source_bundle_id = _write_large_external_source(
        tmp_path / "external-source", department_id, chunk_ids
    )
    runtime = tmp_path / "runtime"
    (runtime / "training_datasets").mkdir(parents=True, mode=0o700)
    settings = SftImportSettings(database_url=_database_url(), data_dir=runtime)
    from app import sft_services

    original = sft_services._mark_attempt_published

    def mutate_after_capture(*args, **kwargs) -> None:
        original(*args, **kwargs)
        with Session(engine) as session:
            chunk = session.get(DocumentChunk, chunk_ids[0])
            assert chunk is not None
            chunk.content_sha256 = "f" * 64
            session.commit()

    monkeypatch.setattr(sft_services, "_mark_attempt_published", mutate_after_capture)
    with pytest.raises(SftImportConfigurationError, match="SFT source import failed"):
        import_sft_source(
            settings,
            department_id=department_id,
            actor_issuer=actor_issuer,
            actor_subject=actor_subject,
            source_directory=external,
            apply=True,
        )
    with Session(engine) as session:
        assert session.get(SftSourceBundle, source_bundle_id) is None


@pytest.mark.parametrize("boundary", ("registration", "final_commit"))
def test_source_import_reauthorizes_after_capture_and_before_final_commit(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, boundary: str
) -> None:
    """A capture result is never authorization evidence for a later mutation."""

    with Session(engine) as session:
        department_id, actor_issuer, actor_subject, chunk_ids = _large_authority_source(
            session, references=9
        )
    external, source_bundle_id = _write_large_external_source(
        tmp_path / "external-source", department_id, chunk_ids
    )
    runtime = tmp_path / "runtime"
    (runtime / "training_datasets").mkdir(parents=True, mode=0o700)
    settings = SftImportSettings(database_url=_database_url(), data_dir=runtime)
    from app import sft_services

    def revoke() -> None:
        with Session(engine) as session:
            membership = session.execute(
                select(Membership).where(
                    Membership.department_id == department_id,
                    Membership.role == "department_admin",
                )
            ).scalar_one()
            membership.status = "suspended"
            session.commit()

    if boundary == "registration":
        original_capture = sft_services.validate_source_authority

        def revoke_after_capture(*args, **kwargs):
            result = original_capture(*args, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(sft_services, "validate_source_authority", revoke_after_capture)
    else:
        original_publish = sft_services._mark_attempt_published

        def revoke_after_publish(*args, **kwargs) -> None:
            original_publish(*args, **kwargs)
            revoke()

        monkeypatch.setattr(sft_services, "_mark_attempt_published", revoke_after_publish)

    with pytest.raises(SftImportConfigurationError, match="Department access denied"):
        import_sft_source(
            settings,
            department_id=department_id,
            actor_issuer=actor_issuer,
            actor_subject=actor_subject,
            source_directory=external,
            apply=True,
        )
    with Session(engine) as session:
        assert session.get(SftSourceBundle, source_bundle_id) is None


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

"""PostgreSQL 16 coverage for Phase 11 metadata-only job persistence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app import training_job_maintenance
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine
from app.models import (
    Base,
    Department,
    Membership,
    PersistentAuditEvent,
    SftDatasetBuild,
    SftSourceBundle,
    TrainingJob,
    TrainingJobArtifactOperation,
    TrainingJobArtifactOperationItem,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
    UserIdentity,
)
from app.schemas import TrainingJobCreateRequest
from app.services import ServiceError
from app.sft_artifacts import SftArtifactError, SftArtifactStore
from app.training_job_maintenance import (
    _register_candidates,
    archive_training_job,
    purge_training_job_artifacts,
    reconcile_training_job_artifacts,
)
from app.training_job_queue import (
    TrainingJobQueueError,
    _fail_or_cancel,
    _load_eligible_dataset,
    claim_next,
    process_training_job,
    renew_lease,
)
from app.training_job_services import enqueue_training_job, review_training_job

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
            "0011_phase12_adapter_registry"
        )


def test_phase11_metadata_schema_is_content_free_and_registered(engine) -> None:
    inspector = inspect(engine)
    job_columns = {column["name"] for column in inspector.get_columns("training_jobs")}
    attempt_columns = {column["name"] for column in inspector.get_columns("training_job_attempts")}
    operation_columns = {
        column["name"] for column in inspector.get_columns("training_job_artifact_operations")
    }
    reservation_columns = {
        column["name"] for column in inspector.get_columns("training_job_purge_reservations")
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
        "dataset_source_bundle_id",
        "dataset_publication_attempt_id",
        "dataset_train_sha256",
        "dataset_source_reference_count",
        "profile_id",
        "base_model_revision",
        "publication_manifest",
        "result_manifest_sha256",
    }.issubset(job_columns)
    assert {"publication_attempt_id", "ownership_manifest", "cleanup_confirmed_at"}.issubset(
        attempt_columns
    )
    assert {
        "operation_type",
        "limit_value",
        "retention_days",
        "status",
        "purged_job_count",
        "success_audited_at",
        "version",
    }.issubset(operation_columns)
    assert {
        "expected_job_version",
        "expected_review_status",
        "retention_anchor_at",
        "retention_days",
        "deletion_authorized_at",
        "tombstone_bound_at",
        "tombstone_identity",
        "authoritative_publication_attempt_id",
        "authoritative_manifest",
        "tombstone_operation_id",
    }.issubset(reservation_columns)
    assert forbidden.isdisjoint(
        job_columns | attempt_columns | operation_columns | reservation_columns
    )
    assert set(Base.metadata.tables).issuperset(
        {
            "training_jobs",
            "training_job_attempts",
            "training_job_artifact_operations",
            "training_job_artifact_operation_items",
            "training_job_purge_reservations",
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
        "ck_training_job_dataset_snapshot_lifecycle",
        "ck_training_job_dataset_snapshot_counts",
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


def _approved_dataset(session: Session, *, role: str = "department_admin"):
    """Create the smallest approved Phase 10 authority surface for queue tests."""

    department = Department(slug=f"phase11-{uuid4().hex}", display_name="Phase 11", status="active")
    identity = UserIdentity(issuer="https://phase11.invalid", subject=uuid4().hex, status="active")
    session.add_all((department, identity))
    session.flush()
    session.add(
        Membership(
            user_id=identity.id,
            department_id=department.id,
            role=role,
            status="active",
            created_by_user_id=identity.id,
        )
    )
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
        examples_byte_size=1,
    )
    session.add(source)
    session.flush()
    now = datetime.now(UTC)
    dataset = SftDatasetBuild(
        department_id=department.id,
        source_bundle_id=source.id,
        requested_by_user_id=identity.id,
        status="succeeded",
        review_status="approved",
        publication_attempt_id=uuid4(),
        attempt_number=1,
        code_revision="a" * 40,
        artifact_contract_version="phase10-sft-dataset-v1",
        example_contract_version="phase10-sft-example-v1",
        normalization_version="phase10-sft-normalization-v1",
        split_version="phase10-sft-group-split-v1",
        validation_ratio=Decimal("0.10"),
        source_example_count=2,
        source_group_count=2,
        source_reference_count=2,
        train_example_count=1,
        validation_example_count=1,
        result_manifest_sha256="d" * 64,
        train_sha256="e" * 64,
        train_byte_size=1,
        validation_sha256="f" * 64,
        validation_byte_size=1,
        provenance_sha256="0" * 64,
        provenance_byte_size=1,
        publication_manifest={},
        finished_at=now,
        reviewed_at=now,
    )
    session.add(dataset)
    session.flush()
    return department, identity, dataset


def _enqueue(
    session: Session,
    department: Department,
    identity: UserIdentity,
    dataset: SftDatasetBuild,
    *,
    code_revision: str = "a" * 40,
):
    return enqueue_training_job(
        session,
        AuthenticatedPrincipal(identity.subject, identity.issuer),
        DepartmentRequestScope(DepartmentScope(department.id)),
        TrainingJobCreateRequest(
            dataset_build_id=dataset.id,
            profile_id="phase11-qwen3-0.6b-lora-v1",
            expected_dataset_version=dataset.version,
            dataset_rights_confirmed=True,
            evaluation_contamination_reviewed=True,
        ),
        code_revision=code_revision,
    )


def _unique_code_revision() -> str:
    return f"{uuid4().hex}{uuid4().hex[:8]}"


def _phase10_records(example_id: str) -> bytes:
    return (
        f'{{"example_id":"{example_id}","messages":[{{"role":"user",'
        '"content":"Synthetic question"},{"role":"assistant",'
        '"content":"Synthetic answer"}]}\n'
    ).encode()


def _publish_private_dataset(root: Path, department: Department, dataset: SftDatasetBuild) -> None:
    """Create the smallest real descriptor-verified Phase 10 final artifact."""

    train = _phase10_records("11111111-1111-1111-1111-111111111111")
    validation = _phase10_records("22222222-2222-2222-2222-222222222222")
    provenance = b'{"source_example_id":"33333333-3333-3333-3333-333333333333"}\n'
    manifest = {
        "artifact_contract_version": dataset.artifact_contract_version,
        "department_id": str(department.id),
        "source_bundle_id": str(dataset.source_bundle_id),
        "build_id": str(dataset.id),
        "publication_attempt_id": str(dataset.publication_attempt_id),
        "attempt_number": dataset.attempt_number,
        "code_revision": dataset.code_revision,
        "normalization_version": dataset.normalization_version,
        "example_contract_version": dataset.example_contract_version,
        "split_version": dataset.split_version,
        "validation_ratio": "0.10",
        "source_example_count": dataset.source_example_count,
        "source_group_count": dataset.source_group_count,
        "source_reference_count": dataset.source_reference_count,
        "train_example_count": 1,
        "validation_example_count": 1,
        "files": {
            "train.jsonl": {"sha256": sha256(train).hexdigest(), "byte_size": len(train)},
            "validation.jsonl": {
                "sha256": sha256(validation).hexdigest(),
                "byte_size": len(validation),
            },
            "provenance.jsonl": {
                "sha256": sha256(provenance).hexdigest(),
                "byte_size": len(provenance),
            },
        },
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    dataset.result_manifest_sha256 = sha256(raw).hexdigest()
    dataset.train_sha256 = sha256(train).hexdigest()
    dataset.train_byte_size = len(train)
    dataset.validation_sha256 = sha256(validation).hexdigest()
    dataset.validation_byte_size = len(validation)
    dataset.provenance_sha256 = sha256(provenance).hexdigest()
    dataset.provenance_byte_size = len(provenance)
    dataset.train_example_count = 1
    dataset.validation_example_count = 1
    dataset.publication_manifest = manifest
    (root / "training_datasets").mkdir(parents=True, mode=0o700)
    with SftArtifactStore(root) as store:
        staged = store.stage_dataset(
            DepartmentScope(department.id),
            dataset.id,
            dataset.publication_attempt_id,
            manifest=raw,
            train=train,
            validation=validation,
            provenance=provenance,
        )
        store.publish(
            staged,
            allowlist=frozenset(
                {"manifest.json", "train.jsonl", "validation.jsonl", "provenance.jsonl"}
            ),
            expected=manifest,
        ).close()


def _succeeded_training_job(factory, root: Path):
    """Build the minimal real Phase 10/11 artifact path for purge tests."""

    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _publish_private_dataset(root, department, dataset)
        code_revision = _unique_code_revision()
        job = _enqueue(session, department, identity, dataset, code_revision=code_revision)
        values = department.id, identity.issuer, identity.subject, job.id
    claim = claim_next(factory, uuid4(), 30, code_revision)
    assert claim is not None and claim.id == values[3]
    process_training_job(factory, root, claim, lease_seconds=30, operation_seconds=20)
    return values


def test_phase11_approved_dataset_claims_with_exact_live_lease(engine) -> None:
    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        code_revision = _unique_code_revision()
        job = _enqueue(session, department, identity, dataset, code_revision=code_revision)
        job_id = job.id
        expected_snapshot = (
            dataset.source_bundle_id,
            dataset.publication_attempt_id,
            dataset.train_sha256,
            dataset.source_reference_count,
        )
    claim = claim_next(factory, uuid4(), 30, code_revision)
    assert claim is not None and claim.id == job_id
    assert (
        claim.dataset_source_bundle_id == expected_snapshot[0]
        and claim.dataset_publication_attempt_id == expected_snapshot[1]
        and claim.dataset_train_sha256 == expected_snapshot[2]
        and claim.dataset_source_reference_count == expected_snapshot[3]
    )
    renew_lease(factory, claim, 30)
    with factory() as session:
        job = session.scalar(select(TrainingJob).where(TrainingJob.id == job_id))
        attempts = session.scalars(
            select(TrainingJobAttempt).where(TrainingJobAttempt.training_job_id == job_id)
        ).all()
        assert job is not None and job.status == "running" and job.claim_token == claim.claim_token
        assert len(attempts) == 1 and attempts[0].status == "running"


def test_phase11_real_worker_publishes_one_descriptor_bound_bundle(engine, tmp_path: Path) -> None:
    """Exercise the real supervisor and child, without a model or LlamaFactory install."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _publish_private_dataset(root, department, dataset)
        code_revision = _unique_code_revision()
        job = _enqueue(session, department, identity, dataset, code_revision=code_revision)
        department_id, job_id = department.id, job.id
    claim = claim_next(factory, uuid4(), 30, code_revision)
    assert claim is not None and claim.id == job_id
    process_training_job(factory, root, claim, lease_seconds=30, operation_seconds=20)
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and (job.status, job.review_status) == ("succeeded", "pending")
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.generate",
            )
            .count()
            == 1
        )


def test_phase11_dataset_eligibility_and_mutation_roles_fail_closed(engine) -> None:
    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session, role="instructor")
        with pytest.raises(ServiceError, match="Department access denied"):
            _enqueue(session, department, identity, dataset)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        dataset.review_status = "rejected"
        with pytest.raises(ServiceError, match="Training dataset is unavailable"):
            _enqueue(session, department, identity, dataset)
        dataset.review_status = "approved"
        with pytest.raises(ServiceError, match="Training dataset is unavailable"):
            enqueue_training_job(
                session,
                AuthenticatedPrincipal(identity.subject, identity.issuer),
                DepartmentRequestScope(DepartmentScope(department.id)),
                TrainingJobCreateRequest(
                    dataset_build_id=dataset.id,
                    profile_id="phase11-qwen3-0.6b-lora-v1",
                    expected_dataset_version=dataset.version + 1,
                    dataset_rights_confirmed=True,
                    evaluation_contamination_reviewed=True,
                ),
                code_revision="a" * 40,
            )


def test_phase11_expired_or_replaced_claim_cannot_renew(engine) -> None:
    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _enqueue(session, department, identity, dataset)
    claim = claim_next(factory, uuid4(), 1, "a" * 40)
    assert claim is not None
    with factory.begin() as session:
        job = session.scalar(
            select(TrainingJob).where(TrainingJob.id == claim.id).with_for_update()
        )
        assert job is not None
        job.claim_token = uuid4()
    with pytest.raises(TrainingJobQueueError, match="claim_lost"):
        renew_lease(factory, claim, 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("publication_attempt_id", uuid4),
        ("attempt_number", lambda: 2),
        ("code_revision", lambda: "b" * 40),
        ("train_sha256", lambda: "1" * 64),
        ("train_byte_size", lambda: 2),
        ("validation_sha256", lambda: "2" * 64),
        ("validation_byte_size", lambda: 2),
        ("provenance_sha256", lambda: "3" * 64),
        ("provenance_byte_size", lambda: 2),
        ("train_example_count", lambda: 2),
        ("validation_example_count", lambda: 2),
        ("source_example_count", lambda: 3),
        ("source_group_count", lambda: 3),
        ("source_reference_count", lambda: 3),
    ],
)
def test_phase11_snapshot_drift_fails_before_artifact_open(engine, field, value) -> None:
    """Every captured Phase 10 authority class is compared, not just version."""

    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _enqueue(session, department, identity, dataset)
    claim = claim_next(factory, uuid4(), 30, "a" * 40)
    assert claim is not None
    with factory.begin() as session:
        dataset = session.get(SftDatasetBuild, claim.dataset_build_id)
        assert dataset is not None
        setattr(dataset, field, value())
        # The source-count constraint deliberately forbids a source example
        # count that exceeds the reference count. Preserve that invariant so
        # this test reaches the Phase 11 snapshot-authority comparison.
        if field == "source_example_count":
            dataset.source_reference_count = dataset.source_example_count
    with pytest.raises(TrainingJobQueueError, match="dataset_authority_changed"):
        _load_eligible_dataset(factory, claim, lambda: None)  # type: ignore[arg-type]


def test_phase11_source_bundle_snapshot_drift_fails_before_artifact_open(engine) -> None:
    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _enqueue(session, department, identity, dataset)
    claim = claim_next(factory, uuid4(), 30, "a" * 40)
    assert claim is not None
    with factory.begin() as session:
        replacement = SftSourceBundle(
            department_id=claim.department_id,
            imported_by_user_id=claim.requested_by_user_id,
            status="active",
            artifact_contract_version="phase10-sft-source-v1",
            normalization_version="phase10-sft-normalization-v1",
            example_contract_version="phase10-sft-example-v1",
            example_count=2,
            group_count=2,
            source_reference_count=2,
            manifest_sha256="4" * 64,
            examples_sha256="5" * 64,
            authority_snapshot_sha256="6" * 64,
            examples_byte_size=1,
        )
        session.add(replacement)
        session.flush()
        dataset = session.get(SftDatasetBuild, claim.dataset_build_id)
        assert dataset is not None
        dataset.source_bundle_id = replacement.id
    with pytest.raises(TrainingJobQueueError, match="dataset_authority_changed"):
        _load_eligible_dataset(factory, claim, lambda: None)  # type: ignore[arg-type]


def test_phase11_active_purge_reservation_fences_review_and_archive(engine, tmp_path: Path) -> None:
    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _publish_private_dataset(root, department, dataset)
        code_revision = _unique_code_revision()
        job = _enqueue(session, department, identity, dataset, code_revision=code_revision)
        department_id, issuer, subject, job_id = (
            department.id,
            identity.issuer,
            identity.subject,
            job.id,
        )
    claim = claim_next(factory, uuid4(), 30, code_revision)
    assert claim is not None and claim.id == job_id
    process_training_job(factory, root, claim, lease_seconds=30, operation_seconds=20)
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and job.status == "succeeded"
        job.review_status = "rejected"
        job.reviewed_at = datetime.now(UTC) - timedelta(days=31)
        job.version += 1
    operation_id, _candidates = _register_candidates(
        factory,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        operation_type="purge",
        retention_days=30,
        limit=1,
        apply=True,
    )
    assert operation_id is not None
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        principal = AuthenticatedPrincipal(subject, issuer)
        scope = DepartmentRequestScope(DepartmentScope(department_id))
        with pytest.raises(ServiceError, match="purge is in progress"):
            review_training_job(
                session,
                principal,
                scope,
                job_id,
                action="archive",
                expected_version=job.version,
            )
    with pytest.raises(ServiceError, match="purge is in progress"):
        archive_training_job(
            factory,
            department_id=department_id,
            training_job_id=job_id,
            actor_issuer=issuer,
            actor_subject=subject,
            apply=True,
        )


def test_phase11_empty_purge_is_a_repeatable_successful_noop(engine, tmp_path: Path) -> None:
    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, _dataset = _approved_dataset(session)
        department_id, issuer, subject = department.id, identity.issuer, identity.subject
    root = tmp_path / "runtime"
    (root / "training_datasets").mkdir(parents=True, mode=0o700)
    for _ in range(2):
        result = purge_training_job_artifacts(
            factory,
            data_dir=root,
            department_id=department_id,
            actor_issuer=issuer,
            actor_subject=subject,
            retention_days=30,
            limit=1,
            apply=True,
        )
        assert (result.eligible_count, result.applied_count, result.blocked_count) == (0, 0, 0)
    with factory() as session:
        assert (
            session.query(TrainingJobArtifactOperation)
            .filter(
                TrainingJobArtifactOperation.department_id == department_id,
                TrainingJobArtifactOperation.operation_type == "purge",
            )
            .count()
            == 0
        )


def test_phase11_legacy_empty_registered_purge_operation_cannot_wedge_maintenance(
    engine, tmp_path: Path
) -> None:
    """A legacy empty operation closes safely before a fresh purge is considered."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    with factory.begin() as session:
        department, identity, _dataset = _approved_dataset(session)
        operation = TrainingJobArtifactOperation(
            department_id=department.id,
            requested_by_user_id=identity.id,
            limit_value=1,
            retention_days=30,
            operation_type="purge",
            status="registered",
        )
        session.add(operation)
        session.flush()
        operation_id = operation.id
        department_id, issuer, subject = department.id, identity.issuer, identity.subject
    (root / "training_datasets").mkdir(parents=True, mode=0o700)
    result = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=1,
        apply=True,
    )
    assert (result.eligible_count, result.applied_count, result.blocked_count) == (0, 0, 0)
    with factory() as session:
        operation = session.get(TrainingJobArtifactOperation, operation_id)
        assert operation is not None and operation.status == "completed"


def test_phase11_stage_only_reconciliation_confirms_exact_attempt(engine, tmp_path: Path) -> None:
    """A terminal attempt with no final manifest completes after stage cleanup."""

    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        code_revision = _unique_code_revision()
        job = _enqueue(session, department, identity, dataset, code_revision=code_revision)
        department_id, issuer, subject, job_id = (
            department.id,
            identity.issuer,
            identity.subject,
            job.id,
        )
    claim = claim_next(factory, uuid4(), 30, code_revision)
    assert claim is not None and claim.id == job_id
    root = tmp_path / "runtime"
    (root / "training_datasets").mkdir(parents=True, mode=0o700)
    with SftArtifactStore(root) as store:
        stage = store.prepare_training_job_stage(
            DepartmentScope(department_id), job_id, claim.publication_attempt_id
        )
        stage.close()
    assert _fail_or_cancel(factory, claim, "dataset_unavailable")

    result = reconcile_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        limit=1,
        apply=True,
    )
    assert (result.eligible_count, result.applied_count, result.blocked_count) == (1, 1, 0)
    with factory() as session:
        attempt = session.scalar(
            select(TrainingJobAttempt).where(
                TrainingJobAttempt.training_job_id == job_id,
                TrainingJobAttempt.publication_attempt_id == claim.publication_attempt_id,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.reconcile",
            )
            .count()
            == 1
        )


def test_phase11_blocked_historical_stage_leaves_authoritative_final_intact(
    engine, tmp_path: Path
) -> None:
    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _publish_private_dataset(root, department, dataset)
        code_revision = _unique_code_revision()
        job = _enqueue(session, department, identity, dataset, code_revision=code_revision)
        department_id, issuer, subject, job_id = (
            department.id,
            identity.issuer,
            identity.subject,
            job.id,
        )
    claim = claim_next(factory, uuid4(), 30, code_revision)
    assert claim is not None and claim.id == job_id
    process_training_job(factory, root, claim, lease_seconds=30, operation_seconds=20)
    historical_attempt_id = uuid4()
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and isinstance(job.publication_manifest, dict)
        historical_manifest = dict(job.publication_manifest)
        historical_manifest["publication_attempt_id"] = str(historical_attempt_id)
        historical_manifest["attempt_number"] = 2
        session.add(
            TrainingJobAttempt(
                department_id=department_id,
                training_job_id=job_id,
                attempt_number=2,
                publication_attempt_id=historical_attempt_id,
                code_revision=job.code_revision,
                status="reclaimed",
                ownership_manifest=historical_manifest,
                finished_at=datetime.now(UTC),
            )
        )
        job.review_status = "archived"
        job.archived_at = datetime.now(UTC) - timedelta(days=31)
        job.version += 1
        expected = dict(job.publication_manifest)
    with SftArtifactStore(root) as store:
        stage = store.prepare_training_job_stage(
            DepartmentScope(department_id), job_id, historical_attempt_id
        )
        stage.close()
    stage_path = (
        root
        / "training_datasets"
        / ".staging"
        / "jobs"
        / str(department_id)
        / str(job_id)
        / str(historical_attempt_id)
    )
    os.symlink("unexpected", stage_path / "unsafe")

    result = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=1,
        apply=True,
    )
    assert result.blocked_count == 1
    with SftArtifactStore(root) as store:
        assert store.verify_training_job_final(
            DepartmentScope(department_id), job_id, expected=expected
        )
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        reservation = session.scalar(
            select(TrainingJobPurgeReservation).where(
                TrainingJobPurgeReservation.training_job_id == job_id
            )
        )
        assert job is not None and job.review_status == "archived" and job.purged_at is None
        assert reservation is not None and reservation.status == "terminalized"


def test_phase11_purge_has_one_authoritative_final_and_historical_stage_only(
    engine, tmp_path: Path
) -> None:
    """Historical manifests never authorize a second physical final deletion."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _publish_private_dataset(root, department, dataset)
        code_revision = _unique_code_revision()
        job = _enqueue(session, department, identity, dataset, code_revision=code_revision)
        department_id, issuer, subject, job_id = (
            department.id,
            identity.issuer,
            identity.subject,
            job.id,
        )
    claim = claim_next(factory, uuid4(), 30, code_revision)
    assert claim is not None and claim.id == job_id
    process_training_job(factory, root, claim, lease_seconds=30, operation_seconds=20)
    historical_attempt_id = uuid4()
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and isinstance(job.publication_manifest, dict)
        historical_manifest = dict(job.publication_manifest)
        historical_manifest["publication_attempt_id"] = str(historical_attempt_id)
        historical_manifest["attempt_number"] = 2
        session.add(
            TrainingJobAttempt(
                department_id=department_id,
                training_job_id=job_id,
                attempt_number=2,
                publication_attempt_id=historical_attempt_id,
                code_revision=job.code_revision,
                status="reclaimed",
                ownership_manifest=historical_manifest,
                finished_at=datetime.now(UTC),
            )
        )
        job.review_status = "rejected"
        job.reviewed_at = datetime.now(UTC) - timedelta(days=31)
        job.version += 1
        authoritative_attempt_id = job.publication_attempt_id
        authoritative_manifest = dict(job.publication_manifest)
    with SftArtifactStore(root) as store:
        historical_stage = store.prepare_training_job_stage(
            DepartmentScope(department_id), job_id, historical_attempt_id
        )
        historical_stage.close()

    result = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=1,
        apply=True,
    )
    assert (result.eligible_count, result.applied_count, result.blocked_count) == (3, 3, 0)
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        attempts = session.scalars(
            select(TrainingJobAttempt)
            .where(TrainingJobAttempt.training_job_id == job_id)
            .order_by(TrainingJobAttempt.attempt_number)
        ).all()
        assert job is not None and job.review_status == "purged" and job.purged_at is not None
        assert len(attempts) == 2 and all(
            item.cleanup_confirmed_at is not None for item in attempts
        )
        items = session.scalars(
            select(TrainingJobArtifactOperationItem).where(
                TrainingJobArtifactOperationItem.training_job_id == job_id,
                TrainingJobArtifactOperationItem.resource_surface == "final",
            )
        ).all()
        assert len(items) == 1
        assert items[0].publication_attempt_id == authoritative_attempt_id
        assert items[0].ownership_manifest == authoritative_manifest
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
            )
            .count()
            == 1
        )


def test_phase11_purge_recovers_after_final_deletion_before_database_commit(
    engine, tmp_path: Path, monkeypatch
) -> None:
    """A committed authorization makes an absent final idempotent on retry."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    department_id, issuer, subject, job_id = _succeeded_training_job(factory, root)
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        job.review_status = "rejected"
        job.reviewed_at = datetime.now(UTC) - timedelta(days=31)
        job.version += 1

    original = training_job_maintenance._persist_purge_final_outcomes

    def database_finalization_failure(*_args, **_kwargs) -> None:
        raise ServiceError(503, "Database unavailable")

    monkeypatch.setattr(
        training_job_maintenance,
        "_persist_purge_final_outcomes",
        database_finalization_failure,
    )
    with pytest.raises(ServiceError, match="Database unavailable"):
        purge_training_job_artifacts(
            factory,
            data_dir=root,
            department_id=department_id,
            actor_issuer=issuer,
            actor_subject=subject,
            retention_days=30,
            limit=1,
            apply=True,
        )
    with factory() as session:
        reservation = session.scalar(
            select(TrainingJobPurgeReservation).where(
                TrainingJobPurgeReservation.training_job_id == job_id
            )
        )
        assert reservation is not None and reservation.status == "tombstone_bound"
    monkeypatch.setattr(training_job_maintenance, "_persist_purge_final_outcomes", original)
    result = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=1,
        apply=True,
    )
    assert (result.eligible_count, result.applied_count, result.blocked_count) == (1, 1, 0)
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and job.review_status == "purged" and job.purged_at is not None
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
            )
            .count()
            == 1
        )


def test_phase11_purge_keeps_tombstone_reservation_active_until_cleanup_resumes(
    engine, tmp_path: Path, monkeypatch
) -> None:
    """A post-rename cleanup interruption cannot terminalize final deletion."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    department_id, issuer, subject, job_id = _succeeded_training_job(factory, root)
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        job.review_status = "rejected"
        job.reviewed_at = datetime.now(UTC) - timedelta(days=31)
        job.version += 1

    original = SftArtifactStore.unlink_bound_training_job_tombstone_file

    def interrupt_after_tombstone_member(*args, **kwargs) -> None:
        original(*args, **kwargs)
        raise SftArtifactError("artifact_ownership_mismatch")

    monkeypatch.setattr(
        SftArtifactStore,
        "unlink_bound_training_job_tombstone_file",
        interrupt_after_tombstone_member,
    )
    result = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=1,
        apply=True,
    )
    assert (result.eligible_count, result.applied_count, result.blocked_count) == (2, 1, 1)
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        reservation = session.scalar(
            select(TrainingJobPurgeReservation).where(
                TrainingJobPurgeReservation.training_job_id == job_id
            )
        )
        assert job is not None and job.review_status == "rejected" and job.purged_at is None
        assert reservation is not None and reservation.status == "tombstone_bound"
        assert reservation.tombstone_operation_id == reservation.operation_id
        assert reservation.authoritative_publication_attempt_id is not None
        assert isinstance(reservation.authoritative_manifest, dict)
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
            )
            .count()
            == 0
        )
    monkeypatch.setattr(SftArtifactStore, "unlink_bound_training_job_tombstone_file", original)
    recovered = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=1,
        apply=True,
    )
    assert (recovered.eligible_count, recovered.applied_count, recovered.blocked_count) == (1, 1, 0)
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and job.review_status == "purged" and job.purged_at is not None
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
            )
            .count()
            == 1
        )


def test_phase11_multi_job_purge_emits_one_audit_only_after_recovery(
    engine, tmp_path: Path, monkeypatch
) -> None:
    """One operation may purge many jobs but can record only one success audit."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        _publish_private_dataset(root, department, dataset)
        first = _enqueue(
            session, department, identity, dataset, code_revision=_unique_code_revision()
        )
        second = _enqueue(
            session, department, identity, dataset, code_revision=_unique_code_revision()
        )
        department_id, issuer, subject = department.id, identity.issuer, identity.subject
        first_id, second_id = first.id, second.id
        revisions = {first.id: first.code_revision, second.id: second.code_revision}
    for job_id in (first_id, second_id):
        claim = claim_next(factory, uuid4(), 30, revisions[job_id])
        assert claim is not None and claim.id == job_id
        process_training_job(factory, root, claim, lease_seconds=30, operation_seconds=20)
    with factory.begin() as session:
        for job_id in (first_id, second_id):
            job = session.get(TrainingJob, job_id)
            assert job is not None
            job.review_status = "rejected"
            job.reviewed_at = datetime.now(UTC) - timedelta(days=31)
            job.version += 1

    original = SftArtifactStore.unlink_bound_training_job_tombstone_file

    def interrupt_second_job(store, scope, job_id, operation_id, **kwargs):
        result = original(store, scope, job_id, operation_id, **kwargs)
        if job_id == second_id:
            raise SftArtifactError("artifact_ownership_mismatch")
        return result

    monkeypatch.setattr(
        SftArtifactStore,
        "unlink_bound_training_job_tombstone_file",
        interrupt_second_job,
    )
    first_result = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=2,
        apply=True,
    )
    assert first_result.applied_count > 0 and first_result.blocked_count == 1
    with factory() as session:
        operation = session.scalar(
            select(TrainingJobArtifactOperation)
            .where(TrainingJobArtifactOperation.department_id == department_id)
            .order_by(TrainingJobArtifactOperation.created_at.desc())
        )
        assert operation is not None and operation.status == "registered"
        assert operation.purged_job_count == 1 and operation.success_audited_at is None
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
                PersistentAuditEvent.resource_id == str(operation.id),
            )
            .count()
            == 0
        )
    monkeypatch.setattr(SftArtifactStore, "unlink_bound_training_job_tombstone_file", original)
    recovered = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=2,
        apply=True,
    )
    assert recovered.applied_count > 0 and recovered.blocked_count == 0
    with factory() as session:
        operation = session.scalar(
            select(TrainingJobArtifactOperation)
            .where(TrainingJobArtifactOperation.department_id == department_id)
            .order_by(TrainingJobArtifactOperation.created_at.desc())
        )
        assert operation is not None
        assert operation.status == "completed" and operation.purged_job_count == 2
        assert operation.success_audited_at is not None
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
                PersistentAuditEvent.resource_id == str(operation.id),
            )
            .count()
            == 1
        )

    repeated = purge_training_job_artifacts(
        factory,
        data_dir=root,
        department_id=department_id,
        actor_issuer=issuer,
        actor_subject=subject,
        retention_days=30,
        limit=2,
        apply=True,
    )
    assert (repeated.eligible_count, repeated.applied_count, repeated.blocked_count) == (0, 0, 0)
    with factory() as session:
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
            )
            .count()
            == 1
        )


def test_phase11_purge_never_deletes_final_before_stage_outcomes_are_committed(
    engine, tmp_path: Path, monkeypatch
) -> None:
    """A failure before final authorization leaves the current final untouched."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    department_id, issuer, subject, job_id = _succeeded_training_job(factory, root)
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and isinstance(job.publication_manifest, dict)
        expected = dict(job.publication_manifest)
        job.review_status = "archived"
        job.archived_at = datetime.now(UTC) - timedelta(days=31)
        job.version += 1

    original = training_job_maintenance._authorize_final_deletion

    def authorization_failure(*_args, **_kwargs):
        raise ServiceError(503, "Database unavailable")

    monkeypatch.setattr(
        training_job_maintenance, "_authorize_final_deletion", authorization_failure
    )
    with pytest.raises(ServiceError, match="Database unavailable"):
        purge_training_job_artifacts(
            factory,
            data_dir=root,
            department_id=department_id,
            actor_issuer=issuer,
            actor_subject=subject,
            retention_days=30,
            limit=1,
            apply=True,
        )
    with SftArtifactStore(root) as store:
        assert store.verify_training_job_final(
            DepartmentScope(department_id), job_id, expected=expected
        )
    with factory() as session:
        reservation = session.scalar(
            select(TrainingJobPurgeReservation).where(
                TrainingJobPurgeReservation.training_job_id == job_id
            )
        )
        assert reservation is not None and reservation.status == "registered"
    monkeypatch.setattr(training_job_maintenance, "_authorize_final_deletion", original)


def test_phase11_authoritative_final_requires_exact_cross_row_owner_metadata(
    engine, tmp_path: Path
) -> None:
    """Persistable cross-row ambiguity fails closed before any artifact mutation."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    department_id, issuer, subject, job_id = _succeeded_training_job(factory, root)
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None and isinstance(job.publication_manifest, dict)
        altered = dict(job.publication_manifest)
        altered["attempt_number"] = 2
        job.publication_manifest = altered
        job.review_status = "rejected"
        job.reviewed_at = datetime.now(UTC) - timedelta(days=31)
        job.version += 1
    with pytest.raises(ServiceError, match="purge authority changed"):
        purge_training_job_artifacts(
            factory,
            data_dir=root,
            department_id=department_id,
            actor_issuer=issuer,
            actor_subject=subject,
            retention_days=30,
            limit=1,
            apply=True,
        )
    with factory() as session:
        assert (
            session.query(TrainingJobArtifactOperation)
            .filter(
                TrainingJobArtifactOperation.department_id == department_id,
                TrainingJobArtifactOperation.operation_type == "purge",
            )
            .count()
            == 0
        )


def test_phase11_succeeded_rows_reject_missing_owner_or_nonpositive_payload_metadata(
    engine, tmp_path: Path
) -> None:
    """The migration check prevents incomplete success rows from becoming purgeable."""

    factory = sessionmaker(engine)
    root = tmp_path / "runtime"
    _department_id, _issuer, _subject, job_id = _succeeded_training_job(factory, root)
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        job.publication_manifest = None
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    with factory() as session:
        attempt = session.scalar(
            select(TrainingJobAttempt).where(
                TrainingJobAttempt.training_job_id == job_id,
                TrainingJobAttempt.status == "succeeded",
            )
        )
        assert attempt is not None
        attempt.ownership_manifest = None
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    with factory() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        job.training_config_byte_size = 0
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

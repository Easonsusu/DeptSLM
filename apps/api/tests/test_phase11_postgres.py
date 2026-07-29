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
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
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
    TrainingJobAttempt,
    UserIdentity,
)
from app.schemas import TrainingJobCreateRequest
from app.services import ServiceError
from app.sft_artifacts import SftArtifactStore
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
            "0009_phase11_training_jobs"
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
    assert {"operation_type", "limit_value", "retention_days", "status"}.issubset(operation_columns)
    assert {
        "expected_job_version",
        "expected_review_status",
        "retention_anchor_at",
        "retention_days",
        "deletion_authorized_at",
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
        code_revision="a" * 40,
    )


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


def test_phase11_approved_dataset_claims_with_exact_live_lease(engine) -> None:
    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        job = _enqueue(session, department, identity, dataset)
        job_id = job.id
    claim = claim_next(factory, uuid4(), 30, "a" * 40)
    assert claim is not None and claim.id == job_id
    assert (
        claim.dataset_source_bundle_id == dataset.source_bundle_id
        and claim.dataset_publication_attempt_id == dataset.publication_attempt_id
        and claim.dataset_train_sha256 == dataset.train_sha256
        and claim.dataset_source_reference_count == dataset.source_reference_count
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
        job = _enqueue(session, department, identity, dataset)
        department_id, job_id = department.id, job.id
    claim = claim_next(factory, uuid4(), 30, "a" * 40)
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


def test_phase11_active_purge_reservation_fences_review_and_archive(engine) -> None:
    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        job = _enqueue(session, department, identity, dataset)
        now = datetime.now(UTC)
        job.status = "succeeded"
        job.review_status = "rejected"
        job.finished_at = now
        job.reviewed_at = now - timedelta(days=31)
        job.train_example_count = job.validation_example_count = 1
        job.result_manifest_sha256 = "1" * 64
        job.training_config_sha256 = "2" * 64
        job.training_config_byte_size = 1
        job.dataset_info_sha256 = "3" * 64
        job.dataset_info_byte_size = 1
        job.train_sha256 = "4" * 64
        job.train_byte_size = 1
        job.validation_sha256 = "5" * 64
        job.validation_byte_size = 1
        session.add(
            TrainingJobAttempt(
                department_id=department.id,
                training_job_id=job.id,
                attempt_number=1,
                publication_attempt_id=uuid4(),
                code_revision=job.code_revision,
                status="succeeded",
                published_at=now,
                finished_at=now,
            )
        )
        department_id, issuer, subject, job_id = (
            department.id,
            identity.issuer,
            identity.subject,
            job.id,
        )
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


def test_phase11_stage_only_reconciliation_confirms_exact_attempt(engine, tmp_path: Path) -> None:
    """A terminal attempt with no final manifest completes after stage cleanup."""

    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        job = _enqueue(session, department, identity, dataset)
        department_id, issuer, subject, job_id = (
            department.id,
            identity.issuer,
            identity.subject,
            job.id,
        )
    claim = claim_next(factory, uuid4(), 30, "a" * 40)
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


def test_phase11_purge_selects_one_job_and_all_of_its_terminal_attempts(
    engine, tmp_path: Path
) -> None:
    """The purge limit applies to jobs, never an arbitrary attempt subset."""

    factory = sessionmaker(engine)
    with factory.begin() as session:
        department, identity, dataset = _approved_dataset(session)
        job = _enqueue(session, department, identity, dataset)
        now = datetime.now(UTC)
        job.status = "succeeded"
        job.review_status = "rejected"
        job.finished_at = now
        job.reviewed_at = now - timedelta(days=31)
        job.train_example_count = 1
        job.validation_example_count = 1
        job.result_manifest_sha256 = "1" * 64
        job.training_config_sha256 = "2" * 64
        job.training_config_byte_size = 1
        job.dataset_info_sha256 = "3" * 64
        job.dataset_info_byte_size = 1
        job.train_sha256 = "4" * 64
        job.train_byte_size = 1
        job.validation_sha256 = "5" * 64
        job.validation_byte_size = 1
        job.worker_id = job.claim_token = job.lease_expires_at = None
        attempts = []
        for number in (1, 2):
            attempts.append(
                TrainingJobAttempt(
                    department_id=department.id,
                    training_job_id=job.id,
                    attempt_number=number,
                    publication_attempt_id=uuid4(),
                    code_revision=job.code_revision,
                    status="succeeded",
                    published_at=now,
                    finished_at=now,
                )
            )
        session.add_all(attempts)
        department_id, issuer, subject, job_id = (
            department.id,
            identity.issuer,
            identity.subject,
            job.id,
        )
    root = tmp_path / "runtime"
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
    assert (result.eligible_count, result.applied_count, result.blocked_count) == (2, 2, 0)
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
        assert (
            session.query(PersistentAuditEvent)
            .filter(
                PersistentAuditEvent.department_id == department_id,
                PersistentAuditEvent.action == "training.job.purge",
            )
            .count()
            == 1
        )

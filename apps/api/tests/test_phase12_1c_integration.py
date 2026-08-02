"""Real PostgreSQL integration coverage for Phase 12.1C registry publication."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.adapter_registry_queue import (
    AdapterRegistryQueueError,
    ClaimedAdapter,
    claim_next_adapter,
    renew_adapter_lease,
    terminal_failure,
)
from app.adapter_registry_services import enqueue_adapter_registry
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine
from app.models import (
    Adapter,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    Department,
    Membership,
    PersistentAuditEvent,
    SftDatasetBuild,
    SftDatasetBuildAttempt,
    SftSourceBundle,
    TrainingJob,
    TrainingJobArtifactOperation,
    TrainingJobArtifactOperationItem,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
    UserIdentity,
)
from app.services import ServiceError
from app.training_job_domain import ValidatedDataset, build_bundle

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


@pytest.fixture
def factory(engine):
    return sessionmaker(engine)


@dataclass(frozen=True, slots=True)
class Authority:
    department_id: UUID
    admin_id: UUID
    issuer: str
    subject: str
    source_id: UUID
    source_version: int
    training_job_id: UUID
    training_job_version: int
    dataset_id: UUID
    dataset_attempt_id: UUID
    source_attempt_id: UUID
    source_attempt_version: int
    training_attempt_id: UUID
    training_attempt_version: int
    source_publication_attempt_id: UUID
    training_publication_attempt_id: UUID
    dataset_publication_attempt_id: UUID
    code_revision: str


def _manifest(value: dict[str, object]) -> tuple[dict[str, object], str, int]:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    return value, hashlib.sha256(raw).hexdigest(), len(raw)


def _seed_authority(session: Session) -> Authority:
    """Insert one complete content-free Phase 10/11/12 authority lineage."""

    now = datetime.now(UTC)
    department = Department(
        slug=f"phase12c-{uuid4().hex[:12]}", display_name="Phase 12.1C", status="active"
    )
    identity = UserIdentity(
        issuer="https://phase12c.invalid", subject=f"admin-{uuid4().hex}", status="active"
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

    source_id = uuid4()
    source_attempt_id = uuid4()
    source_publication_id = uuid4()
    source_code_revision = "1" * 40
    source_manifest = {
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "department_id": str(department.id),
        "source_bundle_id": str(source_id),
        "import_attempt_id": str(source_attempt_id),
        "publication_attempt_id": str(source_publication_id),
        "attempt_number": 1,
        "imported_by_user_id": str(identity.id),
        "code_revision": source_code_revision,
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
            "adapter_config.json": {"sha256": "a" * 64, "byte_size": 2},
            "adapter_model.safetensors": {"sha256": "b" * 64, "byte_size": 5},
        },
    }
    source_manifest, source_manifest_sha, source_manifest_size = _manifest(source_manifest)
    source = AdapterImportSource(
        id=source_id,
        department_id=department.id,
        imported_by_user_id=identity.id,
        status="staging",
        source_contract_version="phase12-adapter-source-v1",
        intake_contract_version="phase12-adapter-intake-v1",
        config_contract_version="phase12-adapter-config-v1",
        tensor_contract_version="phase12-adapter-tensors-v1",
        base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        base_model_license="Apache-2.0",
        peft_version="0.18.1",
        safetensors_format="0.7.0",
        code_revision=source_code_revision,
    )
    source_attempt = AdapterImportAttempt(
        id=source_attempt_id,
        department_id=department.id,
        source_bundle_id=source_id,
        attempt_number=1,
        publication_attempt_id=source_publication_id,
        status="committed",
        ownership_manifest=source_manifest,
        code_revision=source_code_revision,
        validated_at=now,
        staged_at=now,
        published_at=now,
        committed_at=now,
        finished_at=now,
        version=5,
    )
    # The source/authoritative-attempt FK is closed only after both rows exist.
    session.add(source)
    session.flush()
    session.add(source_attempt)
    session.flush()
    source.authoritative_attempt_id = source_attempt.id
    source.status = "committed"
    source.adapter_config_sha256 = "a" * 64
    source.adapter_config_byte_size = 2
    source.adapter_model_sha256 = "b" * 64
    source.adapter_model_byte_size = 5
    source.intake_manifest_sha256 = source_manifest_sha
    source.intake_manifest_byte_size = source_manifest_size
    source.tensor_dtype = "F16"
    source.tensor_count = 392
    source.tensor_element_count = 10092544
    source.tensor_payload_byte_size = 20185088
    source.committed_at = now
    source.version = 2

    phase10_source = SftSourceBundle(
        department_id=department.id,
        imported_by_user_id=identity.id,
        status="active",
        artifact_contract_version="phase10-sft-source-v1",
        normalization_version="phase10-sft-normalization-v1",
        example_contract_version="phase10-sft-example-v1",
        example_count=2,
        group_count=2,
        source_reference_count=2,
        manifest_sha256="c" * 64,
        examples_sha256="d" * 64,
        authority_snapshot_sha256="e" * 64,
        examples_byte_size=1,
    )
    session.add(phase10_source)
    session.flush()
    dataset_publication_id = uuid4()
    dataset = SftDatasetBuild(
        id=uuid4(),
        department_id=department.id,
        source_bundle_id=phase10_source.id,
        requested_by_user_id=identity.id,
        status="succeeded",
        review_status="approved",
        publication_attempt_id=dataset_publication_id,
        attempt_number=1,
        code_revision="2" * 40,
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
        result_manifest_sha256="f" * 64,
        train_sha256="1" * 64,
        train_byte_size=10,
        validation_sha256="2" * 64,
        validation_byte_size=11,
        provenance_sha256="3" * 64,
        provenance_byte_size=12,
        publication_manifest={"artifact_contract_version": "phase10-sft-dataset-v1"},
        finished_at=now,
        reviewed_at=now,
        version=2,
    )
    session.add(dataset)
    session.flush()
    dataset_attempt = SftDatasetBuildAttempt(
        id=uuid4(),
        department_id=department.id,
        build_id=dataset.id,
        attempt_number=1,
        publication_attempt_id=dataset_publication_id,
        code_revision=dataset.code_revision,
        status="succeeded",
        ownership_manifest={},
        claimed_at=now,
        published_at=now,
        finished_at=now,
        version=2,
    )
    session.add(dataset_attempt)
    session.flush()

    train_raw = b'{"messages":[{"role":"user","content":"q"}]}\n'
    validation_raw = b'{"messages":[{"role":"user","content":"v"}]}\n'
    dataset_values = ValidatedDataset(
        train_count=1,
        validation_count=1,
        train_sha256=hashlib.sha256(train_raw).hexdigest(),
        validation_sha256=hashlib.sha256(validation_raw).hexdigest(),
        train_byte_size=len(train_raw),
        validation_byte_size=len(validation_raw),
    )
    training_job_id = uuid4()
    training_publication_id = uuid4()
    training_scope_id = uuid4()
    training_code_revision = uuid4().hex + uuid4().hex[:8]
    bundle = build_bundle(
        department_id=department.id,
        training_job_id=training_job_id,
        dataset_build_id=dataset.id,
        publication_attempt_id=training_publication_id,
        execution_scope_id=training_scope_id,
        attempt_number=1,
        code_revision=training_code_revision,
        dataset_build_version=dataset.version,
        dataset_manifest_sha256=dataset.result_manifest_sha256,
        dataset_artifact_contract_version=dataset.artifact_contract_version,
        dataset_example_contract_version=dataset.example_contract_version,
        dataset_normalization_version=dataset.normalization_version,
        dataset_split_version=dataset.split_version,
        profile_id="phase11-qwen3-0.6b-lora-v1",
        dataset_rights_attested=True,
        evaluation_contamination_reviewed=True,
        dataset=dataset_values,
    )
    training_manifest = json.loads(bundle.manifest)
    training_manifest_sha = hashlib.sha256(bundle.manifest).hexdigest()
    training_files = training_manifest["files"]
    job = TrainingJob(
        id=training_job_id,
        department_id=department.id,
        dataset_build_id=dataset.id,
        requested_by_user_id=identity.id,
        status="succeeded",
        review_status="approved",
        profile_id="phase11-qwen3-0.6b-lora-v1",
        base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        base_model_license="Apache-2.0",
        llamafactory_version="0.9.5",
        artifact_contract_version=training_manifest["artifact_contract_version"],
        manifest_contract_version=training_manifest["manifest_contract_version"],
        configuration_contract_version=training_manifest["configuration_contract_version"],
        dataset_info_contract_version=training_manifest["dataset_info_contract_version"],
        execution_profile_contract_version=training_manifest["execution_profile_contract_version"],
        dataset_artifact_contract_version=dataset.artifact_contract_version,
        dataset_example_contract_version=dataset.example_contract_version,
        dataset_normalization_version=dataset.normalization_version,
        dataset_split_version=dataset.split_version,
        dataset_build_version=dataset.version,
        dataset_manifest_sha256=dataset.result_manifest_sha256,
        dataset_source_bundle_id=dataset.source_bundle_id,
        dataset_status=dataset.status,
        dataset_review_status=dataset.review_status,
        dataset_publication_attempt_id=dataset.publication_attempt_id,
        dataset_publication_attempt_number=dataset.attempt_number,
        dataset_code_revision=dataset.code_revision,
        dataset_train_sha256=dataset.train_sha256,
        dataset_train_byte_size=dataset.train_byte_size,
        dataset_validation_sha256=dataset.validation_sha256,
        dataset_validation_byte_size=dataset.validation_byte_size,
        dataset_provenance_sha256=dataset.provenance_sha256,
        dataset_provenance_byte_size=dataset.provenance_byte_size,
        dataset_train_example_count=dataset.train_example_count,
        dataset_validation_example_count=dataset.validation_example_count,
        dataset_source_example_count=dataset.source_example_count,
        dataset_source_group_count=dataset.source_group_count,
        dataset_source_reference_count=dataset.source_reference_count,
        dataset_rights_attested=True,
        evaluation_contamination_reviewed=True,
        execution_scope_id=training_scope_id,
        publication_attempt_id=training_publication_id,
        attempt_number=1,
        code_revision=training_code_revision,
        train_example_count=1,
        validation_example_count=1,
        maximum_record_content_bytes=7680,
        result_manifest_sha256=training_manifest_sha,
        training_config_sha256=training_files["training.yaml"]["sha256"],
        training_config_byte_size=training_files["training.yaml"]["byte_size"],
        dataset_info_sha256=training_files["dataset_info.json"]["sha256"],
        dataset_info_byte_size=training_files["dataset_info.json"]["byte_size"],
        train_sha256=training_files["train.jsonl"]["sha256"],
        train_byte_size=training_files["train.jsonl"]["byte_size"],
        validation_sha256=training_files["validation.jsonl"]["sha256"],
        validation_byte_size=training_files["validation.jsonl"]["byte_size"],
        publication_manifest=training_manifest,
        finished_at=now,
        version=2,
    )
    session.add(job)
    session.flush()
    training_attempt = TrainingJobAttempt(
        id=uuid4(),
        department_id=department.id,
        training_job_id=job.id,
        attempt_number=1,
        publication_attempt_id=training_publication_id,
        code_revision=training_code_revision,
        status="succeeded",
        ownership_manifest=training_manifest,
        claimed_at=now,
        staged_at=now,
        published_at=now,
        finished_at=now,
        version=2,
    )
    session.add(training_attempt)
    session.commit()
    return Authority(
        department_id=department.id,
        admin_id=identity.id,
        issuer=identity.issuer,
        subject=identity.subject,
        source_id=source.id,
        source_version=source.version,
        training_job_id=job.id,
        training_job_version=job.version,
        dataset_id=dataset.id,
        dataset_attempt_id=dataset_attempt.id,
        source_attempt_id=source_attempt.id,
        source_attempt_version=source_attempt.version,
        training_attempt_id=training_attempt.id,
        training_attempt_version=training_attempt.version,
        source_publication_attempt_id=source_publication_id,
        training_publication_attempt_id=training_publication_id,
        dataset_publication_attempt_id=dataset_publication_id,
        code_revision=training_code_revision,
    )


@pytest.fixture
def authority(factory):
    with factory() as session:
        value = _seed_authority(session)
    yield value
    with factory.begin() as session:
        # Keep this module isolated from migration tests that intentionally
        # downgrade the shared CI database to 0010. First break the source
        # foreign-key cycle and source-to-adapter claim before deleting the
        # exact seeded lineage.
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == value.department_id,
                AdapterImportSource.id == value.source_id,
            )
            .values(
                status="committed",
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
                error_code=None,
            )
        )
        session.execute(
            delete(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobPurgeReservation).where(
                TrainingJobPurgeReservation.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobArtifactOperationItem).where(
                TrainingJobArtifactOperationItem.department_id == value.department_id
            )
        )
        session.execute(
            delete(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.department_id == value.department_id
            )
        )
        session.execute(
            delete(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == value.department_id
            )
        )
        session.execute(delete(Adapter).where(Adapter.department_id == value.department_id))
        session.execute(
            delete(TrainingJobArtifactOperation).where(
                TrainingJobArtifactOperation.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobAttempt).where(
                TrainingJobAttempt.department_id == value.department_id
            )
        )
        session.execute(delete(TrainingJob).where(TrainingJob.department_id == value.department_id))
        session.execute(
            delete(SftDatasetBuildAttempt).where(
                SftDatasetBuildAttempt.department_id == value.department_id
            )
        )
        session.execute(
            delete(SftDatasetBuild).where(SftDatasetBuild.department_id == value.department_id)
        )
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == value.department_id,
                AdapterImportSource.id == value.source_id,
            )
            .values(
                status="staging",
                authoritative_attempt_id=None,
                committed_at=None,
                intake_manifest_sha256=None,
                intake_manifest_byte_size=None,
            )
        )
        session.execute(
            delete(AdapterImportAttempt).where(
                AdapterImportAttempt.department_id == value.department_id
            )
        )
        session.execute(
            delete(AdapterImportSource).where(
                AdapterImportSource.department_id == value.department_id
            )
        )
        session.execute(
            delete(SftSourceBundle).where(SftSourceBundle.department_id == value.department_id)
        )
        session.execute(delete(Membership).where(Membership.department_id == value.department_id))
        session.execute(delete(UserIdentity).where(UserIdentity.issuer == value.issuer))
        session.execute(delete(Department).where(Department.id == value.department_id))


def _principal(authority: Authority) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(subject=authority.subject, issuer=authority.issuer)


def _scope(authority: Authority) -> DepartmentRequestScope:
    return DepartmentRequestScope(DepartmentScope(authority.department_id))


def _enqueue(factory, authority: Authority, *, apply: bool, **overrides):
    values = {
        "source_bundle_id": authority.source_id,
        "training_job_id": authority.training_job_id,
        "expected_source_version": authority.source_version,
        "expected_training_job_version": authority.training_job_version,
        "confirm_declared_training_association": True,
        "apply": apply,
        "code_revision": authority.code_revision,
    }
    values.update(overrides)
    with factory.begin() as session:
        return enqueue_adapter_registry(session, _principal(authority), _scope(authority), **values)


def _claim(factory, authority: Authority) -> ClaimedAdapter:
    _enqueue(factory, authority, apply=True)
    claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    return claim


def test_real_dry_run_is_read_only(factory, authority: Authority) -> None:
    result = _enqueue(factory, authority, apply=False)
    assert result.eligible and not result.applied and result.adapter_id is None
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterRegistryAttempt.id)).where(
                    AdapterRegistryAttempt.department_id == authority.department_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(AdapterUpstreamDependency.id)).where(
                    AdapterUpstreamDependency.department_id == authority.department_id
                )
            )
            == 0
        )


def test_real_apply_persists_one_atomic_registry_lineage(factory, authority: Authority) -> None:
    result = _enqueue(factory, authority, apply=True)
    assert result.applied
    assert result.adapter_id is not None and result.registry_attempt_id is not None
    with factory() as session:
        source = session.get(AdapterImportSource, authority.source_id)
        assert source is not None and source.status == "claimed"
        assert (
            session.scalar(
                select(func.count(AdapterRegistryAttempt.id)).where(
                    AdapterRegistryAttempt.department_id == authority.department_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(AdapterUpstreamDependency.id)).where(
                    AdapterUpstreamDependency.department_id == authority.department_id
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.registry.enqueue",
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    ("role", "identity_status", "membership_status", "expired"),
    [
        ("system_admin", "active", "active", False),
        ("department_admin", "active", "active", False),
        ("instructor", "active", "active", False),
        ("student", "active", "active", False),
        ("viewer", "active", "active", False),
        ("department_admin", "suspended", "active", False),
        ("department_admin", "revoked", "active", False),
        ("department_admin", "active", "suspended", False),
        ("department_admin", "active", "revoked", False),
        ("department_admin", "active", "active", True),
    ],
)
def test_real_enqueue_authorization_matrix(
    factory,
    authority: Authority,
    role: str,
    identity_status: str,
    membership_status: str,
    expired: bool,
) -> None:
    with factory.begin() as session:
        actor = UserIdentity(
            issuer=authority.issuer,
            subject=f"{role}-{uuid4().hex}",
            status=identity_status,
        )
        session.add(actor)
        session.flush()
        session.add(
            Membership(
                user_id=actor.id,
                department_id=authority.department_id,
                role=role,
                status=membership_status,
                expires_at=datetime.now(UTC) - timedelta(seconds=1) if expired else None,
                created_by_user_id=authority.admin_id,
            )
        )
        principal = AuthenticatedPrincipal(subject=actor.subject, issuer=actor.issuer)
    request_scope = DepartmentRequestScope(DepartmentScope(authority.department_id))
    permitted = role in {"system_admin", "department_admin"} and identity_status == "active"
    permitted = permitted and membership_status == "active" and not expired
    if permitted:
        with factory.begin() as session:
            result = enqueue_adapter_registry(
                session,
                principal,
                request_scope,
                source_bundle_id=authority.source_id,
                training_job_id=authority.training_job_id,
                expected_source_version=authority.source_version,
                expected_training_job_version=authority.training_job_version,
                confirm_declared_training_association=True,
                apply=True,
                code_revision=authority.code_revision,
            )
        assert result.applied
    else:
        with pytest.raises(ServiceError) as error:
            with factory.begin() as session:
                enqueue_adapter_registry(
                    session,
                    principal,
                    request_scope,
                    source_bundle_id=authority.source_id,
                    training_job_id=authority.training_job_id,
                    expected_source_version=authority.source_version,
                    expected_training_job_version=authority.training_job_version,
                    confirm_declared_training_association=True,
                    apply=True,
                    code_revision=authority.code_revision,
                )
        assert error.value.status_code in {403, 404, 409}


def test_real_system_admin_has_no_cross_department_bypass(factory, authority: Authority) -> None:
    with factory.begin() as session:
        foreign = Department(
            slug=f"foreign-{uuid4().hex[:12]}", display_name="Foreign", status="active"
        )
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
    with pytest.raises(ServiceError) as error:
        with factory.begin() as session:
            enqueue_adapter_registry(
                session,
                _principal(authority),
                DepartmentRequestScope(DepartmentScope(foreign_id)),
                source_bundle_id=authority.source_id,
                training_job_id=authority.training_job_id,
                expected_source_version=authority.source_version,
                expected_training_job_version=authority.training_job_version,
                confirm_declared_training_association=True,
                apply=True,
                code_revision=authority.code_revision,
            )
    assert error.value.status_code == 403


@pytest.mark.parametrize("field", ["source_version", "training_job_version"])
def test_real_enqueue_rejects_stale_authority(factory, authority: Authority, field: str) -> None:
    expected = (
        authority.source_version + 1
        if field == "source_version"
        else authority.training_job_version + 1
    )
    key = f"expected_{field}"
    with pytest.raises(ServiceError):
        _enqueue(factory, authority, apply=True, **{key: expected})
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterRegistryAttempt.id)).where(
                    AdapterRegistryAttempt.department_id == authority.department_id
                )
            )
            == 0
        )


def test_real_claim_and_terminal_failure_are_durable(factory, authority: Authority) -> None:
    claim = _claim(factory, authority)
    assert claim.department_id == authority.department_id
    terminal_failure(factory, claim, "adapter_registry_publication_failed")
    with factory() as session:
        row = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert row is not None and row.status == "failed" and row.error_code is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.registry.complete",
                )
            )
            == 0
        )


def test_real_claim_renew_revalidates_the_live_server_time_lease(
    factory, authority: Authority
) -> None:
    claim = _claim(factory, authority)
    renewed = renew_adapter_lease(factory, claim, 30)
    assert renewed.claim_token == claim.claim_token
    with factory() as session:
        adapter = session.get(Adapter, claim.id)
        assert adapter is not None
        assert adapter.worker_id == claim.worker_id
        assert adapter.claim_token == claim.claim_token
        assert adapter.lease_expires_at is not None


def test_real_expired_claim_reclaims_exact_prior_attempt(factory, authority: Authority) -> None:
    claim = _claim(factory, authority)
    with factory.begin() as session:
        row = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert row is not None
        row.status = "staged"
        row.ownership_manifest = {"attempt": str(claim.publication_attempt_id)}
        row.staged_at = datetime.now(UTC)
        row.version = 3
        adapter = session.get(Adapter, claim.id)
        assert adapter is not None
        adapter.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    replacement = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    assert replacement is not None
    assert replacement.stale_publication_attempt_id == claim.publication_attempt_id
    with factory() as session:
        prior = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert prior is not None and prior.status == "reclaimed"
        assert prior.finished_at is not None
        assert (
            session.scalar(
                select(func.count(AdapterRegistryAttempt.id)).where(
                    AdapterRegistryAttempt.adapter_id == claim.id
                )
            )
            == 2
        )


def test_real_claim_refusal_does_not_fabricate_replacement(factory, authority: Authority) -> None:
    claim = _claim(factory, authority)
    with factory.begin() as session:
        row = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert row is not None
        row.version = 99
        adapter = session.get(Adapter, claim.id)
        assert adapter is not None
        adapter.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(AdapterRegistryQueueError, match="claim_lost"):
        claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterRegistryAttempt.id)).where(
                    AdapterRegistryAttempt.adapter_id == claim.id
                )
            )
            == 1
        )


def test_real_purge_reservations_fence_enqueue(factory, authority: Authority) -> None:
    with factory.begin() as session:
        operation = TrainingJobArtifactOperation(
            department_id=authority.department_id,
            requested_by_user_id=authority.admin_id,
            limit_value=1,
            retention_days=30,
            operation_type="purge",
            status="registered",
        )
        session.add(operation)
        session.flush()
        session.add(
            TrainingJobPurgeReservation(
                operation_id=operation.id,
                department_id=authority.department_id,
                training_job_id=authority.training_job_id,
                expected_job_version=authority.training_job_version,
                expected_review_status="archived",
                retention_anchor_at=datetime.now(UTC),
                retention_days=30,
                authoritative_publication_attempt_id=authority.training_publication_attempt_id,
                authoritative_manifest={},
                tombstone_operation_id=operation.id,
            )
        )
    with pytest.raises(ServiceError):
        _enqueue(factory, authority, apply=True)

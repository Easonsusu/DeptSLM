"""Server-time leased Phase 10 dataset construction with no inference capability."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.models import (
    Department,
    Membership,
    PersistentAuditEvent,
    SftDatasetBuild,
    SftSourceBundle,
)
from app.sft_artifacts import DATASET_FILES, SftArtifactError, SftArtifactStore
from app.sft_authority import SftSourceAuthorityError, validate_source_authority
from app.sft_domain import (
    DATASET_ARTIFACT_CONTRACT_VERSION,
    EXAMPLE_CONTRACT_VERSION,
    NORMALIZATION_VERSION,
    SPLIT_VERSION,
    VALIDATION_RATIO,
    SftContractError,
    canonical_json_bytes,
    dataset_record,
    parse_source_bundle,
    provenance_record,
    split_examples,
)


class SftQueueError(RuntimeError):
    def __init__(self, code: str = "database_unavailable") -> None:
        self.code = (
            code
            if code
            in {
                "source_artifact_missing",
                "source_artifact_mismatch",
                "source_contract_invalid",
                "source_authority_changed",
                "department_unavailable",
                "requester_unauthorized",
                "dataset_publication_failed",
                "claim_lost",
                "cancelled",
                "worker_shutdown",
                "database_unavailable",
            }
            else "database_unavailable"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ClaimedSftBuild:
    id: UUID
    department_id: UUID
    source_bundle_id: UUID
    requested_by_user_id: UUID
    worker_id: UUID
    claim_token: UUID
    publication_attempt_id: UUID
    code_revision: str
    attempt_number: int
    stale_publication_attempt_id: UUID | None


def claim_next(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int, code_revision: str
) -> ClaimedSftBuild | None:
    try:
        with factory() as session, session.begin():
            row = session.execute(
                select(SftDatasetBuild)
                .where(
                    SftDatasetBuild.code_revision == code_revision,
                    or_(
                        SftDatasetBuild.status == "queued",
                        (SftDatasetBuild.status == "running")
                        & (SftDatasetBuild.lease_expires_at <= func.clock_timestamp()),
                    ),
                )
                .order_by(SftDatasetBuild.created_at, SftDatasetBuild.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            department = session.execute(
                select(Department).where(Department.id == row.department_id).with_for_update()
            ).scalar_one_or_none()
            if department is None or department.status != "active":
                _terminal_failure(
                    row, session.scalar(select(func.clock_timestamp())), "department_unavailable"
                )
                return None
            stale_attempt = row.publication_attempt_id if row.status == "running" else None
            now = session.scalar(select(func.clock_timestamp()))
            if stale_attempt is not None:
                row.attempt_number += 1
            row.status = "running"
            row.review_status = "not_ready"
            row.worker_id = worker_id
            row.claim_token = uuid4()
            row.publication_attempt_id = uuid4()
            row.claimed_at = now
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.started_at = row.started_at or now
            row.finished_at = None
            row.error_code = None
            row.version += 1
            session.flush()
            return ClaimedSftBuild(
                id=row.id,
                department_id=row.department_id,
                source_bundle_id=row.source_bundle_id,
                requested_by_user_id=row.requested_by_user_id,
                worker_id=worker_id,
                claim_token=row.claim_token,
                publication_attempt_id=row.publication_attempt_id,
                code_revision=row.code_revision,
                attempt_number=row.attempt_number,
                stale_publication_attempt_id=stale_attempt,
            )
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def require_live_claim(factory: sessionmaker[Session], job: ClaimedSftBuild) -> None:
    try:
        with factory() as session:
            row = session.execute(
                select(SftDatasetBuild.cancellation_requested_at).where(
                    *_owned(job), _live(), *_contract(job)
                )
            ).one_or_none()
            if row is None:
                raise SftQueueError("claim_lost")
            if row.cancellation_requested_at is not None:
                raise SftQueueError("cancelled")
    except SftQueueError:
        raise
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def renew_lease(factory: sessionmaker[Session], job: ClaimedSftBuild, lease_seconds: int) -> None:
    try:
        with factory.begin() as session:
            result = session.execute(
                update(SftDatasetBuild)
                .where(
                    *_owned(job),
                    *_contract(job),
                    _live(),
                    SftDatasetBuild.cancellation_requested_at.is_(None),
                )
                .values(
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                    version=SftDatasetBuild.version + 1,
                )
            )
            if result.rowcount != 1:
                raise SftQueueError("claim_lost")
    except SftQueueError:
        raise
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def process_build(
    factory: sessionmaker[Session], data_dir, job: ClaimedSftBuild, *, lease_seconds: int
) -> None:
    """Build exact JSONL artifacts with rechecks before external mutation and final success."""

    scope = DepartmentScope(job.department_id)
    store = SftArtifactStore(data_dir)
    staged = None
    try:
        if job.stale_publication_attempt_id is not None:
            require_live_claim(factory, job)
            store.remove_owned_dataset_stage(scope, job.id, job.stale_publication_attempt_id)
            require_live_claim(factory, job)
        source = _read_and_validate_source(factory, store, scope, job)
        require_live_claim(factory, job)
        train, validation = split_examples(source, build_id=job.id)
        train_bytes = _lines(dataset_record(item) for item in train)
        validation_bytes = _lines(dataset_record(item) for item in validation)
        provenance_bytes = _lines(
            [
                *(provenance_record(item, split="train") for item in train),
                *(provenance_record(item, split="validation") for item in validation),
            ]
        )
        manifest = _manifest(
            job, train_bytes, validation_bytes, provenance_bytes, len(train), len(validation)
        )
        require_live_claim(factory, job)
        staged = store.stage_dataset(
            scope,
            job.id,
            job.publication_attempt_id,
            manifest=canonical_json_bytes(manifest) + b"\n",
            train=train_bytes,
            validation=validation_bytes,
            provenance=provenance_bytes,
        )
        require_live_claim(factory, job)
        published = store.publish(staged, allowlist=DATASET_FILES)
        require_live_claim(factory, job)
        _complete(
            factory,
            job,
            published,
            source_chunk_ids={
                chunk_id for example in source.examples for chunk_id in example.source_chunk_ids
            },
            train_count=len(train),
            validation_count=len(validation),
        )
    except SftQueueError as error:
        _fail_or_cancel(factory, job, error.code)
    except SftContractError as error:
        _fail_or_cancel(factory, job, error.code)
    except SftArtifactError as error:
        _fail_or_cancel(factory, job, error.code)
    except Exception:
        _fail_or_cancel(factory, job, "dataset_publication_failed")


def _read_and_validate_source(
    factory: sessionmaker[Session],
    store: SftArtifactStore,
    scope: DepartmentScope,
    job: ClaimedSftBuild,
):
    try:
        with factory() as session:
            source = session.execute(
                select(SftSourceBundle).where(
                    SftSourceBundle.id == job.source_bundle_id,
                    SftSourceBundle.department_id == job.department_id,
                    SftSourceBundle.status == "active",
                )
            ).scalar_one_or_none()
            requester = session.execute(
                select(Membership).where(
                    Membership.user_id == job.requested_by_user_id,
                    Membership.department_id == job.department_id,
                    Membership.status == "active",
                )
            ).scalar_one_or_none()
            if source is None:
                raise SftQueueError("source_authority_changed")
            if requester is None or requester.role not in {
                "system_admin",
                "department_admin",
                "instructor",
            }:
                raise SftQueueError("requester_unauthorized")
            manifest_raw, examples_raw = store.read_source(scope, job.source_bundle_id)
            parsed = parse_source_bundle(manifest_raw, examples_raw)
            if (
                parsed.department_id != job.department_id
                or parsed.source_bundle_id != job.source_bundle_id
                or hashlib.sha256(manifest_raw).hexdigest() != source.manifest_sha256
                or parsed.examples_sha256 != source.examples_sha256
                or parsed.examples_byte_size != source.examples_byte_size
                or len(parsed.examples) != source.example_count
                or parsed.group_count != source.group_count
                or parsed.source_reference_count != source.source_reference_count
            ):
                raise SftQueueError("source_artifact_mismatch")
            validate_source_authority(
                session,
                job.department_id,
                {chunk_id for example in parsed.examples for chunk_id in example.source_chunk_ids},
            )
            return parsed
    except SftQueueError:
        raise
    except SftArtifactError as error:
        raise SftQueueError("source_artifact_missing") from error
    except SftContractError as error:
        raise SftQueueError("source_contract_invalid") from error
    except SftSourceAuthorityError as error:
        raise SftQueueError("source_authority_changed") from error
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def _complete(
    factory: sessionmaker[Session],
    job: ClaimedSftBuild,
    published,
    *,
    source_chunk_ids: set[UUID],
    train_count: int,
    validation_count: int,
) -> None:
    try:
        files = dict(published.files)
        with factory.begin() as session:
            row = session.execute(
                select(SftDatasetBuild)
                .where(*_owned(job), *_contract(job), _live())
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or row.cancellation_requested_at is not None:
                raise SftQueueError("claim_lost")
            source = session.execute(
                select(SftSourceBundle)
                .where(
                    SftSourceBundle.id == job.source_bundle_id,
                    SftSourceBundle.department_id == job.department_id,
                    SftSourceBundle.status == "active",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if source is None:
                raise SftQueueError("source_authority_changed")
            validate_source_authority(session, job.department_id, source_chunk_ids)
            now = session.scalar(select(func.clock_timestamp()))
            row.status = "succeeded"
            row.review_status = "pending"
            row.finished_at = now
            row.lease_expires_at = None
            row.worker_id = None
            row.claim_token = None
            row.train_example_count = train_count
            row.validation_example_count = validation_count
            row.result_manifest_sha256 = files["manifest.json"].sha256
            row.train_sha256 = files["train.jsonl"].sha256
            row.train_byte_size = files["train.jsonl"].byte_size
            row.validation_sha256 = files["validation.jsonl"].sha256
            row.validation_byte_size = files["validation.jsonl"].byte_size
            row.provenance_sha256 = files["provenance.jsonl"].sha256
            row.provenance_byte_size = files["provenance.jsonl"].byte_size
            row.version += 1
            session.add(
                PersistentAuditEvent(
                    actor_subject="service:sft-dataset-builder",
                    actor_user_id=None,
                    department_id=job.department_id,
                    action="sft.build.complete",
                    resource_type="sft_dataset_build",
                    resource_id=str(job.id),
                    result="allowed",
                    reason_code="mutation_applied",
                )
            )
            session.flush()
    except SftQueueError:
        raise
    except SftSourceAuthorityError as error:
        raise SftQueueError("source_authority_changed") from error
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def _fail_or_cancel(factory: sessionmaker[Session], job: ClaimedSftBuild, code: str) -> bool:
    try:
        with factory.begin() as session:
            row = session.execute(
                select(SftDatasetBuild).where(*_owned(job), _live()).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return False
            now = session.scalar(select(func.clock_timestamp()))
            row.status = (
                "cancelled"
                if code == "cancelled" or row.cancellation_requested_at is not None
                else "failed"
            )
            row.error_code = "cancelled" if row.status == "cancelled" else code
            row.finished_at = now
            row.lease_expires_at = None
            row.worker_id = None
            row.claim_token = None
            row.version += 1
            return True
    except SQLAlchemyError:
        return False


def _owned(job: ClaimedSftBuild):
    return (
        SftDatasetBuild.id == job.id,
        SftDatasetBuild.department_id == job.department_id,
        SftDatasetBuild.source_bundle_id == job.source_bundle_id,
        SftDatasetBuild.status == "running",
        SftDatasetBuild.worker_id == job.worker_id,
        SftDatasetBuild.claim_token == job.claim_token,
        SftDatasetBuild.publication_attempt_id == job.publication_attempt_id,
    )


def _live():
    return SftDatasetBuild.lease_expires_at > func.clock_timestamp()


def _contract(job: ClaimedSftBuild):
    return (
        SftDatasetBuild.code_revision == job.code_revision,
        SftDatasetBuild.artifact_contract_version == DATASET_ARTIFACT_CONTRACT_VERSION,
        SftDatasetBuild.example_contract_version == EXAMPLE_CONTRACT_VERSION,
        SftDatasetBuild.normalization_version == NORMALIZATION_VERSION,
        SftDatasetBuild.split_version == SPLIT_VERSION,
        SftDatasetBuild.validation_ratio == VALIDATION_RATIO,
    )


def _terminal_failure(row: SftDatasetBuild, now, code: str) -> None:
    row.status = "failed"
    row.error_code = code
    row.finished_at = now
    row.lease_expires_at = None
    row.worker_id = None
    row.claim_token = None
    row.version += 1


def _lines(values) -> bytes:
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


def _manifest(
    job, train: bytes, validation: bytes, provenance: bytes, train_count: int, validation_count: int
):
    def digest(value: bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "byte_size": len(value)}

    return {
        "artifact_contract_version": DATASET_ARTIFACT_CONTRACT_VERSION,
        "department_id": str(job.department_id),
        "source_bundle_id": str(job.source_bundle_id),
        "build_id": str(job.id),
        "publication_attempt_id": str(job.publication_attempt_id),
        "attempt_number": job.attempt_number,
        "code_revision": job.code_revision,
        "normalization_version": NORMALIZATION_VERSION,
        "example_contract_version": EXAMPLE_CONTRACT_VERSION,
        "split_version": SPLIT_VERSION,
        "validation_ratio": str(VALIDATION_RATIO),
        "source_example_count": train_count + validation_count,
        "source_group_count": len({item.group_id for item in [*train, *validation]}),
        "source_reference_count": sum(len(item.source_chunk_ids) for item in [*train, *validation]),
        "train_example_count": train_count,
        "validation_example_count": validation_count,
        "files": {
            "train.jsonl": digest(train),
            "validation.jsonl": digest(validation),
            "provenance.jsonl": digest(provenance),
        },
    }

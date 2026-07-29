"""Leased Phase 11 configuration-bundle generation; never executes training."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.models import (
    Department,
    Membership,
    PersistentAuditEvent,
    SftDatasetBuild,
    TrainingJob,
    TrainingJobAttempt,
)
from app.sft_artifacts import (
    DATASET_FILES,
    SftArtifactError,
    SftArtifactStore,
    SftFinalArtifactVerification,
)
from app.training_job_domain import TRAINING_JOB_FILES, TrainingJobContractError, parse_job_manifest
from app.training_job_supervision import run_training_job_child


class TrainingJobQueueError(RuntimeError):
    def __init__(self, code: str = "database_unavailable") -> None:
        self.code = (
            code
            if code
            in {
                "dataset_unavailable",
                "dataset_artifact_mismatch",
                "dataset_contract_invalid",
                "dataset_record_invalid",
                "dataset_authority_changed",
                "department_unavailable",
                "requester_unauthorized",
                "training_job_publication_failed",
                "claim_lost",
                "cancelled",
                "worker_shutdown",
                "worker_timeout",
                "database_unavailable",
            }
            else "training_job_publication_failed"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ClaimedTrainingJob:
    id: UUID
    department_id: UUID
    dataset_build_id: UUID
    requested_by_user_id: UUID
    profile_id: str
    publication_attempt_id: UUID
    execution_scope_id: UUID
    attempt_number: int
    worker_id: UUID
    claim_token: UUID
    code_revision: str
    dataset_build_version: int
    dataset_manifest_sha256: str
    dataset_source_bundle_id: UUID
    dataset_status: str
    dataset_review_status: str
    dataset_publication_attempt_id: UUID
    dataset_publication_attempt_number: int
    dataset_code_revision: str
    dataset_train_sha256: str
    dataset_train_byte_size: int
    dataset_validation_sha256: str
    dataset_validation_byte_size: int
    dataset_provenance_sha256: str
    dataset_provenance_byte_size: int
    dataset_train_example_count: int
    dataset_validation_example_count: int
    dataset_source_example_count: int
    dataset_source_group_count: int
    dataset_source_reference_count: int
    dataset_artifact_contract_version: str
    dataset_example_contract_version: str
    dataset_normalization_version: str
    dataset_split_version: str
    dataset_rights_attested: bool
    evaluation_contamination_reviewed: bool
    stale_publication_attempt_id: UUID | None


class _Lease:
    def __init__(
        self,
        factory: sessionmaker[Session],
        job: ClaimedTrainingJob,
        *,
        lease_seconds: int,
        operation_seconds: int,
        should_stop: Callable[[], bool],
    ) -> None:
        self.factory = factory
        self.job = job
        self.lease_seconds = lease_seconds
        self.deadline = time.monotonic() + operation_seconds
        self.should_stop = should_stop
        self.last_heartbeat = 0.0

    def _local_check(self) -> None:
        if self.should_stop():
            raise TrainingJobQueueError("worker_shutdown")
        if time.monotonic() >= self.deadline:
            raise TrainingJobQueueError("worker_timeout")

    def __call__(self) -> None:
        self._local_check()
        if time.monotonic() >= self.last_heartbeat + max(0.1, self.lease_seconds / 3):
            renew_lease(self.factory, self.job, self.lease_seconds)
            self.last_heartbeat = time.monotonic()

    def renew_before_final_transaction(self) -> None:
        """Renew once before final locking; never from inside that transaction."""

        self._local_check()
        renew_lease(self.factory, self.job, self.lease_seconds)
        self.last_heartbeat = time.monotonic()

    def final_transaction_check(self) -> None:
        """No-I/O shutdown/deadline check for the already-open final transaction."""

        self._local_check()


def claim_next(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int, code_revision: str
) -> ClaimedTrainingJob | None:
    """Claim one queued or expired job with PostgreSQL server time and SKIP LOCKED."""

    try:
        with factory.begin() as session:
            row = session.execute(
                select(TrainingJob)
                .where(
                    TrainingJob.code_revision == code_revision,
                    (TrainingJob.status == "queued")
                    | (
                        (TrainingJob.status == "running")
                        & (TrainingJob.lease_expires_at <= func.clock_timestamp())
                    ),
                )
                .order_by(TrainingJob.created_at, TrainingJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            now = session.scalar(select(func.clock_timestamp()))
            department = session.execute(
                select(Department)
                .where(Department.id == row.department_id, Department.status == "active")
                .with_for_update()
            ).scalar_one_or_none()
            requester = session.execute(
                select(Membership)
                .where(
                    Membership.user_id == row.requested_by_user_id,
                    Membership.department_id == row.department_id,
                    Membership.status == "active",
                    Membership.role.in_(("system_admin", "department_admin")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if department is None or requester is None:
                _terminal_claim_failure(
                    session,
                    row,
                    now,
                    "department_unavailable" if department is None else "requester_unauthorized",
                )
                return None
            stale_attempt = row.publication_attempt_id if row.status == "running" else None
            if stale_attempt is not None:
                previous = session.execute(
                    select(TrainingJobAttempt)
                    .where(
                        TrainingJobAttempt.department_id == row.department_id,
                        TrainingJobAttempt.training_job_id == row.id,
                        TrainingJobAttempt.publication_attempt_id == stale_attempt,
                        TrainingJobAttempt.status.in_(
                            ("registered", "running", "staged", "published")
                        ),
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if previous is None:
                    raise TrainingJobQueueError("database_unavailable")
                previous.status = "reclaimed"
                previous.finished_at = session.scalar(select(func.clock_timestamp()))
                previous.version += 1
                row.attempt_number += 1
            claim_token, publication_attempt_id = uuid4(), uuid4()
            execution_scope_id = uuid4()
            row.status = "running"
            row.worker_id = worker_id
            row.claim_token = claim_token
            row.claimed_at = now
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.started_at = row.started_at or now
            row.publication_attempt_id = publication_attempt_id
            row.execution_scope_id = execution_scope_id
            row.cancellation_requested_at = None
            row.error_code = None
            row.version += 1
            session.add(
                TrainingJobAttempt(
                    department_id=row.department_id,
                    training_job_id=row.id,
                    attempt_number=row.attempt_number,
                    publication_attempt_id=publication_attempt_id,
                    code_revision=row.code_revision,
                    status="running",
                    claimed_at=now,
                )
            )
            return ClaimedTrainingJob(
                id=row.id,
                department_id=row.department_id,
                dataset_build_id=row.dataset_build_id,
                requested_by_user_id=row.requested_by_user_id,
                profile_id=row.profile_id,
                publication_attempt_id=publication_attempt_id,
                execution_scope_id=execution_scope_id,
                attempt_number=row.attempt_number,
                worker_id=worker_id,
                claim_token=claim_token,
                code_revision=row.code_revision,
                dataset_build_version=row.dataset_build_version,
                dataset_manifest_sha256=row.dataset_manifest_sha256,
                dataset_source_bundle_id=row.dataset_source_bundle_id,
                dataset_status=row.dataset_status,
                dataset_review_status=row.dataset_review_status,
                dataset_publication_attempt_id=row.dataset_publication_attempt_id,
                dataset_publication_attempt_number=row.dataset_publication_attempt_number,
                dataset_code_revision=row.dataset_code_revision,
                dataset_train_sha256=row.dataset_train_sha256,
                dataset_train_byte_size=row.dataset_train_byte_size,
                dataset_validation_sha256=row.dataset_validation_sha256,
                dataset_validation_byte_size=row.dataset_validation_byte_size,
                dataset_provenance_sha256=row.dataset_provenance_sha256,
                dataset_provenance_byte_size=row.dataset_provenance_byte_size,
                dataset_train_example_count=row.dataset_train_example_count,
                dataset_validation_example_count=row.dataset_validation_example_count,
                dataset_source_example_count=row.dataset_source_example_count,
                dataset_source_group_count=row.dataset_source_group_count,
                dataset_source_reference_count=row.dataset_source_reference_count,
                dataset_artifact_contract_version=row.dataset_artifact_contract_version,
                dataset_example_contract_version=row.dataset_example_contract_version,
                dataset_normalization_version=row.dataset_normalization_version,
                dataset_split_version=row.dataset_split_version,
                dataset_rights_attested=row.dataset_rights_attested,
                evaluation_contamination_reviewed=row.evaluation_contamination_reviewed,
                stale_publication_attempt_id=stale_attempt,
            )
    except TrainingJobQueueError:
        raise
    except SQLAlchemyError as error:
        raise TrainingJobQueueError() from error


def _terminal_claim_failure(session: Session, row: TrainingJob, now: datetime, code: str) -> None:
    if row.status == "running" and row.publication_attempt_id is not None:
        previous = session.execute(
            select(TrainingJobAttempt)
            .where(
                TrainingJobAttempt.department_id == row.department_id,
                TrainingJobAttempt.training_job_id == row.id,
                TrainingJobAttempt.publication_attempt_id == row.publication_attempt_id,
                TrainingJobAttempt.status.in_(("registered", "running", "staged", "published")),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if previous is None:
            raise TrainingJobQueueError("database_unavailable")
        previous.status = "reclaimed"
        previous.finished_at = now
        previous.version += 1
        row.attempt_number += 1
    session.add(
        TrainingJobAttempt(
            department_id=row.department_id,
            training_job_id=row.id,
            attempt_number=row.attempt_number,
            publication_attempt_id=uuid4(),
            code_revision=row.code_revision,
            status="failed",
            finished_at=now,
        )
    )
    row.status = "failed"
    row.review_status = "not_ready"
    row.worker_id = None
    row.claim_token = None
    row.lease_expires_at = None
    row.finished_at = now
    row.error_code = code
    row.version += 1


def renew_lease(
    factory: sessionmaker[Session], job: ClaimedTrainingJob, lease_seconds: int
) -> None:
    try:
        with factory.begin() as session:
            now = session.scalar(select(func.clock_timestamp()))
            result = session.execute(
                update(TrainingJob)
                .where(
                    *_owned(job),
                    TrainingJob.lease_expires_at > now,
                    TrainingJob.cancellation_requested_at.is_(None),
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    version=TrainingJob.version + 1,
                )
            )
            if result.rowcount != 1:
                raise TrainingJobQueueError("claim_lost")
    except TrainingJobQueueError:
        raise
    except SQLAlchemyError as error:
        raise TrainingJobQueueError() from error


def process_training_job(
    factory: sessionmaker[Session],
    data_dir: Path,
    job: ClaimedTrainingJob,
    *,
    lease_seconds: int,
    operation_seconds: int = 120,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Generate a Phase 11 bundle from one approved, retained Phase 10 dataset."""

    scope = DepartmentScope(job.department_id)
    source: SftFinalArtifactVerification | None = None
    staged = None
    preverified = None
    final: SftFinalArtifactVerification | None = None

    lease_guard = _Lease(
        factory,
        job,
        lease_seconds=lease_seconds,
        operation_seconds=operation_seconds,
        should_stop=should_stop,
    )

    def guard() -> _Lease:
        lease_guard()
        return lease_guard

    try:
        if job.stale_publication_attempt_id is not None:
            _cleanup_stale_attempt(
                factory, data_dir, scope, job, job.stale_publication_attempt_id, guard
            )
        _load_eligible_dataset(factory, job, guard())
        with SftArtifactStore(data_dir) as store:
            source_guard = guard()
            source = store.open_retained_final(
                scope,
                job.dataset_build_id,
                category="dataset",
                attempt_id=job.dataset_publication_attempt_id,
                allowlist=DATASET_FILES,
                expected=_dataset_manifest(job),
                checkpoint=source_guard,
            )
            source_guard()
            stage_guard = guard()
            staged = store.prepare_training_job_stage(scope, job.id, job.publication_attempt_id)
            if staged.stage_fd is None or source.artifact.stage_fd is None:
                raise TrainingJobQueueError("training_job_publication_failed")
            stage_guard()
            child_guard = guard()
            result = run_training_job_child(
                timeout_seconds=operation_seconds,
                heartbeat_seconds=max(1, lease_seconds),
                should_stop=should_stop,
                heartbeat=child_guard,
                error=TrainingJobQueueError,
                request=_child_request(source, staged.stage_fd, job),
                pass_fds=(
                    source.descriptor("manifest.json")[0],
                    source.descriptor("train.jsonl")[0],
                    source.descriptor("validation.jsonl")[0],
                    source.descriptor("provenance.jsonl")[0],
                    staged.stage_fd,
                ),
            )
            child_guard()
            manifest, train_count, validation_count = _validate_child_result(result, job)
            record_guard = guard()
            _record_attempt_manifest(factory, job, manifest)
            record_guard()
            pre_guard = guard()
            preverified = store.preverify_staged(
                staged, allowlist=TRAINING_JOB_FILES, expected=manifest, checkpoint=pre_guard
            )
            staged = None
            pre_guard()
            marker_guard = guard()
            store.transition_stage_marker(preverified, checkpoint=marker_guard)
            marker_guard()
            rename_guard = guard()
            store.rename_preverified_stage(preverified, checkpoint=rename_guard)
            rename_guard()
            post_guard = guard()
            final = store.verify_preverified_final(preverified, checkpoint=post_guard)
            preverified = None
            post_guard()
            _mark_attempt_published(factory, job)
            _complete(
                factory,
                job,
                source,
                final,
                manifest,
                train_count,
                validation_count,
                lease_guard,
            )
    except TrainingJobQueueError as error:
        _fail_or_cancel(factory, job, error.code)
    except TrainingJobContractError as error:
        _fail_or_cancel(factory, job, error.code)
    except SftArtifactError as error:
        _fail_or_cancel(factory, job, error.code)
    except Exception:
        _fail_or_cancel(factory, job, "training_job_publication_failed")
    finally:
        if source is not None:
            source.close()
        if final is not None:
            final.close()
        elif preverified is not None:
            preverified.close()
        elif staged is not None:
            staged.close()


def _load_eligible_dataset(
    factory: sessionmaker[Session], job: ClaimedTrainingJob, check: _Lease
) -> SftDatasetBuild:
    try:
        with factory() as session:
            row = session.execute(
                select(SftDatasetBuild).where(
                    SftDatasetBuild.id == job.dataset_build_id,
                    SftDatasetBuild.department_id == job.department_id,
                    SftDatasetBuild.status == "succeeded",
                    SftDatasetBuild.review_status == "approved",
                    SftDatasetBuild.purged_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is None or not _dataset_matches(job, row):
                raise TrainingJobQueueError("dataset_authority_changed")
            check()
            return row
    except TrainingJobQueueError:
        raise
    except SQLAlchemyError as error:
        raise TrainingJobQueueError() from error


def _child_request(
    source: SftFinalArtifactVerification, stage_fd: int, job: ClaimedTrainingJob
) -> dict[str, object]:
    files = dict(source.files)
    required = {"manifest.json", "train.jsonl", "validation.jsonl", "provenance.jsonl"}
    if set(files) != required:
        raise TrainingJobQueueError("dataset_artifact_mismatch")
    manifest_fd, _manifest_identity = source.descriptor("manifest.json")
    train_fd, _train_identity = source.descriptor("train.jsonl")
    validation_fd, _validation_identity = source.descriptor("validation.jsonl")
    provenance_fd, _provenance_identity = source.descriptor("provenance.jsonl")
    if files["manifest.json"].sha256 != job.dataset_manifest_sha256:
        raise TrainingJobQueueError("dataset_artifact_mismatch")
    return {
        "manifest_fd": manifest_fd,
        "train_fd": train_fd,
        "validation_fd": validation_fd,
        "provenance_fd": provenance_fd,
        "stage_fd": stage_fd,
        "department_id": str(job.department_id),
        "training_job_id": str(job.id),
        "dataset_build_id": str(job.dataset_build_id),
        "publication_attempt_id": str(job.publication_attempt_id),
        "execution_scope_id": str(job.execution_scope_id),
        "attempt_number": job.attempt_number,
        "code_revision": job.code_revision,
        "dataset_build_version": job.dataset_build_version,
        "dataset_manifest_sha256": job.dataset_manifest_sha256,
        "dataset_source_bundle_id": str(job.dataset_source_bundle_id),
        "dataset_status": job.dataset_status,
        "dataset_review_status": job.dataset_review_status,
        "dataset_publication_attempt_id": str(job.dataset_publication_attempt_id),
        "dataset_publication_attempt_number": job.dataset_publication_attempt_number,
        "dataset_code_revision": job.dataset_code_revision,
        "dataset_artifact_contract_version": job.dataset_artifact_contract_version,
        "dataset_example_contract_version": job.dataset_example_contract_version,
        "dataset_normalization_version": job.dataset_normalization_version,
        "dataset_split_version": job.dataset_split_version,
        "profile_id": job.profile_id,
        "dataset_rights_attested": job.dataset_rights_attested,
        "evaluation_contamination_reviewed": job.evaluation_contamination_reviewed,
        "expected_manifest_sha256": files["manifest.json"].sha256,
        "expected_manifest_byte_size": files["manifest.json"].byte_size,
        "dataset_train_example_count": job.dataset_train_example_count,
        "dataset_validation_example_count": job.dataset_validation_example_count,
        "dataset_source_example_count": job.dataset_source_example_count,
        "dataset_source_group_count": job.dataset_source_group_count,
        "dataset_source_reference_count": job.dataset_source_reference_count,
        "expected_train_sha256": job.dataset_train_sha256,
        "expected_train_byte_size": job.dataset_train_byte_size,
        "expected_validation_sha256": job.dataset_validation_sha256,
        "expected_validation_byte_size": job.dataset_validation_byte_size,
        "expected_provenance_sha256": job.dataset_provenance_sha256,
        "expected_provenance_byte_size": job.dataset_provenance_byte_size,
    }


def _validate_child_result(
    result: object, job: ClaimedTrainingJob
) -> tuple[dict[str, object], int, int]:
    if not isinstance(result, dict) or set(result) != {
        "publication_manifest",
        "files",
        "train_count",
        "validation_count",
    }:
        raise TrainingJobQueueError("training_job_publication_failed")
    manifest = result["publication_manifest"]
    train_count, validation_count = result["train_count"], result["validation_count"]
    if (
        not isinstance(manifest, dict)
        or type(train_count) is not int
        or type(validation_count) is not int
    ):
        raise TrainingJobQueueError("training_job_publication_failed")
    if (
        train_count < 1
        or validation_count < 1
        or train_count != job.dataset_train_example_count
        or validation_count != job.dataset_validation_example_count
    ):
        raise TrainingJobQueueError("dataset_artifact_mismatch")
    if (
        manifest.get("training_job_id") != str(job.id)
        or manifest.get("dataset_build_id") != str(job.dataset_build_id)
        or manifest.get("publication_attempt_id") != str(job.publication_attempt_id)
    ):
        raise TrainingJobQueueError("training_job_publication_failed")
    parse_job_manifest(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return manifest, train_count, validation_count


def _record_attempt_manifest(
    factory: sessionmaker[Session], job: ClaimedTrainingJob, manifest: dict[str, object]
) -> None:
    try:
        with factory.begin() as session:
            row = session.execute(
                select(TrainingJob).where(*_owned(job), _live()).with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(TrainingJobAttempt)
                .where(
                    TrainingJobAttempt.department_id == job.department_id,
                    TrainingJobAttempt.training_job_id == job.id,
                    TrainingJobAttempt.publication_attempt_id == job.publication_attempt_id,
                    TrainingJobAttempt.status == "running",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or row.cancellation_requested_at is not None or attempt is None:
                raise TrainingJobQueueError("claim_lost")
            row.publication_manifest = manifest
            attempt.ownership_manifest = manifest
            attempt.status = "staged"
            attempt.staged_at = session.scalar(select(func.clock_timestamp()))
            attempt.version += 1
    except TrainingJobQueueError:
        raise
    except SQLAlchemyError as error:
        raise TrainingJobQueueError() from error


def _mark_attempt_published(factory: sessionmaker[Session], job: ClaimedTrainingJob) -> None:
    try:
        with factory.begin() as session:
            row = session.execute(
                select(TrainingJob).where(*_owned(job), _live()).with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(TrainingJobAttempt)
                .where(
                    TrainingJobAttempt.department_id == job.department_id,
                    TrainingJobAttempt.training_job_id == job.id,
                    TrainingJobAttempt.publication_attempt_id == job.publication_attempt_id,
                    TrainingJobAttempt.status == "staged",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or attempt is None or row.cancellation_requested_at is not None:
                raise TrainingJobQueueError("claim_lost")
            attempt.status = "published"
            attempt.published_at = session.scalar(select(func.clock_timestamp()))
            attempt.version += 1
    except TrainingJobQueueError:
        raise
    except SQLAlchemyError as error:
        raise TrainingJobQueueError() from error


def _complete(
    factory: sessionmaker[Session],
    job: ClaimedTrainingJob,
    source: SftFinalArtifactVerification,
    final: SftFinalArtifactVerification,
    manifest: dict[str, object],
    train_count: int,
    validation_count: int,
    guard: _Lease,
) -> None:
    try:
        files = dict(final.files)
        # This is the last renewable parent-operation checkpoint. The final
        # transaction itself must never open another connection to renew.
        guard.renew_before_final_transaction()
        with factory.begin() as session:
            guard.final_transaction_check()
            row = session.execute(
                select(TrainingJob).where(*_owned(job), _live()).with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(TrainingJobAttempt)
                .where(
                    TrainingJobAttempt.department_id == job.department_id,
                    TrainingJobAttempt.training_job_id == job.id,
                    TrainingJobAttempt.publication_attempt_id == job.publication_attempt_id,
                    TrainingJobAttempt.status == "published",
                )
                .with_for_update()
            ).scalar_one_or_none()
            current_dataset = session.execute(
                select(SftDatasetBuild)
                .where(
                    SftDatasetBuild.id == job.dataset_build_id,
                    SftDatasetBuild.department_id == job.department_id,
                    SftDatasetBuild.status == "succeeded",
                    SftDatasetBuild.review_status == "approved",
                    SftDatasetBuild.purged_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            department = session.execute(
                select(Department)
                .where(Department.id == job.department_id, Department.status == "active")
                .with_for_update()
            ).scalar_one_or_none()
            requester = session.execute(
                select(Membership)
                .where(
                    Membership.user_id == job.requested_by_user_id,
                    Membership.department_id == job.department_id,
                    Membership.status == "active",
                    Membership.role.in_(("system_admin", "department_admin")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if (
                row is None
                or attempt is None
                or current_dataset is None
                or department is None
                or requester is None
                or row.cancellation_requested_at is not None
                or not _dataset_matches(job, current_dataset)
            ):
                raise TrainingJobQueueError("dataset_authority_changed")
            if row.publication_manifest != manifest or attempt.ownership_manifest != manifest:
                raise TrainingJobQueueError("training_job_publication_failed")
            guard.final_transaction_check()
            now = session.scalar(select(func.clock_timestamp()))
            if row.lease_expires_at is None or row.lease_expires_at <= now:
                raise TrainingJobQueueError("claim_lost")
            source.recheck_identity()
            final.recheck_identity()
            row.status = "succeeded"
            row.review_status = "pending"
            row.finished_at = now
            row.worker_id = None
            row.claim_token = None
            row.lease_expires_at = None
            row.train_example_count = train_count
            row.validation_example_count = validation_count
            row.result_manifest_sha256 = files["manifest.json"].sha256
            row.training_config_sha256 = files["training.yaml"].sha256
            row.training_config_byte_size = files["training.yaml"].byte_size
            row.dataset_info_sha256 = files["dataset_info.json"].sha256
            row.dataset_info_byte_size = files["dataset_info.json"].byte_size
            row.train_sha256 = files["train.jsonl"].sha256
            row.train_byte_size = files["train.jsonl"].byte_size
            row.validation_sha256 = files["validation.jsonl"].sha256
            row.validation_byte_size = files["validation.jsonl"].byte_size
            row.version += 1
            attempt.status = "succeeded"
            attempt.finished_at = now
            attempt.version += 1
            session.add(
                PersistentAuditEvent(
                    actor_subject="service:training-job-worker",
                    actor_user_id=None,
                    department_id=job.department_id,
                    action="training.job.generate",
                    resource_type="training_job",
                    resource_id=str(job.id),
                    result="allowed",
                    reason_code="mutation_applied",
                )
            )
    except TrainingJobQueueError:
        raise
    except SQLAlchemyError as error:
        raise TrainingJobQueueError() from error


def _fail_or_cancel(factory: sessionmaker[Session], job: ClaimedTrainingJob, code: str) -> bool:
    try:
        with factory.begin() as session:
            row = session.execute(
                select(TrainingJob).where(*_owned(job), _live()).with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(TrainingJobAttempt)
                .where(
                    TrainingJobAttempt.department_id == job.department_id,
                    TrainingJobAttempt.training_job_id == job.id,
                    TrainingJobAttempt.publication_attempt_id == job.publication_attempt_id,
                    TrainingJobAttempt.status.in_(("running", "staged", "published")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or attempt is None:
                return False
            now = session.scalar(select(func.clock_timestamp()))
            terminal = (
                "cancelled"
                if code == "cancelled" or row.cancellation_requested_at is not None
                else "failed"
            )
            row.status = terminal
            row.review_status = "not_ready"
            row.finished_at = now
            row.worker_id = None
            row.claim_token = None
            row.lease_expires_at = None
            row.error_code = "cancelled" if terminal == "cancelled" else code
            row.version += 1
            attempt.status = terminal
            attempt.finished_at = now
            attempt.version += 1
            return True
    except SQLAlchemyError:
        return False


def _cleanup_stale_attempt(
    factory: sessionmaker[Session],
    data_dir: Path,
    scope: DepartmentScope,
    job: ClaimedTrainingJob,
    attempt_id: UUID,
    guard_factory: Callable[[], _Lease],
) -> None:
    """Only the replacement claim may clean the exact old attempt's surfaces."""

    manifest = None
    try:
        with factory() as session:
            previous = session.execute(
                select(TrainingJobAttempt).where(
                    TrainingJobAttempt.department_id == job.department_id,
                    TrainingJobAttempt.training_job_id == job.id,
                    TrainingJobAttempt.publication_attempt_id == attempt_id,
                    TrainingJobAttempt.status == "reclaimed",
                )
            ).scalar_one_or_none()
            if previous is None:
                raise TrainingJobQueueError("claim_lost")
            manifest = previous.ownership_manifest
        with SftArtifactStore(data_dir) as store:
            stage_guard = guard_factory()
            store.remove_owned_training_job_stage(scope, job.id, attempt_id, checkpoint=stage_guard)
            stage_guard()
            if isinstance(manifest, dict):
                final_guard = guard_factory()
                store.remove_owned_training_job_final(
                    scope, job.id, attempt_id, expected=manifest, checkpoint=final_guard
                )
                final_guard()
        with factory.begin() as session:
            previous = session.execute(
                select(TrainingJobAttempt)
                .where(
                    TrainingJobAttempt.department_id == job.department_id,
                    TrainingJobAttempt.training_job_id == job.id,
                    TrainingJobAttempt.publication_attempt_id == attempt_id,
                    TrainingJobAttempt.status == "reclaimed",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if previous is not None:
                previous.cleanup_confirmed_at = session.scalar(select(func.clock_timestamp()))
                previous.version += 1
    except TrainingJobQueueError:
        raise
    except SftArtifactError as error:
        raise TrainingJobQueueError(error.code) from error
    except SQLAlchemyError as error:
        raise TrainingJobQueueError() from error


def _owned(job: ClaimedTrainingJob):
    return (
        TrainingJob.id == job.id,
        TrainingJob.department_id == job.department_id,
        TrainingJob.status == "running",
        TrainingJob.worker_id == job.worker_id,
        TrainingJob.claim_token == job.claim_token,
        TrainingJob.publication_attempt_id == job.publication_attempt_id,
        TrainingJob.execution_scope_id == job.execution_scope_id,
    )


def _live():
    return TrainingJob.lease_expires_at > func.clock_timestamp()


def _dataset_matches(job: ClaimedTrainingJob, row: SftDatasetBuild) -> bool:
    return (
        row.source_bundle_id == job.dataset_source_bundle_id
        and row.status == job.dataset_status == "succeeded"
        and row.review_status == job.dataset_review_status == "approved"
        and row.version == job.dataset_build_version
        and row.result_manifest_sha256 == job.dataset_manifest_sha256
        and row.publication_attempt_id == job.dataset_publication_attempt_id
        and row.attempt_number == job.dataset_publication_attempt_number
        and row.code_revision == job.dataset_code_revision
        and row.train_sha256 == job.dataset_train_sha256
        and row.train_byte_size == job.dataset_train_byte_size
        and row.validation_sha256 == job.dataset_validation_sha256
        and row.validation_byte_size == job.dataset_validation_byte_size
        and row.provenance_sha256 == job.dataset_provenance_sha256
        and row.provenance_byte_size == job.dataset_provenance_byte_size
        and row.train_example_count == job.dataset_train_example_count
        and row.validation_example_count == job.dataset_validation_example_count
        and row.source_example_count == job.dataset_source_example_count
        and row.source_group_count == job.dataset_source_group_count
        and row.source_reference_count == job.dataset_source_reference_count
        and row.artifact_contract_version == job.dataset_artifact_contract_version
        and row.example_contract_version == job.dataset_example_contract_version
        and row.normalization_version == job.dataset_normalization_version
        and row.split_version == job.dataset_split_version
        and isinstance(row.publication_manifest, dict)
        and row.purged_at is None
    )


def _dataset_manifest(job: ClaimedTrainingJob) -> dict[str, object]:
    """Return the exact retained Phase 10 manifest expected from the job snapshot."""

    return {
        "artifact_contract_version": job.dataset_artifact_contract_version,
        "department_id": str(job.department_id),
        "source_bundle_id": str(job.dataset_source_bundle_id),
        "build_id": str(job.dataset_build_id),
        "publication_attempt_id": str(job.dataset_publication_attempt_id),
        "attempt_number": job.dataset_publication_attempt_number,
        "code_revision": job.dataset_code_revision,
        "normalization_version": job.dataset_normalization_version,
        "example_contract_version": job.dataset_example_contract_version,
        "split_version": job.dataset_split_version,
        "validation_ratio": "0.10",
        "source_example_count": job.dataset_source_example_count,
        "source_group_count": job.dataset_source_group_count,
        "source_reference_count": job.dataset_source_reference_count,
        "train_example_count": job.dataset_train_example_count,
        "validation_example_count": job.dataset_validation_example_count,
        "files": {
            "train.jsonl": {
                "sha256": job.dataset_train_sha256,
                "byte_size": job.dataset_train_byte_size,
            },
            "validation.jsonl": {
                "sha256": job.dataset_validation_sha256,
                "byte_size": job.dataset_validation_byte_size,
            },
            "provenance.jsonl": {
                "sha256": job.dataset_provenance_sha256,
                "byte_size": job.dataset_provenance_byte_size,
            },
        },
    }

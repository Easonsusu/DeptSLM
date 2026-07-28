"""Server-time leased Phase 10 dataset construction with no inference capability."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
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
    SftDatasetBuildAttempt,
    SftSourceBundle,
)
from app.sft_artifacts import (
    DATASET_FILES,
    ArtifactDigest,
    SftArtifactError,
    SftArtifactStore,
    SftFinalArtifactVerification,
    SftStagedArtifactVerification,
)
from app.sft_authority import (
    SftAuthorityMapping,
    SftSourceAuthorityError,
    validate_authority_selector,
    write_authority_mapping,
)
from app.sft_domain import (
    DATASET_ARTIFACT_CONTRACT_VERSION,
    EXAMPLE_CONTRACT_VERSION,
    NORMALIZATION_VERSION,
    SPLIT_VERSION,
    VALIDATION_RATIO,
    SftContractError,
)
from app.sft_supervision import SftChildOperation, run_claimed_operation

_SELECTOR_FILE = ".deptslm-selector.jsonl"


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
                "worker_timeout",
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
    stale_publication_manifest: dict[str, object] | None


class _LeaseCheckpoint:
    """Monotonic, cancellation-aware parent-operation lease coverage."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        job: ClaimedSftBuild,
        *,
        lease_seconds: int,
        operation_seconds: int,
        should_stop: Callable[[], bool],
    ) -> None:
        self.factory = factory
        self.job = job
        self.lease_seconds = lease_seconds
        self.should_stop = should_stop
        self.deadline = time.monotonic() + operation_seconds
        self._next_heartbeat = 0.0
        self._interval = max(0.1, min(15.0, lease_seconds / 3))

    def __call__(self) -> None:
        now = time.monotonic()
        if self.should_stop():
            raise SftQueueError("worker_shutdown")
        if now >= self.deadline:
            raise SftQueueError("worker_timeout")
        if now >= self._next_heartbeat:
            renew_lease(self.factory, self.job, self.lease_seconds)
            self._next_heartbeat = now + self._interval

    def final_check(self) -> None:
        """Check cancellation and deadline while a final transaction owns locks."""

        if self.should_stop():
            raise SftQueueError("worker_shutdown")
        if time.monotonic() >= self.deadline:
            raise SftQueueError("worker_timeout")


def claim_next(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int, code_revision: str
) -> ClaimedSftBuild | None:
    """Claim one build and durably register its external ownership first."""

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
            now = session.scalar(select(func.clock_timestamp()))
            if department is None or department.status != "active":
                terminal_attempt = _terminal_attempt_for_claim_failure(session, row, now)
                _terminal_failure(row, terminal_attempt, now, "department_unavailable")
                return None

            stale_id: UUID | None = None
            stale_manifest: dict[str, object] | None = None
            if row.status == "running":
                if row.publication_attempt_id is None:
                    raise SftQueueError("database_unavailable")
                stale = session.execute(
                    select(SftDatasetBuildAttempt)
                    .where(
                        SftDatasetBuildAttempt.department_id == row.department_id,
                        SftDatasetBuildAttempt.build_id == row.id,
                        SftDatasetBuildAttempt.publication_attempt_id == row.publication_attempt_id,
                        SftDatasetBuildAttempt.status == "running",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if stale is None:
                    raise SftQueueError("database_unavailable")
                stale.status = "reclaimed"
                stale.finished_at = now
                stale.version += 1
                stale_id = stale.publication_attempt_id
                stale_manifest = (
                    dict(stale.ownership_manifest)
                    if isinstance(stale.ownership_manifest, dict)
                    else None
                )
                row.attempt_number += 1

            attempt_id = uuid4()
            token = uuid4()
            session.add(
                SftDatasetBuildAttempt(
                    department_id=row.department_id,
                    build_id=row.id,
                    attempt_number=row.attempt_number,
                    publication_attempt_id=attempt_id,
                    code_revision=row.code_revision,
                    status="running",
                    claimed_at=now,
                    version=1,
                )
            )
            row.status = "running"
            row.review_status = "not_ready"
            row.worker_id = worker_id
            row.claim_token = token
            row.publication_attempt_id = attempt_id
            row.publication_manifest = None
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
                claim_token=token,
                publication_attempt_id=attempt_id,
                code_revision=row.code_revision,
                attempt_number=row.attempt_number,
                stale_publication_attempt_id=stale_id,
                stale_publication_manifest=stale_manifest,
            )
    except SftQueueError:
        raise
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
            row = session.execute(
                select(SftDatasetBuild.cancellation_requested_at).where(
                    *_owned(job), *_contract(job), _live()
                )
            ).one_or_none()
            if row is None:
                raise SftQueueError("claim_lost")
            if row.cancellation_requested_at is not None:
                raise SftQueueError("cancelled")
            result = session.execute(
                update(SftDatasetBuild)
                .where(*_owned(job), *_contract(job), _live())
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
    factory: sessionmaker[Session],
    data_dir,
    job: ClaimedSftBuild,
    *,
    lease_seconds: int,
    operation_seconds: int = 120,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Build only under one live claim, without parent-side source parsing."""

    scope = DepartmentScope(job.department_id)
    verification: SftFinalArtifactVerification | None = None
    staged = None
    prepublication: SftStagedArtifactVerification | None = None
    selector_fd: int | None = None
    authority_mapping: SftAuthorityMapping | None = None

    def checkpoint() -> _LeaseCheckpoint:
        """Start a fresh bounded deadline for one parent-owned operation."""

        result = _LeaseCheckpoint(
            factory,
            job,
            lease_seconds=lease_seconds,
            operation_seconds=operation_seconds,
            should_stop=should_stop,
        )
        result()
        return result

    try:
        if job.stale_publication_attempt_id is not None:
            _cleanup_stale_attempt(factory, data_dir, scope, job, checkpoint_factory=checkpoint)
        source = _load_source_metadata(factory, job, checkpoint=checkpoint())
        with SftArtifactStore(data_dir) as store:
            open_source = checkpoint()
            source_fd = store.open_source_directory(scope, job.source_bundle_id)
            try:
                open_source()
                prepare_stage = checkpoint()
                staged = store.prepare_dataset_stage(scope, job.id, job.publication_attempt_id)
                if staged.stage_fd is None:
                    raise SftQueueError("dataset_publication_failed")
                prepare_stage()
                selector_result = _supervise(
                    factory,
                    job,
                    lease_seconds,
                    operation_seconds,
                    should_stop,
                    operation=SftChildOperation.SELECT_SOURCE,
                    request={
                        "source_fd": source_fd,
                        "stage_fd": staged.stage_fd,
                        "department_id": str(job.department_id),
                        "source_bundle_id": str(job.source_bundle_id),
                    },
                    pass_fds=(source_fd, staged.stage_fd),
                )
                selector = _validate_selector_result(selector_result, source)
                map_authority = checkpoint()
                selector_fd = store.open_stage_scratch(
                    staged,
                    _SELECTOR_FILE,
                    expected=ArtifactDigest(selector["sha256"], selector["byte_size"]),
                    checkpoint=map_authority,
                )
                with factory() as session:
                    authority_mapping = write_authority_mapping(
                        session,
                        job.department_id,
                        selector_fd,
                        staged.stage_fd,
                        checkpoint=map_authority,
                    )
                authority = authority_mapping.snapshot
                if (
                    authority.fingerprint != source.authority_snapshot_sha256
                    or authority.selector_count != selector["count"]
                ):
                    raise SftQueueError("source_authority_changed")
                map_authority()
                result = _supervise(
                    factory,
                    job,
                    lease_seconds,
                    operation_seconds,
                    should_stop,
                    operation=SftChildOperation.BUILD_DATASET,
                    request=_build_request(
                        source_fd,
                        staged.stage_fd,
                        selector_fd,
                        job,
                        authority_mapping,
                    ),
                    pass_fds=(
                        source_fd,
                        staged.stage_fd,
                        selector_fd,
                        authority_mapping.descriptor,
                    ),
                )
            finally:
                try:
                    os.close(source_fd)
                except OSError:
                    pass
            manifest, train_count, validation_count = _validate_child_result(result, source, job)
            record_manifest = checkpoint()
            _record_publication_manifest(factory, job, manifest)
            record_manifest()
            prepublication_guard = checkpoint()
            prepublication = store.preverify_staged(
                staged,
                allowlist=DATASET_FILES,
                expected=manifest,
                checkpoint=prepublication_guard,
            )
            staged = None
            prepublication_guard()
            marker_transition = checkpoint()
            store.transition_stage_marker(prepublication, checkpoint=marker_transition)
            marker_transition()
            rename_and_durability = checkpoint()
            store.rename_preverified_stage(prepublication, checkpoint=rename_and_durability)
            rename_and_durability()
            post_rename = checkpoint()
            verification = store.verify_preverified_final(prepublication, checkpoint=post_rename)
            prepublication = None
            post_rename()
            mark_published = checkpoint()
            _mark_attempt_published(factory, job)
            mark_published()
            # Extend server-time ownership immediately before the short commit.
            renew_lease(factory, job, lease_seconds)
            finalize = checkpoint()
            _complete(
                factory,
                job,
                verification,
                selector_fd=selector_fd,
                selector_count=selector["count"],
                authority_fingerprint=authority.fingerprint,
                publication_manifest=manifest,
                train_count=train_count,
                validation_count=validation_count,
                guard=finalize,
                lease_seconds=lease_seconds,
            )
    except SftQueueError as error:
        _fail_or_cancel(factory, job, error.code)
    except SftContractError as error:
        _fail_or_cancel(factory, job, error.code)
    except SftArtifactError as error:
        _fail_or_cancel(factory, job, error.code)
    except SftSourceAuthorityError:
        _fail_or_cancel(factory, job, "source_authority_changed")
    except Exception:
        _fail_or_cancel(factory, job, "dataset_publication_failed")
    finally:
        if selector_fd is not None:
            try:
                os.close(selector_fd)
            except OSError:
                pass
        if authority_mapping is not None:
            authority_mapping.close()
        if verification is not None:
            verification.close()
        elif prepublication is not None:
            prepublication.close()
        elif staged is not None:
            staged.close()


def _load_source_metadata(
    factory: sessionmaker[Session], job: ClaimedSftBuild, *, checkpoint: _LeaseCheckpoint
) -> SftSourceBundle:
    checkpoint()
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
            return source
    except SftQueueError:
        raise
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def _supervise(
    factory,
    job,
    lease_seconds,
    operation_seconds,
    should_stop,
    *,
    operation: SftChildOperation,
    request: dict[str, object],
    pass_fds: tuple[int, ...],
):
    return run_claimed_operation(
        timeout_seconds=operation_seconds,
        heartbeat_seconds=lease_seconds,
        should_stop=should_stop,
        heartbeat=lambda: renew_lease(factory, job, lease_seconds),
        error=SftQueueError,
        operation=operation,
        request=request,
        pass_fds=pass_fds,
    )


def _cleanup_stale_attempt(
    factory: sessionmaker[Session],
    data_dir,
    scope: DepartmentScope,
    job: ClaimedSftBuild,
    *,
    checkpoint_factory: Callable[[], _LeaseCheckpoint],
) -> bool:
    if job.stale_publication_attempt_id is None:
        return False
    with SftArtifactStore(data_dir) as store:
        stage_cleanup = checkpoint_factory()
        removed = store.remove_owned_dataset_stage(
            scope, job.id, job.stale_publication_attempt_id, checkpoint=stage_cleanup
        )
        if job.stale_publication_manifest is not None:
            final_cleanup = checkpoint_factory()
            removed = (
                store.remove_owned_dataset_final(
                    scope,
                    job.id,
                    job.stale_publication_attempt_id,
                    expected=job.stale_publication_manifest,
                    checkpoint=final_cleanup,
                )
                or removed
            )
    cleanup_confirmation = checkpoint_factory()
    cleanup_confirmation()
    _confirm_attempt_cleanup(factory, job, job.stale_publication_attempt_id)
    return removed


def _confirm_attempt_cleanup(
    factory: sessionmaker[Session], job: ClaimedSftBuild, attempt_id: UUID
) -> None:
    try:
        with factory.begin() as session:
            row = session.execute(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.department_id == job.department_id,
                    SftDatasetBuildAttempt.build_id == job.id,
                    SftDatasetBuildAttempt.publication_attempt_id == attempt_id,
                    SftDatasetBuildAttempt.status.in_(("reclaimed", "failed", "cancelled")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise SftQueueError("claim_lost")
            row.cleanup_confirmed_at = session.scalar(select(func.clock_timestamp()))
            row.version += 1
    except SftQueueError:
        raise
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def _record_publication_manifest(
    factory: sessionmaker[Session], job: ClaimedSftBuild, manifest: dict[str, object]
) -> None:
    try:
        with factory.begin() as session:
            row = session.execute(
                select(SftDatasetBuild)
                .where(*_owned(job), *_contract(job), _live())
                .with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.department_id == job.department_id,
                    SftDatasetBuildAttempt.build_id == job.id,
                    SftDatasetBuildAttempt.publication_attempt_id == job.publication_attempt_id,
                    SftDatasetBuildAttempt.status == "running",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or attempt is None or row.cancellation_requested_at is not None:
                raise SftQueueError("claim_lost")
            if row.publication_manifest is not None and row.publication_manifest != manifest:
                raise SftQueueError("dataset_publication_failed")
            row.publication_manifest = manifest
            row.version += 1
            attempt.ownership_manifest = manifest
            attempt.version += 1
    except SftQueueError:
        raise
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def _mark_attempt_published(factory: sessionmaker[Session], job: ClaimedSftBuild) -> None:
    try:
        with factory.begin() as session:
            row = session.execute(
                select(SftDatasetBuild)
                .where(*_owned(job), *_contract(job), _live())
                .with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.department_id == job.department_id,
                    SftDatasetBuildAttempt.build_id == job.id,
                    SftDatasetBuildAttempt.publication_attempt_id == job.publication_attempt_id,
                    SftDatasetBuildAttempt.status == "running",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or attempt is None or row.cancellation_requested_at is not None:
                raise SftQueueError("claim_lost")
            attempt.published_at = session.scalar(select(func.clock_timestamp()))
            attempt.version += 1
    except SftQueueError:
        raise
    except SQLAlchemyError as error:
        raise SftQueueError() from error


def _build_request(
    source_fd: int,
    stage_fd: int,
    selector_fd: int,
    job: ClaimedSftBuild,
    authority_mapping: SftAuthorityMapping,
) -> dict[str, object]:
    """Closed, content-free IPC request; authority data stays descriptor-backed."""

    return {
        "source_fd": source_fd,
        "stage_fd": stage_fd,
        "selector_fd": selector_fd,
        "authority_fd": authority_mapping.descriptor,
        "department_id": str(job.department_id),
        "source_bundle_id": str(job.source_bundle_id),
        "build_id": str(job.id),
        "publication_attempt_id": str(job.publication_attempt_id),
        "attempt_number": job.attempt_number,
        "code_revision": job.code_revision,
        "authority_fingerprint": authority_mapping.snapshot.fingerprint,
        "authority_count": authority_mapping.snapshot.selector_count,
        "authority_mapping_sha256": authority_mapping.sha256,
        "authority_mapping_byte_size": authority_mapping.byte_size,
    }


def _validate_selector_result(result: object, source: SftSourceBundle) -> dict[str, int | str]:
    if not isinstance(result, dict) or set(result) != {"source", "selector"}:
        raise SftQueueError("dataset_publication_failed")
    _validate_source_result(result["source"], source)
    selector = result["selector"]
    if (
        not isinstance(selector, dict)
        or set(selector) != {"sha256", "byte_size", "count"}
        or not isinstance(selector["sha256"], str)
        or len(selector["sha256"]) != 64
        or type(selector["byte_size"]) is not int
        or type(selector["count"]) is not int
        or selector["byte_size"] < selector["count"]
        or not 1 <= selector["count"] <= source.source_reference_count
    ):
        raise SftQueueError("source_artifact_mismatch")
    return selector


def _validate_child_result(
    result: object, source: SftSourceBundle, job: ClaimedSftBuild
) -> tuple[dict[str, object], int, int]:
    if not isinstance(result, dict) or set(result) != {
        "source",
        "publication_manifest",
        "files",
        "train_count",
        "validation_count",
        "authority_fingerprint",
    }:
        raise SftQueueError("dataset_publication_failed")
    _validate_source_result(result["source"], source)
    if result["authority_fingerprint"] != source.authority_snapshot_sha256:
        raise SftQueueError("source_authority_changed")
    manifest = result["publication_manifest"]
    train_count = result["train_count"]
    validation_count = result["validation_count"]
    if (
        not isinstance(manifest, dict)
        or type(train_count) is not int
        or type(validation_count) is not int
        or train_count < 1
        or validation_count < 1
        or train_count + validation_count != source.example_count
        or manifest.get("publication_attempt_id") != str(job.publication_attempt_id)
    ):
        raise SftQueueError("dataset_publication_failed")
    return manifest, train_count, validation_count


def _validate_source_result(value: object, source: SftSourceBundle) -> None:
    if not isinstance(value, dict) or set(value) != {
        "manifest_sha256",
        "examples_sha256",
        "examples_byte_size",
        "example_count",
        "group_count",
        "source_reference_count",
    }:
        raise SftQueueError("source_artifact_mismatch")
    if (
        value.get("manifest_sha256") != source.manifest_sha256
        or value.get("examples_sha256") != source.examples_sha256
        or value.get("examples_byte_size") != source.examples_byte_size
        or value.get("example_count") != source.example_count
        or value.get("group_count") != source.group_count
        or value.get("source_reference_count") != source.source_reference_count
    ):
        raise SftQueueError("source_artifact_mismatch")


def _complete(
    factory: sessionmaker[Session],
    job: ClaimedSftBuild,
    verification: SftFinalArtifactVerification,
    *,
    selector_fd: int | None,
    selector_count: int,
    authority_fingerprint: str,
    publication_manifest: dict[str, object],
    train_count: int,
    validation_count: int,
    guard: _LeaseCheckpoint,
    lease_seconds: int,
) -> None:
    if selector_fd is None:
        raise SftQueueError("source_authority_changed")
    try:
        files = dict(verification.files)
        with factory.begin() as session:
            row = session.execute(
                select(SftDatasetBuild)
                .where(*_owned(job), *_contract(job), _live())
                .with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.department_id == job.department_id,
                    SftDatasetBuildAttempt.build_id == job.id,
                    SftDatasetBuildAttempt.publication_attempt_id == job.publication_attempt_id,
                    SftDatasetBuildAttempt.status == "running",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or attempt is None or row.cancellation_requested_at is not None:
                raise SftQueueError("claim_lost")
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
                    Membership.role.in_(("system_admin", "department_admin", "instructor")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            source = session.execute(
                select(SftSourceBundle)
                .where(
                    SftSourceBundle.id == job.source_bundle_id,
                    SftSourceBundle.department_id == job.department_id,
                    SftSourceBundle.status == "active",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if department is None:
                raise SftQueueError("department_unavailable")
            if requester is None:
                raise SftQueueError("requester_unauthorized")
            if source is None or source.authority_snapshot_sha256 != authority_fingerprint:
                raise SftQueueError("source_authority_changed")
            if row.publication_manifest != publication_manifest:
                raise SftQueueError("dataset_publication_failed")

            last_renewal = 0.0

            def final_checkpoint() -> None:
                nonlocal last_renewal
                guard.final_check()
                now_monotonic = time.monotonic()
                if now_monotonic < last_renewal + max(0.1, lease_seconds / 3):
                    return
                now = session.scalar(select(func.clock_timestamp()))
                if row.lease_expires_at is None or row.lease_expires_at <= now:
                    raise SftQueueError("claim_lost")
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.version += 1
                session.flush()
                last_renewal = now_monotonic

            validate_authority_selector(
                session,
                job.department_id,
                selector_fd,
                expected_fingerprint=authority_fingerprint,
                expected_count=selector_count,
                lock=True,
                checkpoint=final_checkpoint,
            )
            final_checkpoint()
            # Complete hashes were calculated before this transaction.  The
            # retained descriptor proof binds those bytes to this exact commit.
            verification.recheck_identity()
            now = session.scalar(select(func.clock_timestamp()))
            if row.lease_expires_at is None or row.lease_expires_at <= now:
                raise SftQueueError("claim_lost")
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
            attempt.status = "succeeded"
            attempt.finished_at = now
            attempt.published_at = attempt.published_at or now
            attempt.version += 1
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
            attempt = session.execute(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.department_id == job.department_id,
                    SftDatasetBuildAttempt.build_id == job.id,
                    SftDatasetBuildAttempt.publication_attempt_id == job.publication_attempt_id,
                    SftDatasetBuildAttempt.status == "running",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                return False
            now = session.scalar(select(func.clock_timestamp()))
            terminal = (
                "cancelled" if code == "cancelled" or row.cancellation_requested_at else "failed"
            )
            row.status = terminal
            row.error_code = "cancelled" if terminal == "cancelled" else code
            row.finished_at = now
            row.lease_expires_at = None
            row.worker_id = None
            row.claim_token = None
            row.version += 1
            attempt.status = terminal
            attempt.finished_at = now
            attempt.version += 1
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


def _terminal_attempt_for_claim_failure(
    session: Session, row: SftDatasetBuild, now
) -> SftDatasetBuildAttempt:
    """Return the exact attempt that must terminalize with a claim failure."""

    if row.status == "running":
        if row.publication_attempt_id is None:
            raise SftQueueError("database_unavailable")
        attempt = session.execute(
            select(SftDatasetBuildAttempt)
            .where(
                SftDatasetBuildAttempt.department_id == row.department_id,
                SftDatasetBuildAttempt.build_id == row.id,
                SftDatasetBuildAttempt.publication_attempt_id == row.publication_attempt_id,
                SftDatasetBuildAttempt.status == "running",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if attempt is None:
            raise SftQueueError("database_unavailable")
        return attempt
    if row.status != "queued":
        raise SftQueueError("database_unavailable")
    attempt = SftDatasetBuildAttempt(
        department_id=row.department_id,
        build_id=row.id,
        attempt_number=row.attempt_number,
        publication_attempt_id=uuid4(),
        code_revision=row.code_revision,
        status="running",
        claimed_at=now,
        version=1,
    )
    session.add(attempt)
    return attempt


def _terminal_failure(
    row: SftDatasetBuild, attempt: SftDatasetBuildAttempt, now, code: str
) -> None:
    """Atomically terminalize a build and its exact durable attempt."""

    if attempt.status != "running":
        raise SftQueueError("database_unavailable")
    row.status = "failed"
    row.error_code = code
    row.finished_at = now
    row.lease_expires_at = None
    row.worker_id = None
    row.claim_token = None
    row.version += 1
    attempt.status = "failed"
    attempt.finished_at = now
    attempt.version += 1

"""Department-scoped, metadata-only Phase 11 job-generation control plane."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.models import SftDatasetBuild, TrainingExecution, TrainingJob, TrainingJobPurgeReservation
from app.schemas import TrainingJobCreateRequest
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.training_job_domain import (
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    DATASET_INFO_VERSION,
    EXECUTION_PROFILE_VERSION,
    LLAMAFACTORY_VERSION,
    MAX_RECORD_CONTENT_BYTES,
    TRAINING_CONFIG_VERSION,
    TRAINING_JOB_CONTRACT_VERSION,
    TRAINING_JOB_MANIFEST_VERSION,
    training_profile,
)

TRAINING_READ_ROLES = frozenset(
    {DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN, DepartmentRole.INSTRUCTOR}
)
TRAINING_MUTATION_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def enqueue_training_job(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    request: TrainingJobCreateRequest,
    *,
    code_revision: str | None,
) -> TrainingJob:
    if code_revision is None or _REVISION.fullmatch(code_revision) is None:
        raise ServiceError(503, "Training job generator unavailable")
    try:
        profile = training_profile(request.profile_id)
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            TRAINING_MUTATION_ROLES,
            lock=True,
            audit_action="training.job.enqueue.authorization",
        )
        dataset = session.execute(
            select(SftDatasetBuild)
            .where(
                SftDatasetBuild.id == request.dataset_build_id,
                SftDatasetBuild.department_id == request_scope.department.value,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if dataset is None:
            raise ServiceError(404, "Training dataset not found")
        _require_eligible_dataset(dataset, request.expected_dataset_version)
        job = TrainingJob(
            id=uuid4(),
            department_id=request_scope.department.value,
            dataset_build_id=dataset.id,
            requested_by_user_id=authorization.identity.id,
            status="queued",
            review_status="not_ready",
            profile_id=profile.profile_id,
            base_model_id=BASE_MODEL_ID,
            base_model_revision=BASE_MODEL_REVISION,
            base_model_license=BASE_MODEL_LICENSE,
            llamafactory_version=LLAMAFACTORY_VERSION,
            artifact_contract_version=TRAINING_JOB_CONTRACT_VERSION,
            manifest_contract_version=TRAINING_JOB_MANIFEST_VERSION,
            configuration_contract_version=TRAINING_CONFIG_VERSION,
            dataset_info_contract_version=DATASET_INFO_VERSION,
            execution_profile_contract_version=EXECUTION_PROFILE_VERSION,
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
            execution_scope_id=uuid4(),
            attempt_number=1,
            code_revision=code_revision,
            maximum_record_content_bytes=MAX_RECORD_CONTENT_BYTES,
        )
        session.add(job)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="training.job.enqueue",
            resource_type="training_job",
            resource_id=job.id,
        )
        return job
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Training job conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_training_jobs(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    offset: int,
) -> tuple[TrainingJob, ...]:
    _page(limit, offset)
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            TRAINING_READ_ROLES,
            lock=False,
            audit_action="training.job.list.authorization",
        )
        return tuple(
            session.scalars(
                select(TrainingJob)
                .where(TrainingJob.department_id == request_scope.department.value)
                .order_by(TrainingJob.created_at.desc(), TrainingJob.id)
                .offset(offset)
                .limit(limit)
            )
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_training_job(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    training_job_id: UUID,
) -> TrainingJob:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            TRAINING_READ_ROLES,
            lock=False,
            audit_action="training.job.read.authorization",
        )
        row = session.execute(
            select(TrainingJob).where(
                TrainingJob.id == training_job_id,
                TrainingJob.department_id == request_scope.department.value,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError(404, "Training job not found")
        return row
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def cancel_training_job(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    training_job_id: UUID,
    *,
    expected_version: int,
) -> TrainingJob:
    try:
        # Keep the shared job -> department order used by execution and
        # archive mutations; authorization is revalidated after the job lock.
        job = _locked_job(session, request_scope, training_job_id)
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            TRAINING_MUTATION_ROLES,
            lock=True,
            audit_action="training.job.cancel.authorization",
        )
        _version(job, expected_version)
        _require_no_active_purge_reservation(session, job)
        if job.status == "queued":
            job.status = "cancelled"
            job.error_code = "cancelled"
            job.finished_at = session.scalar(select(func.clock_timestamp()))
        elif job.status == "running":
            if job.cancellation_requested_at is None:
                job.cancellation_requested_at = session.scalar(select(func.clock_timestamp()))
        else:
            raise ServiceError(409, "Training job cannot be cancelled")
        job.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="training.job.cancel",
            resource_type="training_job",
            resource_id=job.id,
        )
        return job
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def review_training_job(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    training_job_id: UUID,
    *,
    action: str,
    expected_version: int,
) -> TrainingJob:
    try:
        if action == "archive":
            # Archive races execution registration; keep the shared job ->
            # department lock order used by the Phase 14.1 control plane.
            job = _locked_job(session, request_scope, training_job_id)
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                TRAINING_MUTATION_ROLES,
                lock=True,
                audit_action="training.job.review.authorization",
            )
        else:
            job = _locked_job(session, request_scope, training_job_id)
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                TRAINING_MUTATION_ROLES,
                lock=True,
                audit_action="training.job.review.authorization",
            )
        _version(job, expected_version)
        if action == "archive":
            if (
                session.scalar(
                    select(TrainingExecution.id)
                    .where(
                        TrainingExecution.department_id == job.department_id,
                        TrainingExecution.training_job_id == job.id,
                        TrainingExecution.status.in_(("queued", "running", "cancel_requested")),
                    )
                    .with_for_update()
                )
                is not None
            ):
                raise ServiceError(409, "Training job has an active execution")
            _require_no_active_purge_reservation(session, job)
        else:
            _require_no_active_purge_reservation(session, job)
        transitions = {
            "approve": ("pending", "approved"),
            "reject": ("pending", "rejected"),
            "archive": (("approved", "rejected"), "archived"),
        }
        expected, target = transitions.get(action, (None, None))
        if expected is None or (
            job.review_status not in expected
            if isinstance(expected, tuple)
            else job.review_status != expected
        ):
            raise ServiceError(409, "Training job review transition is invalid")
        now = session.scalar(select(func.clock_timestamp()))
        job.review_status = target
        job.reviewed_by_user_id = authorization.identity.id
        job.reviewed_at = now
        if target == "archived":
            job.archived_at = now
        job.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action=f"training.job.review.{action}",
            resource_type="training_job",
            resource_id=job.id,
        )
        return job
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _locked_job(
    session: Session, request_scope: DepartmentRequestScope, training_job_id: UUID
) -> TrainingJob:
    job = session.execute(
        select(TrainingJob)
        .where(
            TrainingJob.id == training_job_id,
            TrainingJob.department_id == request_scope.department.value,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise ServiceError(404, "Training job not found")
    return job


def _require_eligible_dataset(dataset: SftDatasetBuild, expected_version: int) -> None:
    if (
        dataset.status != "succeeded"
        or dataset.review_status != "approved"
        or dataset.purged_at is not None
        or dataset.version != expected_version
        or dataset.result_manifest_sha256 is None
        or dataset.train_sha256 is None
        or dataset.validation_sha256 is None
        or dataset.provenance_sha256 is None
        or dataset.train_byte_size is None
        or dataset.validation_byte_size is None
        or dataset.provenance_byte_size is None
        or dataset.publication_attempt_id is None
        or dataset.train_example_count is None
        or dataset.validation_example_count is None
        or dataset.train_example_count < 1
        or dataset.validation_example_count < 1
        or dataset.source_example_count < 2
        or dataset.source_group_count < 2
        or dataset.source_reference_count < dataset.source_example_count
        or dataset.artifact_contract_version != "phase10-sft-dataset-v1"
        or dataset.example_contract_version != "phase10-sft-example-v1"
        or dataset.normalization_version != "phase10-sft-normalization-v1"
        or dataset.split_version != "phase10-sft-group-split-v1"
        or not isinstance(dataset.publication_manifest, dict)
    ):
        raise ServiceError(409, "Training dataset is unavailable")


def _require_no_active_purge_reservation(session: Session, job: TrainingJob) -> None:
    reservation = session.execute(
        select(TrainingJobPurgeReservation)
        .where(
            TrainingJobPurgeReservation.department_id == job.department_id,
            TrainingJobPurgeReservation.training_job_id == job.id,
            TrainingJobPurgeReservation.status.in_(
                ("registered", "deletion_authorized", "tombstone_bound")
            ),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if reservation is not None:
        raise ServiceError(409, "Training job purge is in progress")


def _version(job: TrainingJob, expected_version: int) -> None:
    if type(expected_version) is not int or expected_version < 1 or job.version != expected_version:
        raise ServiceError(409, "Training job version conflict")


def _page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ServiceError(400, "Invalid page")

"""Department-scoped, metadata-only Phase 11 job-generation control plane."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.models import SftDatasetBuild, TrainingJob
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
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            TRAINING_MUTATION_ROLES,
            lock=True,
            audit_action="training.job.cancel.authorization",
        )
        job = _locked_job(session, request_scope, training_job_id)
        _version(job, expected_version)
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
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            TRAINING_MUTATION_ROLES,
            lock=True,
            audit_action="training.job.review.authorization",
        )
        job = _locked_job(session, request_scope, training_job_id)
        _version(job, expected_version)
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
        or dataset.artifact_contract_version != "phase10-sft-dataset-v1"
        or dataset.example_contract_version != "phase10-sft-example-v1"
        or dataset.normalization_version != "phase10-sft-normalization-v1"
        or dataset.split_version != "phase10-sft-group-split-v1"
        or not isinstance(dataset.publication_manifest, dict)
    ):
        raise ServiceError(409, "Training dataset is unavailable")


def _version(job: TrainingJob, expected_version: int) -> None:
    if type(expected_version) is not int or expected_version < 1 or job.version != expected_version:
        raise ServiceError(409, "Training job version conflict")


def _page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ServiceError(400, "Invalid page")

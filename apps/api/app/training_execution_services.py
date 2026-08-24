"""Department-scoped Phase 14.1 execution control-plane services."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.models import (
    TrainingExecution,
    TrainingJob,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
)
from app.schemas import TrainingExecutionCreateRequest
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.training_execution_domain import (
    EXECUTION_CONTRACT_VERSION,
    EXECUTION_LLAMFACTORY_VERSION,
    EXECUTION_MODEL_ID,
    EXECUTION_MODEL_LICENSE,
    EXECUTION_MODEL_REVISION,
    EXECUTION_PROFILES,
    execution_authority_fingerprint,
    training_job_authority_snapshot,
)
from app.training_job_domain import (
    TrainingJobContractError,
    canonical_json_bytes,
    parse_job_manifest,
)

EXECUTION_READ_ROLES = frozenset(
    {DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN, DepartmentRole.INSTRUCTOR}
)
EXECUTION_MUTATION_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
ACTIVE_EXECUTION_STATUSES = ("queued", "running", "cancel_requested")
ACTIVE_PURGE_STATUSES = ("registered", "deletion_authorized", "tombstone_bound")


def _server_now(session: Session) -> datetime:
    value = session.execute(select(func.clock_timestamp())).scalar_one()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _lock_job_first(
    session: Session, principal: AuthenticatedPrincipal, scope: DepartmentRequestScope, job_id: UUID
) -> tuple[TrainingJob, object]:
    """Use job -> department lock order for enqueue/cancel/retry races."""

    # Resolve the path selector without taking a department lock so the job is
    # always the first mutable object locked by this service.
    authorize_transaction(
        session,
        principal,
        scope,
        EXECUTION_MUTATION_ROLES,
        lock=False,
        audit_action="training.execution.authorization.selector",
    )
    job = session.execute(
        select(TrainingJob)
        .where(TrainingJob.id == job_id, TrainingJob.department_id == scope.department.value)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if job is None:
        raise ServiceError(404, "Training job not found")
    authorization = authorize_transaction(
        session,
        principal,
        scope,
        EXECUTION_MUTATION_ROLES,
        lock=True,
        audit_action="training.execution.authorization",
    )
    return job, authorization


def _require_no_active_purge(session: Session, job: TrainingJob) -> None:
    reservation = session.execute(
        select(TrainingJobPurgeReservation.id)
        .where(
            TrainingJobPurgeReservation.training_job_id == job.id,
            TrainingJobPurgeReservation.department_id == job.department_id,
            TrainingJobPurgeReservation.status.in_(ACTIVE_PURGE_STATUSES),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if reservation is not None:
        raise ServiceError(409, "Training job purge is active")


def _require_phase11_authority(session: Session, job: TrainingJob) -> dict[str, object]:
    if (
        job.status != "succeeded"
        or job.review_status != "approved"
        or job.purged_at is not None
        or job.publication_attempt_id is None
        or not isinstance(job.publication_manifest, dict)
        or not job.result_manifest_sha256
        or not job.training_config_sha256
        or not job.dataset_info_sha256
        or not job.train_sha256
        or not job.validation_sha256
    ):
        raise ServiceError(409, "Training job is not an approved authoritative publication")
    try:
        manifest = parse_job_manifest(canonical_json_bytes(job.publication_manifest))
    except (TypeError, TrainingJobContractError):
        raise ServiceError(409, "Training job publication authority is unavailable") from None
    expected_manifest = {
        "artifact_contract_version": job.artifact_contract_version,
        "manifest_contract_version": job.manifest_contract_version,
        "configuration_contract_version": job.configuration_contract_version,
        "dataset_info_contract_version": job.dataset_info_contract_version,
        "execution_profile_contract_version": job.execution_profile_contract_version,
        "department_id": str(job.department_id),
        "training_job_id": str(job.id),
        "publication_attempt_id": str(job.publication_attempt_id),
        "execution_scope_id": str(job.execution_scope_id),
        "attempt_number": job.attempt_number,
        "code_revision": job.code_revision,
        "dataset_build_id": str(job.dataset_build_id),
        "dataset_build_version": job.dataset_build_version,
        "dataset_manifest_sha256": job.dataset_manifest_sha256,
        "dataset_artifact_contract_version": job.dataset_artifact_contract_version,
        "dataset_example_contract_version": job.dataset_example_contract_version,
        "dataset_normalization_version": job.dataset_normalization_version,
        "dataset_split_version": job.dataset_split_version,
        "train_example_count": job.train_example_count,
        "validation_example_count": job.validation_example_count,
        "base_model_id": job.base_model_id,
        "base_model_revision": job.base_model_revision,
        "base_model_license": job.base_model_license,
        "llamafactory_version": job.llamafactory_version,
        "profile_id": job.profile_id,
        "dataset_rights_attested": job.dataset_rights_attested,
        "evaluation_contamination_reviewed": job.evaluation_contamination_reviewed,
    }
    if any(manifest.get(key) != value for key, value in expected_manifest.items()):
        raise ServiceError(409, "Training job publication authority is unavailable")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ServiceError(409, "Training job publication authority is unavailable")
    expected_files = {
        "training.yaml": (job.training_config_sha256, job.training_config_byte_size),
        "dataset_info.json": (job.dataset_info_sha256, job.dataset_info_byte_size),
        "train.jsonl": (job.train_sha256, job.train_byte_size),
        "validation.jsonl": (job.validation_sha256, job.validation_byte_size),
    }
    if set(files) != set(expected_files) or any(
        not isinstance(descriptor, dict)
        or descriptor.get("sha256") != expected_sha
        or descriptor.get("byte_size") != expected_size
        for name, (expected_sha, expected_size) in expected_files.items()
        for descriptor in [files.get(name)]
    ):
        raise ServiceError(409, "Training job publication authority is unavailable")
    if hashlib.sha256(canonical_json_bytes(manifest) + b"\n").hexdigest() != (
        job.result_manifest_sha256
    ):
        raise ServiceError(409, "Training job publication authority is unavailable")
    if job.profile_id not in EXECUTION_PROFILES:
        raise ServiceError(409, "Training job authority is invalid")
    if (
        job.base_model_id != EXECUTION_MODEL_ID
        or job.base_model_revision != EXECUTION_MODEL_REVISION
        or job.base_model_license != EXECUTION_MODEL_LICENSE
        or job.llamafactory_version != EXECUTION_LLAMFACTORY_VERSION
    ):
        raise ServiceError(409, "Training job authority is invalid")
    attempts = tuple(
        session.scalars(
            select(TrainingJobAttempt).where(
                TrainingJobAttempt.training_job_id == job.id,
                TrainingJobAttempt.department_id == job.department_id,
                TrainingJobAttempt.status == "succeeded",
                TrainingJobAttempt.publication_attempt_id == job.publication_attempt_id,
                TrainingJobAttempt.attempt_number == job.attempt_number,
            )
        )
    )
    if len(attempts) != 1:
        raise ServiceError(409, "Training job publication authority is unavailable")
    _require_no_active_purge(session, job)
    return training_job_authority_snapshot(job)


def _parent_from_job(
    *,
    execution_id: UUID,
    job: TrainingJob,
    requester_id: UUID,
    snapshot: dict[str, object],
    execution_code_revision: str,
) -> TrainingExecution:
    publication_files = (
        job.publication_manifest.get("files", {})
        if isinstance(job.publication_manifest, dict)
        else {}
    )
    manifest_descriptor = (
        publication_files.get("manifest.json", {}) if isinstance(publication_files, dict) else {}
    )
    manifest_byte_size = (
        manifest_descriptor.get("byte_size") if isinstance(manifest_descriptor, dict) else None
    )
    if type(manifest_byte_size) is not int or manifest_byte_size <= 0:
        manifest_byte_size = job.training_config_byte_size
    values = {
        "id": execution_id,
        "department_id": job.department_id,
        "training_job_id": job.id,
        "requested_by_user_id": requester_id,
        "training_job_version": job.version,
        "training_job_status": job.status,
        "training_job_review_status": job.review_status,
        "training_job_publication_attempt_id": job.publication_attempt_id,
        "training_job_attempt_number": job.attempt_number,
        "training_job_code_revision": job.code_revision,
        "training_job_execution_scope_id": job.execution_scope_id,
        "training_job_manifest_sha256": job.result_manifest_sha256,
        "training_job_manifest_byte_size": manifest_byte_size,
        "training_job_publication_manifest": job.publication_manifest,
        "training_job_config_sha256": job.training_config_sha256,
        "training_job_config_byte_size": job.training_config_byte_size,
        "training_job_dataset_info_sha256": job.dataset_info_sha256,
        "training_job_dataset_info_byte_size": job.dataset_info_byte_size,
        "training_job_train_sha256": job.train_sha256,
        "training_job_train_byte_size": job.train_byte_size,
        "training_job_validation_sha256": job.validation_sha256,
        "training_job_validation_byte_size": job.validation_byte_size,
        "training_job_artifact_cleanup_confirmed_at": job.artifact_cleanup_confirmed_at,
        "training_job_purged_at": job.purged_at,
        "training_job_profile_id": job.profile_id,
        "training_job_base_model_id": job.base_model_id,
        "training_job_base_model_revision": job.base_model_revision,
        "training_job_base_model_license": job.base_model_license,
        "training_job_llamafactory_version": job.llamafactory_version,
        "training_job_artifact_contract_version": job.artifact_contract_version,
        "training_job_manifest_contract_version": job.manifest_contract_version,
        "training_job_configuration_contract_version": job.configuration_contract_version,
        "training_job_dataset_info_contract_version": job.dataset_info_contract_version,
        "training_job_execution_profile_contract_version": job.execution_profile_contract_version,
        "dataset_build_id": job.dataset_build_id,
        "dataset_build_version": job.dataset_build_version,
        "dataset_manifest_sha256": job.dataset_manifest_sha256,
        "dataset_source_bundle_id": job.dataset_source_bundle_id,
        "dataset_status": job.dataset_status,
        "dataset_review_status": job.dataset_review_status,
        "dataset_publication_attempt_id": job.dataset_publication_attempt_id,
        "dataset_publication_attempt_number": job.dataset_publication_attempt_number,
        "dataset_code_revision": job.dataset_code_revision,
        "dataset_train_sha256": job.dataset_train_sha256,
        "dataset_train_byte_size": job.dataset_train_byte_size,
        "dataset_validation_sha256": job.dataset_validation_sha256,
        "dataset_validation_byte_size": job.dataset_validation_byte_size,
        "dataset_provenance_sha256": job.dataset_provenance_sha256,
        "dataset_provenance_byte_size": job.dataset_provenance_byte_size,
        "dataset_train_example_count": job.dataset_train_example_count,
        "dataset_validation_example_count": job.dataset_validation_example_count,
        "dataset_source_example_count": job.dataset_source_example_count,
        "dataset_source_group_count": job.dataset_source_group_count,
        "dataset_source_reference_count": job.dataset_source_reference_count,
        "dataset_rights_attested": job.dataset_rights_attested,
        "evaluation_contamination_reviewed": job.evaluation_contamination_reviewed,
        "dataset_artifact_contract_version": job.dataset_artifact_contract_version,
        "dataset_example_contract_version": job.dataset_example_contract_version,
        "dataset_normalization_version": job.dataset_normalization_version,
        "dataset_split_version": job.dataset_split_version,
        "profile_id": job.profile_id,
        "base_model_id": job.base_model_id,
        "base_model_revision": job.base_model_revision,
        "base_model_license": job.base_model_license,
        "llamafactory_version": job.llamafactory_version,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "execution_code_revision": execution_code_revision,
        "authority_fingerprint": execution_authority_fingerprint(
            execution_id=execution_id,
            training_job_code_revision=job.code_revision,
            execution_code_revision=execution_code_revision,
            snapshot=snapshot,
        ),
        "status": "queued",
        "current_attempt_number": 1,
        "version": 1,
    }
    return TrainingExecution(**values)


def enqueue_training_execution(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    request: TrainingExecutionCreateRequest,
    *,
    execution_code_revision: str | None = None,
) -> TrainingExecution:
    try:
        job, authorization = _lock_job_first(
            session, principal, request_scope, request.training_job_id
        )
        if job.version != request.expected_training_job_version:
            raise ServiceError(409, "Training job version conflict")
        snapshot = _require_phase11_authority(session, job)
        if (
            execution_code_revision is None
            or re.fullmatch(r"[0-9a-f]{40}", execution_code_revision) is None
        ):
            raise ServiceError(409, "Training execution code authority is unavailable")
        execution = _parent_from_job(
            execution_id=uuid4(),
            job=job,
            requester_id=authorization.identity.id,
            snapshot=snapshot,
            execution_code_revision=execution_code_revision,
        )
        session.add(execution)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="training.execution.enqueue",
            resource_type="training_execution",
            resource_id=execution.id,
        )
        return execution
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Training execution conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_training_executions(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    offset: int,
) -> tuple[TrainingExecution, ...]:
    if not 1 <= limit <= 100 or offset < 0:
        raise ServiceError(422, "Invalid pagination")
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            EXECUTION_READ_ROLES,
            lock=False,
            audit_action="training.execution.list.authorization",
        )
        return tuple(
            session.scalars(
                select(TrainingExecution)
                .where(TrainingExecution.department_id == request_scope.department.value)
                .order_by(TrainingExecution.created_at.desc(), TrainingExecution.id)
                .offset(offset)
                .limit(limit)
            )
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_training_execution(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    execution_id: UUID,
) -> TrainingExecution:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            EXECUTION_READ_ROLES,
            lock=False,
            audit_action="training.execution.read.authorization",
        )
        row = session.execute(
            select(TrainingExecution).where(
                TrainingExecution.id == execution_id,
                TrainingExecution.department_id == request_scope.department.value,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError(404, "Training execution not found")
        return row
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _lock_execution(
    session: Session, request_scope: DepartmentRequestScope, execution_id: UUID
) -> TrainingExecution:
    row = session.execute(
        select(TrainingExecution)
        .where(
            TrainingExecution.id == execution_id,
            TrainingExecution.department_id == request_scope.department.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise ServiceError(404, "Training execution not found")
    return row


def _authorize_execution_mutation(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    execution_id: UUID,
) -> tuple[TrainingExecution, object, TrainingJob]:
    # Lock the Phase 11 job first, then the execution.  Rechecking membership
    # after the job lock closes enqueue/archive and retry/purge races.
    execution_probe = session.execute(
        select(TrainingExecution.training_job_id).where(
            TrainingExecution.id == execution_id,
            TrainingExecution.department_id == request_scope.department.value,
        )
    ).scalar_one_or_none()
    if execution_probe is None:
        raise ServiceError(404, "Training execution not found")
    job, authorization = _lock_job_first(session, principal, request_scope, execution_probe)
    execution = _lock_execution(session, request_scope, execution_id)
    return execution, authorization, job


def cancel_training_execution(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    execution_id: UUID,
    *,
    expected_version: int,
) -> TrainingExecution:
    try:
        execution, authorization, job = _authorize_execution_mutation(
            session, principal, request_scope, execution_id
        )
        if execution.version != expected_version:
            raise ServiceError(409, "Training execution version conflict")
        _require_no_active_purge(session, job)
        if execution.status == "queued":
            execution.status = "cancelled"
            execution.error_code = "cancelled"
            execution.finished_at = _server_now(session)
        elif execution.status == "running":
            execution.status = "cancel_requested"
            execution.cancellation_requested_at = _server_now(session)
        elif execution.status == "cancel_requested":
            return execution
        else:
            raise ServiceError(409, "Training execution is not cancellable")
        execution.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="training.execution.cancel",
            resource_type="training_execution",
            resource_id=execution.id,
        )
        session.flush()
        return execution
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def retry_training_execution(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    execution_id: UUID,
    *,
    expected_version: int,
) -> TrainingExecution:
    try:
        execution, authorization, job = _authorize_execution_mutation(
            session, principal, request_scope, execution_id
        )
        if execution.version != expected_version:
            raise ServiceError(409, "Training execution version conflict")
        if execution.status not in {"failed", "cancelled"}:
            raise ServiceError(409, "Only failed or cancelled executions can be retried")
        _require_phase11_authority(session, job)
        execution.status = "queued"
        execution.current_attempt_number += 1
        execution.current_attempt_id = None
        execution.worker_id = None
        execution.claim_token = None
        execution.lease_expires_at = None
        execution.cancellation_requested_at = None
        execution.started_at = None
        execution.finished_at = None
        execution.error_code = None
        execution.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="training.execution.retry",
            resource_type="training_execution",
            resource_id=execution.id,
        )
        session.flush()
        return execution
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


__all__ = [
    "ACTIVE_EXECUTION_STATUSES",
    "EXECUTION_MUTATION_ROLES",
    "EXECUTION_READ_ROLES",
    "cancel_training_execution",
    "enqueue_training_execution",
    "list_training_executions",
    "read_training_execution",
    "retry_training_execution",
]

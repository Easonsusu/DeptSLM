"""Administrator enqueue boundary for immutable adapter registry publication."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_LICENSE, BASE_MODEL_REVISION
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    Adapter,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    SftArtifactReconciliationOperation,
    SftArtifactReconciliationOperationItem,
    SftDatasetBuild,
    SftDatasetBuildAttempt,
    TrainingJob,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.training_job_domain import canonical_json_bytes as training_canonical_json_bytes

_CODE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ADMIN_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
REGISTRY_CONTRACT_VERSION = "phase12-adapter-registry-v1"


@dataclass(frozen=True, slots=True)
class AdapterRegistryEnqueueResult:
    eligible: bool
    applied: bool
    department_id: UUID
    source_bundle_id: UUID
    training_job_id: UUID
    adapter_id: UUID | None
    registry_attempt_id: UUID | None
    profile_id: str
    base_model_id: str
    tensor_dtype: str
    tensor_count: int
    tensor_payload_byte_size: int

    @property
    def status(self) -> str:
        return "queued" if self.applied else "eligible"


def enqueue_adapter_registry(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    source_bundle_id: UUID,
    training_job_id: UUID,
    expected_source_version: int,
    expected_training_job_version: int,
    confirm_declared_training_association: bool,
    apply: bool,
    code_revision: str | None = None,
) -> AdapterRegistryEnqueueResult:
    """Authorize and atomically claim one exact source/job/dataset lineage."""

    if (
        type(confirm_declared_training_association) is not bool
        or not confirm_declared_training_association
    ):
        raise ServiceError(422, "Training association confirmation is required")
    if type(apply) is not bool:
        raise ServiceError(422, "Apply mode is invalid")
    if type(expected_source_version) is not int or expected_source_version <= 0:
        raise ServiceError(422, "Source version is invalid")
    if type(expected_training_job_version) is not int or expected_training_job_version <= 0:
        raise ServiceError(422, "Training job version is invalid")
    revision = code_revision or os.getenv("DEPTSLM_ADAPTER_REGISTRY_CODE_REVISION", "").strip()
    if _CODE_REVISION.fullmatch(revision) is None:
        raise ServiceError(503, "Adapter registry worker unavailable")
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            _ADMIN_ROLES,
            lock=apply,
            audit_action="adapter.registry.enqueue.authorization",
        )
        department_id = request_scope.department.value
        source = session.execute(
            select(AdapterImportSource)
            .where(
                AdapterImportSource.id == source_bundle_id,
                AdapterImportSource.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        job = session.execute(
            select(TrainingJob)
            .where(TrainingJob.id == training_job_id, TrainingJob.department_id == department_id)
            .with_for_update()
        ).scalar_one_or_none()
        if source is None:
            raise ServiceError(404, "Adapter source not found")
        if job is None:
            raise ServiceError(404, "Training job not found")
        if (
            source.version != expected_source_version
            or source.status != "committed"
            or source.claimed_adapter_id is not None
            or source.purged_at is not None
        ):
            raise ServiceError(409, "Adapter source is not eligible")
        if (
            job.version != expected_training_job_version
            or job.status != "succeeded"
            or job.review_status != "approved"
            or job.archived_at is not None
            or job.purged_at is not None
        ):
            raise ServiceError(409, "Training job is not eligible")
        _require_no_purge_reservation(session, department_id, job.id)
        source_attempt = session.execute(
            select(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.id == source.authoritative_attempt_id,
                AdapterImportAttempt.department_id == department_id,
                AdapterImportAttempt.source_bundle_id == source.id,
                AdapterImportAttempt.status == "committed",
            )
            .with_for_update()
        ).scalar_one_or_none()
        job_attempt = session.execute(
            select(TrainingJobAttempt)
            .where(
                TrainingJobAttempt.training_job_id == job.id,
                TrainingJobAttempt.department_id == department_id,
                TrainingJobAttempt.status == "succeeded",
                TrainingJobAttempt.publication_attempt_id == job.publication_attempt_id,
            )
            .order_by(TrainingJobAttempt.attempt_number.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        dataset = session.execute(
            select(SftDatasetBuild)
            .where(
                SftDatasetBuild.id == job.dataset_build_id,
                SftDatasetBuild.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        dataset_attempt = None
        if dataset is not None:
            dataset_attempt = session.execute(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.build_id == dataset.id,
                    SftDatasetBuildAttempt.department_id == department_id,
                    SftDatasetBuildAttempt.status == "succeeded",
                    SftDatasetBuildAttempt.publication_attempt_id == dataset.publication_attempt_id,
                )
                .order_by(SftDatasetBuildAttempt.attempt_number.desc())
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
        if source_attempt is None:
            raise ServiceError(409, "Adapter source authority changed")
        if job_attempt is None:
            raise ServiceError(409, "Training job authority changed")
        if (
            dataset is None
            or dataset.department_id != department_id
            or dataset.status != "succeeded"
            or dataset.review_status != "approved"
            or dataset.purged_at is not None
            or dataset_attempt is None
        ):
            raise ServiceError(409, "Dataset authority changed")
        _require_dataset_contract(dataset)
        _require_source_authority(source, source_attempt)
        _require_job_authority(job, job_attempt, dataset, dataset_attempt)
        _require_no_dataset_purge_reservation(session, department_id, dataset.id)
        if not apply:
            return AdapterRegistryEnqueueResult(
                eligible=True,
                applied=False,
                department_id=department_id,
                source_bundle_id=source.id,
                training_job_id=job.id,
                adapter_id=None,
                registry_attempt_id=None,
                profile_id=job.profile_id,
                base_model_id=job.base_model_id,
                tensor_dtype=str(source.tensor_dtype),
                tensor_count=int(source.tensor_count or 0),
                tensor_payload_byte_size=int(source.tensor_payload_byte_size or 0),
            )
        existing_adapter = session.execute(
            select(Adapter)
            .where(
                Adapter.source_bundle_id == source.id,
                Adapter.department_id == department_id,
                Adapter.status.not_in(("purged",)),
            )
            .order_by(Adapter.created_at, Adapter.id)
            .with_for_update()
            .limit(1)
        ).scalar_one_or_none()
        if existing_adapter is not None:
            raise ServiceError(409, "Adapter source already claimed")
        adapter_id = uuid4()
        publication_attempt_id = uuid4()
        adapter = Adapter(
            id=adapter_id,
            department_id=department_id,
            requested_by_user_id=authorization.identity.id,
            status="queued",
            execution_scope_id=uuid4(),
            attempt_number=1,
            code_revision=revision,
            source_bundle_id=source.id,
            source_authoritative_attempt_id=source.authoritative_attempt_id,
            source_publication_attempt_id=source_attempt.publication_attempt_id,
            source_attempt_number=source_attempt.attempt_number,
            source_attempt_version=source_attempt.version,
            source_imported_by_user_id=source.imported_by_user_id,
            # The adapter stores the version that will exist after the
            # atomic source-claim mutation below.
            source_version=source.version + 1,
            source_code_revision=source.code_revision,
            source_contract_version=source.source_contract_version,
            intake_contract_version=source.intake_contract_version,
            config_contract_version=source.config_contract_version,
            tensor_contract_version=source.tensor_contract_version,
            source_intake_manifest_sha256=source.intake_manifest_sha256,
            source_intake_manifest_byte_size=source.intake_manifest_byte_size,
            source_adapter_config_sha256=source.adapter_config_sha256,
            source_adapter_config_byte_size=source.adapter_config_byte_size,
            source_adapter_model_sha256=source.adapter_model_sha256,
            source_adapter_model_byte_size=source.adapter_model_byte_size,
            peft_version=source.peft_version,
            safetensors_format=source.safetensors_format,
            tensor_dtype=source.tensor_dtype,
            tensor_count=source.tensor_count,
            tensor_element_count=source.tensor_element_count,
            tensor_payload_byte_size=source.tensor_payload_byte_size,
            training_job_id=job.id,
            training_job_version=job.version,
            training_job_publication_attempt_id=job.publication_attempt_id,
            training_job_attempt_number=job_attempt.attempt_number,
            training_job_attempt_version=job_attempt.version,
            training_job_code_revision=job.code_revision,
            training_job_manifest_sha256=job.result_manifest_sha256,
            training_job_manifest_byte_size=_training_manifest_byte_size(job),
            training_job_execution_scope_id=job.execution_scope_id,
            training_job_config_sha256=job.training_config_sha256,
            training_job_config_byte_size=job.training_config_byte_size,
            training_job_dataset_info_sha256=job.dataset_info_sha256,
            training_job_dataset_info_byte_size=job.dataset_info_byte_size,
            training_job_train_sha256=job.train_sha256,
            training_job_train_byte_size=job.train_byte_size,
            training_job_validation_sha256=job.validation_sha256,
            training_job_validation_byte_size=job.validation_byte_size,
            training_job_profile_id=job.profile_id,
            training_job_artifact_contract_version=job.artifact_contract_version,
            training_job_manifest_contract_version=job.manifest_contract_version,
            training_configuration_contract_version=job.configuration_contract_version,
            training_dataset_info_contract_version=job.dataset_info_contract_version,
            training_execution_profile_contract_version=job.execution_profile_contract_version,
            llamafactory_version=job.llamafactory_version,
            dataset_build_id=dataset.id,
            dataset_build_version=dataset.version,
            dataset_publication_attempt_id=dataset.publication_attempt_id,
            dataset_publication_attempt_number=dataset.attempt_number,
            dataset_attempt_version=dataset_attempt.version,
            dataset_code_revision=dataset.code_revision,
            dataset_manifest_sha256=dataset.result_manifest_sha256,
            dataset_source_bundle_id=dataset.source_bundle_id,
            dataset_artifact_contract_version=dataset.artifact_contract_version,
            dataset_example_contract_version=dataset.example_contract_version,
            dataset_normalization_version=dataset.normalization_version,
            dataset_split_version=dataset.split_version,
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
            # These governance declarations are authoritative Phase 11 job
            # fields about the selected dataset, not Phase 10 row fields.
            dataset_rights_attested=job.dataset_rights_attested,
            evaluation_contamination_reviewed=job.evaluation_contamination_reviewed,
            base_model_id=BASE_MODEL_ID,
            base_model_revision=BASE_MODEL_REVISION,
            base_model_license=BASE_MODEL_LICENSE,
            artifact_contract_version="phase12-adapter-artifact-v1",
            registry_manifest_contract_version="phase12-adapter-manifest-v1",
            declared_external_training_association=True,
            verified_governance_lineage=False,
            verified_artifact_compatibility=False,
            training_provenance_verified=False,
            publication_attempt_id=publication_attempt_id,
        )
        session.add(adapter)
        session.flush()
        attempt = AdapterRegistryAttempt(
            id=uuid4(),
            department_id=department_id,
            adapter_id=adapter.id,
            attempt_number=1,
            publication_attempt_id=publication_attempt_id,
            execution_scope_id=adapter.execution_scope_id,
            code_revision=revision,
            status="registered",
        )
        session.add(attempt)
        session.flush()
        dependency = AdapterUpstreamDependency(
            id=uuid4(),
            department_id=department_id,
            adapter_id=adapter.id,
            training_job_id=job.id,
            dataset_build_id=dataset.id,
            status="active",
            version=1,
        )
        session.add(dependency)
        now = session.scalar(select(func.clock_timestamp()))
        source.status = "claimed"
        source.claimed_adapter_id = adapter.id
        source.claimed_at = now
        source.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="adapter.registry.enqueue",
            resource_type="adapter",
            resource_id=adapter.id,
        )
        return AdapterRegistryEnqueueResult(
            eligible=True,
            applied=True,
            department_id=department_id,
            source_bundle_id=source.id,
            training_job_id=job.id,
            adapter_id=adapter.id,
            registry_attempt_id=attempt.id,
            profile_id=job.profile_id,
            base_model_id=BASE_MODEL_ID,
            tensor_dtype=str(source.tensor_dtype),
            tensor_count=int(source.tensor_count or 0),
            tensor_payload_byte_size=int(source.tensor_payload_byte_size or 0),
        )
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Adapter registry conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _require_no_purge_reservation(
    session: Session, department_id: UUID, training_job_id: UUID
) -> None:
    active = session.execute(
        select(TrainingJobPurgeReservation)
        .where(
            TrainingJobPurgeReservation.department_id == department_id,
            TrainingJobPurgeReservation.training_job_id == training_job_id,
            TrainingJobPurgeReservation.status.in_(
                ("registered", "deletion_authorized", "tombstone_bound")
            ),
        )
        .order_by(TrainingJobPurgeReservation.created_at, TrainingJobPurgeReservation.id)
        .with_for_update()
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        raise ServiceError(409, "Training job purge is in progress")


def _require_no_dataset_purge_reservation(
    session: Session, department_id: UUID, dataset_build_id: UUID
) -> None:
    active = session.execute(
        select(SftArtifactReconciliationOperationItem)
        .join(
            SftArtifactReconciliationOperation,
            SftArtifactReconciliationOperation.id
            == SftArtifactReconciliationOperationItem.operation_id,
        )
        .where(
            SftArtifactReconciliationOperation.department_id == department_id,
            SftArtifactReconciliationOperation.operation_type == "purge",
            SftArtifactReconciliationOperation.status == "registered",
            SftArtifactReconciliationOperationItem.department_id == department_id,
            SftArtifactReconciliationOperationItem.resource_type == "dataset_final",
            SftArtifactReconciliationOperationItem.resource_id == dataset_build_id,
            SftArtifactReconciliationOperationItem.status == "registered",
        )
        .with_for_update()
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        raise ServiceError(409, "Dataset purge is in progress")


def _require_job_authority(
    job: TrainingJob,
    attempt: TrainingJobAttempt,
    dataset: SftDatasetBuild,
    dataset_attempt: SftDatasetBuildAttempt,
) -> None:
    if (
        attempt.status != "succeeded"
        or attempt.publication_attempt_id != job.publication_attempt_id
        or attempt.attempt_number != job.attempt_number
        or job.result_manifest_sha256 is None
        or not isinstance(job.publication_manifest, dict)
        or job.training_config_sha256 is None
        or job.training_config_byte_size is None
        or job.dataset_info_sha256 is None
        or job.dataset_info_byte_size is None
        or job.train_sha256 is None
        or job.train_byte_size is None
        or job.validation_sha256 is None
        or job.validation_byte_size is None
        or any(
            value is None or value <= 0
            for value in (
                job.training_config_byte_size,
                job.dataset_info_byte_size,
                job.train_byte_size,
                job.validation_byte_size,
                dataset.train_byte_size,
                dataset.validation_byte_size,
                dataset.provenance_byte_size,
                dataset.train_example_count,
                dataset.validation_example_count,
                dataset.source_example_count,
                dataset.source_group_count,
                dataset.source_reference_count,
            )
        )
        or dataset_attempt.status != "succeeded"
        or dataset_attempt.publication_attempt_id != dataset.publication_attempt_id
        or dataset_attempt.attempt_number != dataset.attempt_number
        or dataset.result_manifest_sha256 is None
        or job.dataset_build_id != dataset.id
        or job.dataset_build_version != dataset.version
        or job.dataset_manifest_sha256 != dataset.result_manifest_sha256
        or job.dataset_source_bundle_id != dataset.source_bundle_id
        or job.dataset_publication_attempt_id != dataset.publication_attempt_id
        or job.dataset_publication_attempt_number != dataset.attempt_number
        or job.dataset_code_revision != dataset.code_revision
        or job.dataset_train_sha256 != dataset.train_sha256
        or job.dataset_train_byte_size != dataset.train_byte_size
        or job.dataset_validation_sha256 != dataset.validation_sha256
        or job.dataset_validation_byte_size != dataset.validation_byte_size
        or job.dataset_provenance_sha256 != dataset.provenance_sha256
        or job.dataset_provenance_byte_size != dataset.provenance_byte_size
        or job.dataset_train_example_count != dataset.train_example_count
        or job.dataset_validation_example_count != dataset.validation_example_count
        or job.dataset_source_example_count != dataset.source_example_count
        or job.dataset_source_group_count != dataset.source_group_count
        or job.dataset_source_reference_count != dataset.source_reference_count
        or job.dataset_rights_attested is not True
        or job.evaluation_contamination_reviewed is not True
        or job.dataset_artifact_contract_version != dataset.artifact_contract_version
        or job.dataset_example_contract_version != dataset.example_contract_version
        or job.dataset_normalization_version != dataset.normalization_version
        or job.dataset_split_version != dataset.split_version
    ):
        raise ServiceError(409, "Dataset authority changed")


def _training_manifest_byte_size(job: TrainingJob) -> int:
    manifest = job.publication_manifest
    if not isinstance(manifest, dict):
        raise ServiceError(409, "Training job authority changed")
    raw = training_canonical_json_bytes(manifest) + b"\n"
    if (
        job.result_manifest_sha256 is None
        or hashlib.sha256(raw).hexdigest() != job.result_manifest_sha256
    ):
        raise ServiceError(409, "Training job authority changed")
    return len(raw)


def _require_source_authority(source: AdapterImportSource, attempt: AdapterImportAttempt) -> None:
    if (
        attempt.status != "committed"
        or attempt.source_bundle_id != source.id
        or attempt.department_id != source.department_id
        or source.authoritative_attempt_id != attempt.id
        or not isinstance(attempt.ownership_manifest, dict)
        or source.adapter_config_sha256 is None
        or source.adapter_model_sha256 is None
        or source.intake_manifest_sha256 is None
        or source.intake_manifest_byte_size is None
        or source.intake_manifest_byte_size <= 0
        or source.adapter_config_byte_size is None
        or source.adapter_model_byte_size is None
        or source.tensor_count is None
        or source.tensor_element_count is None
        or source.tensor_payload_byte_size is None
        or source.tensor_count != 392
        or source.tensor_element_count != 10_092_544
        or source.tensor_dtype not in {"F16", "BF16", "F32"}
        or source.tensor_payload_byte_size
        != {"F16": 20_185_088, "BF16": 20_185_088, "F32": 40_370_176}.get(source.tensor_dtype)
    ):
        raise ServiceError(409, "Adapter source authority changed")


def _require_dataset_contract(dataset: SftDatasetBuild) -> None:
    if (
        dataset.artifact_contract_version != "phase10-sft-dataset-v1"
        or dataset.example_contract_version != "phase10-sft-example-v1"
        or dataset.normalization_version != "phase10-sft-normalization-v1"
        or dataset.split_version != "phase10-sft-group-split-v1"
        or not dataset.result_manifest_sha256
        or not dataset.train_sha256
        or not dataset.validation_sha256
        or not dataset.provenance_sha256
    ):
        raise ServiceError(409, "Dataset authority changed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enqueue an immutable adapter registry build")
    parser.add_argument("--department-id", required=True)
    parser.add_argument("--actor-issuer", required=True)
    parser.add_argument("--actor-subject", required=True)
    parser.add_argument("--source-bundle-id", required=True)
    parser.add_argument("--training-job-id", required=True)
    parser.add_argument("--expected-source-version", type=int, required=True)
    parser.add_argument("--expected-training-job-version", type=int, required=True)
    parser.add_argument("--confirm-declared-training-association", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    from app.database import create_database_engine, create_session_factory

    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("database_unavailable", flush=True)
        return 2
    try:
        department_id = UUID(args.department_id)
        source_id = UUID(args.source_bundle_id)
        job_id = UUID(args.training_job_id)
    except ValueError:
        print("adapter_registry_manifest_invalid", flush=True)
        return 2
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    principal = AuthenticatedPrincipal(issuer=args.actor_issuer, subject=args.actor_subject)
    scope = DepartmentRequestScope(DepartmentScope(department_id))
    try:
        with factory.begin() as session:
            result = enqueue_adapter_registry(
                session,
                principal,
                scope,
                source_bundle_id=source_id,
                training_job_id=job_id,
                expected_source_version=args.expected_source_version,
                expected_training_job_version=args.expected_training_job_version,
                confirm_declared_training_association=args.confirm_declared_training_association,
                apply=args.apply,
            )
        print(result.status, flush=True)
        return 0
    except ServiceError as error:
        print(error.detail, flush=True)
        return 1
    finally:
        engine.dispose()


__all__ = ["AdapterRegistryEnqueueResult", "enqueue_adapter_registry", "main"]

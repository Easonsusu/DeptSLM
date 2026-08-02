"""Leased Phase 12.1C registry worker queue and publication lifecycle."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_registry_artifacts import (
    AdapterRegistryArtifactError,
    AdapterRegistryArtifactStore,
    RegistryStage,
)
from app.adapter_registry_domain import (
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    canonical_json_bytes,
    parse_registry_manifest,
)
from app.adapter_registry_supervision import run_adapter_registry_child
from app.authorization import DepartmentScope
from app.models import (
    Adapter,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    Department,
    Membership,
    PersistentAuditEvent,
    SftArtifactReconciliationOperation,
    SftArtifactReconciliationOperationItem,
    SftDatasetBuild,
    SftDatasetBuildAttempt,
    TrainingJob,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
)
from app.training_job_domain import canonical_json_bytes as training_canonical_json_bytes
from app.training_job_domain import parse_job_manifest


class AdapterRegistryQueueError(RuntimeError):
    SAFE_CODES = frozenset(
        {
            "adapter_source_unavailable",
            "adapter_source_artifact_mismatch",
            "adapter_source_authority_changed",
            "training_job_unavailable",
            "training_job_artifact_mismatch",
            "training_job_authority_changed",
            "dataset_authority_changed",
            "adapter_config_invalid",
            "adapter_config_unsupported",
            "adapter_header_invalid",
            "adapter_header_too_large",
            "adapter_file_too_large",
            "adapter_tensor_set_invalid",
            "adapter_tensor_shape_invalid",
            "adapter_tensor_dtype_invalid",
            "adapter_tensor_offsets_invalid",
            "adapter_tensor_size_invalid",
            "adapter_registry_manifest_invalid",
            "adapter_registry_publication_failed",
            "adapter_registry_authority_changed",
            "department_unavailable",
            "requester_unauthorized",
            "claim_lost",
            "worker_shutdown",
            "worker_timeout",
            "database_unavailable",
        }
    )

    def __init__(self, code: str = "adapter_registry_publication_failed") -> None:
        self.code = code if code in self.SAFE_CODES else "adapter_registry_publication_failed"
        super().__init__(self.code)


_VALIDATION_FAILURE_CODES = frozenset(
    {
        "adapter_source_artifact_mismatch",
        "adapter_source_authority_changed",
        "training_job_artifact_mismatch",
        "training_job_authority_changed",
        "dataset_authority_changed",
        "adapter_config_invalid",
        "adapter_config_unsupported",
        "adapter_header_invalid",
        "adapter_header_too_large",
        "adapter_file_too_large",
        "adapter_tensor_set_invalid",
        "adapter_tensor_shape_invalid",
        "adapter_tensor_dtype_invalid",
        "adapter_tensor_offsets_invalid",
        "adapter_tensor_size_invalid",
        "adapter_registry_manifest_invalid",
    }
)


@dataclass(slots=True)
class ClaimedAdapter:
    id: UUID
    department_id: UUID
    requested_by_user_id: UUID
    source_bundle_id: UUID
    source_authoritative_attempt_id: UUID
    source_publication_attempt_id: UUID
    source_attempt_number: int
    source_version: int
    source_code_revision: str
    source_intake_manifest_sha256: str
    source_intake_manifest_byte_size: int
    source_adapter_config_sha256: str
    source_adapter_config_byte_size: int
    source_adapter_model_sha256: str
    source_adapter_model_byte_size: int
    tensor_dtype: str
    tensor_count: int
    tensor_element_count: int
    tensor_payload_byte_size: int
    training_job_id: UUID
    training_job_version: int
    training_job_publication_attempt_id: UUID
    training_job_attempt_number: int
    training_job_code_revision: str
    training_job_manifest_sha256: str
    training_job_manifest_byte_size: int
    training_job_execution_scope_id: UUID
    training_job_config_sha256: str
    training_job_config_byte_size: int
    training_job_dataset_info_sha256: str
    training_job_dataset_info_byte_size: int
    training_job_train_sha256: str
    training_job_train_byte_size: int
    training_job_validation_sha256: str
    training_job_validation_byte_size: int
    training_job_profile_id: str
    dataset_build_id: UUID
    dataset_build_version: int
    dataset_publication_attempt_id: UUID
    dataset_publication_attempt_number: int
    dataset_code_revision: str
    dataset_manifest_sha256: str
    dataset_source_bundle_id: UUID
    worker_id: UUID
    claim_token: UUID
    publication_attempt_id: UUID
    execution_scope_id: UUID
    attempt_number: int
    code_revision: str
    source: dict[str, object]
    governance_lineage: dict[str, object]
    stale_publication_attempt_id: UUID | None
    adapter_version: int
    registry_attempt_id: UUID
    registry_attempt_version: int
    adapter_status: str
    registry_attempt_status: str
    lease_expires_at: datetime
    source_attempt_version: int
    training_job_attempt_version: int
    dataset_attempt_version: int
    dependency_id: UUID
    dependency_version: int


class _OperationGuard:
    """Independent monotonic guard for one bounded parent operation."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        claim: ClaimedAdapter,
        lease_seconds: int,
        operation_seconds: int,
        should_stop: Callable[[], bool],
    ) -> None:
        self.factory = factory
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.deadline = time.monotonic() + max(1, operation_seconds)
        self.should_stop = should_stop
        self._last_heartbeat = 0.0

    def checkpoint(self) -> None:
        if self.should_stop():
            raise AdapterRegistryQueueError("worker_shutdown")
        if time.monotonic() >= self.deadline:
            raise AdapterRegistryQueueError("worker_timeout")
        _assert_live(self.factory, self.claim)

    def heartbeat(self) -> None:
        self.checkpoint()
        interval = max(0.25, self.lease_seconds / 3)
        now = time.monotonic()
        if now - self._last_heartbeat < interval:
            return
        renewed = renew_adapter_lease(self.factory, self.claim, self.lease_seconds)
        self.claim = renewed
        self._last_heartbeat = now


def claim_next_adapter(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int, code_revision: str
) -> ClaimedAdapter | None:
    if worker_id.int == 0 or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise AdapterRegistryQueueError("database_unavailable")
    try:
        with factory.begin() as session:
            adapter = session.execute(
                select(Adapter)
                .where(
                    Adapter.code_revision == code_revision,
                    (Adapter.status == "queued")
                    | (
                        (Adapter.status == "running")
                        & (Adapter.lease_expires_at <= func.clock_timestamp())
                    ),
                )
                .order_by(Adapter.queued_at, Adapter.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if adapter is None:
                return None
            now = session.scalar(select(func.clock_timestamp()))
            department = session.execute(
                select(Department)
                .where(Department.id == adapter.department_id, Department.status == "active")
                .with_for_update()
            ).scalar_one_or_none()
            if department is None:
                _terminal_claim_failure(session, adapter, now, "department_unavailable")
                return None
            source = session.execute(
                select(AdapterImportSource)
                .where(
                    AdapterImportSource.id == adapter.source_bundle_id,
                    AdapterImportSource.department_id == adapter.department_id,
                    AdapterImportSource.status.in_(("claimed", "consumed")),
                    AdapterImportSource.claimed_adapter_id == adapter.id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if source is None or source.status != "claimed":
                _terminal_claim_failure(session, adapter, now, "adapter_source_unavailable")
                return None
            stale_attempt: UUID | None = None
            if adapter.status == "running":
                stale_attempt = adapter.publication_attempt_id
                old_rows = (
                    session.execute(
                        select(AdapterRegistryAttempt)
                        .where(
                            AdapterRegistryAttempt.adapter_id == adapter.id,
                            AdapterRegistryAttempt.department_id == adapter.department_id,
                            AdapterRegistryAttempt.publication_attempt_id == stale_attempt,
                            AdapterRegistryAttempt.execution_scope_id == adapter.execution_scope_id,
                            AdapterRegistryAttempt.attempt_number == adapter.attempt_number,
                            AdapterRegistryAttempt.worker_id == adapter.worker_id,
                            AdapterRegistryAttempt.code_revision == adapter.code_revision,
                            AdapterRegistryAttempt.status.in_(("running", "staged", "published")),
                        )
                        .with_for_update()
                    )
                    .scalars()
                    .all()
                )
                if len(old_rows) != 1:
                    _terminal_claim_failure(session, adapter, now, "claim_lost")
                    return None
                old = old_rows[0]
                expected_version = {"running": 2, "staged": 3, "published": 4}.get(old.status)
                if (
                    expected_version is None
                    or old.version != expected_version
                    or old.finished_at is not None
                    or old.error_code is not None
                    or (old.status in ("staged", "published") and old.ownership_manifest is None)
                ):
                    _terminal_claim_failure(session, adapter, now, "claim_lost")
                    return None
                old.status = "reclaimed"
                old.error_code = "claim_lost"
                old.finished_at = now
                old.version += 1
                adapter.attempt_number += 1
                adapter.execution_scope_id = uuid4()
                adapter.publication_attempt_id = uuid4()
            adapter.status = "running"
            adapter.worker_id = worker_id
            adapter.claim_token = uuid4()
            adapter.claimed_at = now
            adapter.lease_expires_at = now + timedelta(seconds=lease_seconds)
            adapter.started_at = now
            adapter.finished_at = None
            adapter.validated_at = None
            adapter.error_code = None
            adapter.version += 1
            if stale_attempt is not None:
                # Reclaim creates a fresh durable attempt in this transaction;
                # it must never assume that the replacement row already exists.
                attempt = AdapterRegistryAttempt(
                    id=uuid4(),
                    department_id=adapter.department_id,
                    adapter_id=adapter.id,
                    attempt_number=adapter.attempt_number,
                    publication_attempt_id=adapter.publication_attempt_id,
                    execution_scope_id=adapter.execution_scope_id,
                    code_revision=adapter.code_revision,
                    status="registered",
                )
                session.add(attempt)
                session.flush()
            else:
                attempt_rows = (
                    session.execute(
                        select(AdapterRegistryAttempt)
                        .where(
                            AdapterRegistryAttempt.adapter_id == adapter.id,
                            AdapterRegistryAttempt.department_id == adapter.department_id,
                            AdapterRegistryAttempt.publication_attempt_id
                            == adapter.publication_attempt_id,
                            AdapterRegistryAttempt.execution_scope_id == adapter.execution_scope_id,
                            AdapterRegistryAttempt.attempt_number == adapter.attempt_number,
                        )
                        .with_for_update()
                    )
                    .scalars()
                    .all()
                )
                if len(attempt_rows) != 1 or attempt_rows[0].status != "registered":
                    _terminal_claim_failure(session, adapter, now, "claim_lost")
                    return None
                attempt = attempt_rows[0]
            attempt.status = "running"
            attempt.worker_id = worker_id
            attempt.claimed_at = now
            attempt.version += 1
            dependency = session.execute(
                select(AdapterUpstreamDependency)
                .where(
                    AdapterUpstreamDependency.adapter_id == adapter.id,
                    AdapterUpstreamDependency.department_id == adapter.department_id,
                    AdapterUpstreamDependency.training_job_id == adapter.training_job_id,
                    AdapterUpstreamDependency.dataset_build_id == adapter.dataset_build_id,
                    AdapterUpstreamDependency.status == "active",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if dependency is None:
                _terminal_claim_failure(session, adapter, now, "claim_lost", attempt=attempt)
                return None
            source_attempt = session.execute(
                select(AdapterImportAttempt)
                .where(
                    AdapterImportAttempt.id == adapter.source_authoritative_attempt_id,
                    AdapterImportAttempt.department_id == adapter.department_id,
                    AdapterImportAttempt.source_bundle_id == adapter.source_bundle_id,
                    AdapterImportAttempt.publication_attempt_id
                    == adapter.source_publication_attempt_id,
                    AdapterImportAttempt.attempt_number == adapter.source_attempt_number,
                )
                .with_for_update()
            ).scalar_one_or_none()
            training_attempt = session.execute(
                select(TrainingJobAttempt)
                .where(
                    TrainingJobAttempt.training_job_id == adapter.training_job_id,
                    TrainingJobAttempt.department_id == adapter.department_id,
                    TrainingJobAttempt.publication_attempt_id
                    == adapter.training_job_publication_attempt_id,
                    TrainingJobAttempt.attempt_number == adapter.training_job_attempt_number,
                )
                .with_for_update()
            ).scalar_one_or_none()
            dataset_attempt = session.execute(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.build_id == adapter.dataset_build_id,
                    SftDatasetBuildAttempt.department_id == adapter.department_id,
                    SftDatasetBuildAttempt.publication_attempt_id
                    == adapter.dataset_publication_attempt_id,
                    SftDatasetBuildAttempt.attempt_number
                    == adapter.dataset_publication_attempt_number,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if source_attempt is None or training_attempt is None or dataset_attempt is None:
                _terminal_claim_failure(session, adapter, now, "claim_lost", attempt=attempt)
                return None
            if (
                source_attempt.version != adapter.source_attempt_version
                or training_attempt.version != adapter.training_job_attempt_version
                or dataset_attempt.version != adapter.dataset_attempt_version
            ):
                _terminal_claim_failure(session, adapter, now, "claim_lost", attempt=attempt)
                return None
            return _claimed(
                adapter,
                source,
                worker_id,
                stale_attempt,
                attempt,
                dependency,
                adapter.source_attempt_version,
                adapter.training_job_attempt_version,
                adapter.dataset_attempt_version,
            )
    except AdapterRegistryQueueError:
        raise
    except SQLAlchemyError as error:
        raise AdapterRegistryQueueError("database_unavailable") from error


def renew_adapter_lease(
    factory: sessionmaker[Session], claim: ClaimedAdapter, lease_seconds: int
) -> ClaimedAdapter:
    try:
        with factory.begin() as session:
            row = _live_claim(session, claim)
            now = session.scalar(select(func.clock_timestamp()))
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.version += 1
            claim.lease_expires_at = row.lease_expires_at
            claim.adapter_version = row.version
            return claim
    except AdapterRegistryQueueError:
        raise
    except SQLAlchemyError as error:
        raise AdapterRegistryQueueError("database_unavailable") from error


def process_adapter_registry(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    claim: ClaimedAdapter,
    lease_seconds: int = 300,
    operation_seconds: int = 600,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Run one claim through verified source/Phase11 bytes and final authority."""

    check_stop = should_stop or (lambda: False)
    stage: RegistryStage | None = None
    source_final = None
    training_final = None
    current_guard = _OperationGuard(factory, claim, lease_seconds, operation_seconds, check_stop)

    def begin_operation() -> None:
        nonlocal current_guard
        current_guard = _OperationGuard(
            factory, claim, lease_seconds, operation_seconds, check_stop
        )

    def checkpoint() -> None:
        current_guard.checkpoint()

    def heartbeat() -> None:
        current_guard.heartbeat()

    try:
        checkpoint()
        with AdapterRegistryArtifactStore(data_dir) as store:
            begin_operation()
            source_final = store.open_source_final(
                DepartmentScope(claim.department_id), claim.source_bundle_id
            )
            checkpoint()
            begin_operation()
            training_final = store.open_training_job_final(
                DepartmentScope(claim.department_id), claim.training_job_id
            )
            checkpoint()
            source_final.verify_identity()
            training_final.verify_identity()
            begin_operation()
            _verify_training_bundle_authority(factory, claim, training_final)
            begin_operation()
            stage = store.prepare_registry_stage(
                DepartmentScope(claim.department_id), claim.id, claim.publication_attempt_id
            )
            source_config_fd, source_config_meta = source_final.descriptor("adapter_config.json")
            source_model_fd, source_model_meta = source_final.descriptor(
                "adapter_model.safetensors"
            )
            source_manifest_fd, source_manifest_meta = source_final.descriptor(
                "intake_manifest.json"
            )
            training_manifest_fd, training_manifest_meta = training_final.descriptor(
                "manifest.json"
            )
            begin_operation()
            result = run_adapter_registry_child(
                source_config_fd=source_config_fd,
                source_model_fd=source_model_fd,
                source_manifest_fd=source_manifest_fd,
                training_manifest_fd=training_manifest_fd,
                stage_fd=stage.stage_fd,
                source_config_size=source_config_meta.st_size,
                source_model_size=source_model_meta.st_size,
                source_manifest_size=source_manifest_meta.st_size,
                training_manifest_size=training_manifest_meta.st_size,
                department_id=claim.department_id,
                adapter_id=claim.id,
                publication_attempt_id=claim.publication_attempt_id,
                attempt_number=claim.attempt_number,
                code_revision=claim.code_revision,
                source=claim.source,
                governance_lineage=claim.governance_lineage,
                should_stop=check_stop,
                heartbeat=heartbeat,
            )
            checkpoint()
            source_final.verify_identity()
            training_final.verify_identity()
            _validate_result_against_claim(claim, result)
            begin_operation()
            _mark_staged(factory, claim, result)
            checkpoint()
            begin_operation()
            store.publish_registry(stage)
            checkpoint()
            begin_operation()
            verified = store.verify_registry_final(stage)
            expected_files = {
                "manifest.json": (
                    result["registry_manifest_sha256"],
                    result["registry_manifest_byte_size"],
                ),
                "adapter_config.json": (
                    result["registry_adapter_config_sha256"],
                    result["registry_adapter_config_byte_size"],
                ),
                "adapter_model.safetensors": (
                    result["registry_adapter_model_sha256"],
                    result["registry_adapter_model_byte_size"],
                ),
            }
            if verified != expected_files:
                raise AdapterRegistryQueueError("adapter_registry_authority_changed")
            begin_operation()
            _mark_published(factory, claim, result)
            checkpoint()
            begin_operation()
            heartbeat()
            _finish_success(
                factory,
                claim,
                result,
                source_final=source_final,
                training_final=training_final,
                stage=stage,
            )
            return "succeeded"
    except AdapterRegistryQueueError as error:
        _record_failure(
            factory,
            claim,
            error.code,
            validation=error.code in _VALIDATION_FAILURE_CODES,
        )
        raise
    except AdapterRegistryArtifactError as error:
        _record_failure(
            factory,
            claim,
            error.code,
            validation=error.code in _VALIDATION_FAILURE_CODES,
        )
        raise AdapterRegistryQueueError(error.code) from error
    except (OSError, SQLAlchemyError) as error:
        _record_failure(factory, claim, "database_unavailable", validation=False)
        raise AdapterRegistryQueueError("database_unavailable") from error
    finally:
        if source_final is not None:
            source_final.close()
        if training_final is not None:
            training_final.close()
        if stage is not None:
            stage.close()


def terminal_failure(factory: sessionmaker[Session], claim: ClaimedAdapter, code: str) -> None:
    _record_failure(factory, claim, code, validation=False)


def _claimed(
    adapter: Adapter,
    source: AdapterImportSource,
    worker_id: UUID,
    stale: UUID | None,
    attempt: AdapterRegistryAttempt,
    dependency: AdapterUpstreamDependency,
    source_attempt_version: int,
    training_job_attempt_version: int,
    dataset_attempt_version: int,
) -> ClaimedAdapter:
    source_snapshot = {
        "source_bundle_id": str(adapter.source_bundle_id),
        "authoritative_attempt_id": str(adapter.source_authoritative_attempt_id),
        "publication_attempt_id": str(adapter.source_publication_attempt_id),
        "attempt_number": adapter.source_attempt_number,
        "version": adapter.source_version,
        "imported_by_user_id": str(adapter.source_imported_by_user_id),
        "base_model_id": adapter.base_model_id,
        "base_model_revision": adapter.base_model_revision,
        "base_model_license": adapter.base_model_license,
        "code_revision": adapter.source_code_revision,
        "source_contract_version": adapter.source_contract_version,
        "intake_contract_version": adapter.intake_contract_version,
        "config_contract_version": adapter.config_contract_version,
        "tensor_contract_version": adapter.tensor_contract_version,
        "intake_manifest_sha256": adapter.source_intake_manifest_sha256,
        "intake_manifest_byte_size": adapter.source_intake_manifest_byte_size,
        "adapter_config_sha256": adapter.source_adapter_config_sha256,
        "adapter_config_byte_size": adapter.source_adapter_config_byte_size,
        "adapter_model_sha256": adapter.source_adapter_model_sha256,
        "adapter_model_byte_size": adapter.source_adapter_model_byte_size,
        "peft_version": adapter.peft_version,
        "safetensors_format": adapter.safetensors_format,
        "tensor_dtype": adapter.tensor_dtype,
        "tensor_count": adapter.tensor_count,
        "tensor_element_count": adapter.tensor_element_count,
        "tensor_payload_byte_size": adapter.tensor_payload_byte_size,
    }
    governance = {
        "training_job_id": str(adapter.training_job_id),
        "training_job_version": adapter.training_job_version,
        "training_job_publication_attempt_id": str(adapter.training_job_publication_attempt_id),
        "training_job_attempt_number": adapter.training_job_attempt_number,
        "training_job_code_revision": adapter.training_job_code_revision,
        "training_job_manifest_sha256": adapter.training_job_manifest_sha256,
        "training_job_manifest_byte_size": adapter.training_job_manifest_byte_size,
        "training_job_execution_scope_id": str(adapter.training_job_execution_scope_id),
        "training_job_config_sha256": adapter.training_job_config_sha256,
        "training_job_config_byte_size": adapter.training_job_config_byte_size,
        "training_job_dataset_info_sha256": adapter.training_job_dataset_info_sha256,
        "training_job_dataset_info_byte_size": adapter.training_job_dataset_info_byte_size,
        "training_job_train_sha256": adapter.training_job_train_sha256,
        "training_job_train_byte_size": adapter.training_job_train_byte_size,
        "training_job_validation_sha256": adapter.training_job_validation_sha256,
        "training_job_validation_byte_size": adapter.training_job_validation_byte_size,
        "training_job_profile_id": adapter.training_job_profile_id,
        "training_job_artifact_contract_version": adapter.training_job_artifact_contract_version,
        "training_job_manifest_contract_version": adapter.training_job_manifest_contract_version,
        "training_configuration_contract_version": adapter.training_configuration_contract_version,
        "training_dataset_info_contract_version": adapter.training_dataset_info_contract_version,
        "training_execution_profile_contract_version": (
            adapter.training_execution_profile_contract_version
        ),
        "llamafactory_version": adapter.llamafactory_version,
        "dataset_build_id": str(adapter.dataset_build_id),
        "dataset_build_version": adapter.dataset_build_version,
        "dataset_publication_attempt_id": str(adapter.dataset_publication_attempt_id),
        "dataset_publication_attempt_number": adapter.dataset_publication_attempt_number,
        "dataset_code_revision": adapter.dataset_code_revision,
        "dataset_manifest_sha256": adapter.dataset_manifest_sha256,
        "dataset_source_bundle_id": str(adapter.dataset_source_bundle_id),
        "dataset_artifact_contract_version": adapter.dataset_artifact_contract_version,
        "dataset_example_contract_version": adapter.dataset_example_contract_version,
        "dataset_normalization_version": adapter.dataset_normalization_version,
        "dataset_split_version": adapter.dataset_split_version,
        "dataset_train_sha256": adapter.dataset_train_sha256,
        "dataset_train_byte_size": adapter.dataset_train_byte_size,
        "dataset_validation_sha256": adapter.dataset_validation_sha256,
        "dataset_validation_byte_size": adapter.dataset_validation_byte_size,
        "dataset_provenance_sha256": adapter.dataset_provenance_sha256,
        "dataset_provenance_byte_size": adapter.dataset_provenance_byte_size,
        "dataset_train_example_count": adapter.dataset_train_example_count,
        "dataset_validation_example_count": adapter.dataset_validation_example_count,
        "dataset_source_example_count": adapter.dataset_source_example_count,
        "dataset_source_group_count": adapter.dataset_source_group_count,
        "dataset_source_reference_count": adapter.dataset_source_reference_count,
        "dataset_rights_attested": adapter.dataset_rights_attested,
        "evaluation_contamination_reviewed": adapter.evaluation_contamination_reviewed,
    }
    return ClaimedAdapter(
        id=adapter.id,
        department_id=adapter.department_id,
        requested_by_user_id=adapter.requested_by_user_id,
        source_bundle_id=adapter.source_bundle_id,
        source_authoritative_attempt_id=adapter.source_authoritative_attempt_id,
        source_publication_attempt_id=adapter.source_publication_attempt_id,
        source_attempt_number=adapter.source_attempt_number,
        source_version=adapter.source_version,
        source_code_revision=adapter.source_code_revision,
        source_intake_manifest_sha256=adapter.source_intake_manifest_sha256,
        source_intake_manifest_byte_size=adapter.source_intake_manifest_byte_size,
        source_adapter_config_sha256=adapter.source_adapter_config_sha256,
        source_adapter_config_byte_size=adapter.source_adapter_config_byte_size,
        source_adapter_model_sha256=adapter.source_adapter_model_sha256,
        source_adapter_model_byte_size=adapter.source_adapter_model_byte_size,
        tensor_dtype=adapter.tensor_dtype,
        tensor_count=adapter.tensor_count,
        tensor_element_count=adapter.tensor_element_count,
        tensor_payload_byte_size=adapter.tensor_payload_byte_size,
        training_job_id=adapter.training_job_id,
        training_job_version=adapter.training_job_version,
        training_job_publication_attempt_id=adapter.training_job_publication_attempt_id,
        training_job_attempt_number=adapter.training_job_attempt_number,
        training_job_code_revision=adapter.training_job_code_revision,
        training_job_manifest_sha256=adapter.training_job_manifest_sha256,
        training_job_manifest_byte_size=adapter.training_job_manifest_byte_size,
        training_job_execution_scope_id=adapter.training_job_execution_scope_id,
        training_job_config_sha256=adapter.training_job_config_sha256,
        training_job_config_byte_size=adapter.training_job_config_byte_size,
        training_job_dataset_info_sha256=adapter.training_job_dataset_info_sha256,
        training_job_dataset_info_byte_size=adapter.training_job_dataset_info_byte_size,
        training_job_train_sha256=adapter.training_job_train_sha256,
        training_job_train_byte_size=adapter.training_job_train_byte_size,
        training_job_validation_sha256=adapter.training_job_validation_sha256,
        training_job_validation_byte_size=adapter.training_job_validation_byte_size,
        training_job_profile_id=adapter.training_job_profile_id,
        dataset_build_id=adapter.dataset_build_id,
        dataset_build_version=adapter.dataset_build_version,
        dataset_publication_attempt_id=adapter.dataset_publication_attempt_id,
        dataset_publication_attempt_number=adapter.dataset_publication_attempt_number,
        dataset_code_revision=adapter.dataset_code_revision,
        dataset_manifest_sha256=adapter.dataset_manifest_sha256,
        dataset_source_bundle_id=adapter.dataset_source_bundle_id,
        worker_id=worker_id,
        claim_token=adapter.claim_token,
        publication_attempt_id=adapter.publication_attempt_id,
        execution_scope_id=adapter.execution_scope_id,
        attempt_number=adapter.attempt_number,
        code_revision=adapter.code_revision,
        source=source_snapshot,
        governance_lineage=governance,
        stale_publication_attempt_id=stale,
        adapter_version=adapter.version,
        registry_attempt_id=attempt.id,
        registry_attempt_version=attempt.version,
        adapter_status=adapter.status,
        registry_attempt_status=attempt.status,
        lease_expires_at=adapter.lease_expires_at,
        source_attempt_version=source_attempt_version,
        training_job_attempt_version=training_job_attempt_version,
        dataset_attempt_version=dataset_attempt_version,
        dependency_id=dependency.id,
        dependency_version=dependency.version,
    )


def _live_claim(session: Session, claim: ClaimedAdapter) -> Adapter:
    _lock_claim_context(session, claim)
    row = session.execute(
        select(Adapter)
        .where(
            Adapter.id == claim.id,
            Adapter.department_id == claim.department_id,
            Adapter.status == "running",
            Adapter.worker_id == claim.worker_id,
            Adapter.claim_token == claim.claim_token,
            Adapter.publication_attempt_id == claim.publication_attempt_id,
            Adapter.execution_scope_id == claim.execution_scope_id,
            Adapter.attempt_number == claim.attempt_number,
            Adapter.version == claim.adapter_version,
            Adapter.lease_expires_at > func.clock_timestamp(),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise AdapterRegistryQueueError("claim_lost")
    _verify_current_authority(session, claim, row, context_locked=True)
    return row


def _lock_claim_context(session: Session, claim: ClaimedAdapter) -> None:
    """Lock department and requester membership before the adapter row."""

    department = session.execute(
        select(Department)
        .where(Department.id == claim.department_id, Department.status == "active")
        .with_for_update()
    ).scalar_one_or_none()
    requester = session.execute(
        select(Membership)
        .where(
            Membership.user_id == claim.requested_by_user_id,
            Membership.department_id == claim.department_id,
            Membership.status == "active",
            Membership.role.in_(("system_admin", "department_admin")),
            or_(Membership.expires_at.is_(None), Membership.expires_at > func.clock_timestamp()),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if department is None or requester is None:
        raise AdapterRegistryQueueError("requester_unauthorized")


def _assert_live(factory: sessionmaker[Session], claim: ClaimedAdapter) -> None:
    try:
        with factory.begin() as session:
            _live_claim(session, claim)
    except AdapterRegistryQueueError:
        raise
    except SQLAlchemyError as error:
        raise AdapterRegistryQueueError("database_unavailable") from error


def _mark_staged(
    factory: sessionmaker[Session], claim: ClaimedAdapter, result: dict[str, object]
) -> None:
    with factory.begin() as session:
        adapter = _live_claim(session, claim)
        attempt = _attempt(session, claim)
        attempt.status = "staged"
        attempt.ownership_manifest = result["publication_manifest"]
        attempt.staged_at = session.scalar(select(func.clock_timestamp()))
        attempt.version += 1
        adapter.version += 1
        claim.adapter_version = adapter.version
        claim.registry_attempt_version = attempt.version
        claim.adapter_status = adapter.status
        claim.registry_attempt_status = attempt.status


def _mark_published(
    factory: sessionmaker[Session], claim: ClaimedAdapter, result: dict[str, object]
) -> None:
    with factory.begin() as session:
        adapter = _live_claim(session, claim)
        attempt = _attempt(session, claim)
        attempt.status = "published"
        attempt.published_at = session.scalar(select(func.clock_timestamp()))
        attempt.version += 1
        adapter.version += 1
        claim.adapter_version = adapter.version
        claim.registry_attempt_version = attempt.version
        claim.adapter_status = adapter.status
        claim.registry_attempt_status = attempt.status


def _validate_result_against_claim(claim: ClaimedAdapter, result: dict[str, object]) -> None:
    """Bind the child result to every immutable enqueue snapshot."""

    manifest = result.get("publication_manifest")
    if not isinstance(manifest, dict):
        raise AdapterRegistryQueueError("adapter_registry_manifest_invalid")
    try:
        parse_registry_manifest(canonical_json_bytes(manifest))
    except (TypeError, ValueError, KeyError):
        raise AdapterRegistryQueueError("adapter_registry_manifest_invalid") from None
    source = claim.source
    expected_source = {
        "source_bundle_id": source["source_bundle_id"],
        "authoritative_import_attempt_id": source["authoritative_attempt_id"],
        "import_publication_attempt_id": source["publication_attempt_id"],
        "import_attempt_number": source["attempt_number"],
        "source_code_revision": source["code_revision"],
        "source_contract_version": source["source_contract_version"],
        "intake_contract_version": source["intake_contract_version"],
        "intake_manifest_sha256": source["intake_manifest_sha256"],
        "external_adapter_config_sha256": source["adapter_config_sha256"],
        "external_adapter_config_byte_size": source["adapter_config_byte_size"],
        "external_adapter_model_sha256": source["adapter_model_sha256"],
        "external_adapter_model_byte_size": source["adapter_model_byte_size"],
    }
    governance = dict(claim.governance_lineage)
    governance["profile_id"] = governance.pop("training_job_profile_id")
    compatibility = {
        "base_model_id": BASE_MODEL_ID,
        "base_model_revision": BASE_MODEL_REVISION,
        "base_model_license": BASE_MODEL_LICENSE,
        "peft_version": claim.source["peft_version"],
        "safetensors_format": claim.source["safetensors_format"],
        "tensor_dtype": claim.tensor_dtype,
        "tensor_count": claim.tensor_count,
        "tensor_element_count": claim.tensor_element_count,
        "tensor_payload_byte_size": claim.tensor_payload_byte_size,
        "adapter_config_contract_version": claim.source["config_contract_version"],
        "adapter_tensor_contract_version": claim.source["tensor_contract_version"],
    }
    files = manifest.get("files")
    if (
        manifest.get("department_id") != str(claim.department_id)
        or manifest.get("adapter_id") != str(claim.id)
        or manifest.get("publication_attempt_id") != str(claim.publication_attempt_id)
        or manifest.get("attempt_number") != claim.attempt_number
        or manifest.get("code_revision") != claim.code_revision
        or manifest.get("source") != expected_source
        or manifest.get("governance_lineage") != governance
        or manifest.get("compatibility") != compatibility
        or not isinstance(files, dict)
        or files.get("adapter_model.safetensors", {}).get("sha256")
        != claim.source_adapter_model_sha256
        or files.get("adapter_model.safetensors", {}).get("byte_size")
        != claim.source_adapter_model_byte_size
        or result.get("registry_adapter_config_sha256")
        != manifest.get("files", {}).get("adapter_config.json", {}).get("sha256")
        or result.get("registry_adapter_config_byte_size")
        != manifest.get("files", {}).get("adapter_config.json", {}).get("byte_size")
        or result.get("artifact_contract_version") != manifest.get("artifact_contract_version")
        or result.get("manifest_contract_version") != manifest.get("manifest_contract_version")
        or result.get("registry_adapter_model_sha256") != claim.source_adapter_model_sha256
        or result.get("registry_adapter_model_byte_size") != claim.source_adapter_model_byte_size
        or result.get("tensor_dtype") != claim.tensor_dtype
        or result.get("tensor_count") != claim.tensor_count
        or result.get("tensor_element_count") != claim.tensor_element_count
        or result.get("tensor_payload_byte_size") != claim.tensor_payload_byte_size
    ):
        raise AdapterRegistryQueueError("adapter_registry_authority_changed")


def _verify_retained_registry(stage: RegistryStage, result: dict[str, object]) -> None:
    names = {name for name, _descriptor, _metadata in stage.files}
    if names != {"adapter_config.json", "adapter_model.safetensors", "manifest.json"}:
        raise AdapterRegistryQueueError("adapter_registry_authority_changed")
    expected = {
        "manifest.json": (
            result["registry_manifest_sha256"],
            result["registry_manifest_byte_size"],
        ),
        "adapter_config.json": (
            result["registry_adapter_config_sha256"],
            result["registry_adapter_config_byte_size"],
        ),
        "adapter_model.safetensors": (
            result["registry_adapter_model_sha256"],
            result["registry_adapter_model_byte_size"],
        ),
    }
    for name, descriptor, before in stage.files:
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=stage.stage_fd, follow_symlinks=False)
        if not _same_identity(before, after) or not _same_identity(before, named):
            raise AdapterRegistryQueueError("adapter_registry_authority_changed")
        digest, size = expected[name]
        if size != after.st_size:
            raise AdapterRegistryQueueError("adapter_registry_authority_changed")
        if not isinstance(digest, str) or len(digest) != 64:
            raise AdapterRegistryQueueError("adapter_registry_authority_changed")


def _verify_training_bundle_authority(
    factory_or_session, claim: ClaimedAdapter, training_final
) -> None:
    """Bind every retained Phase 11 file to the current PostgreSQL row."""

    def check(session: Session) -> None:
        training_final.verify_identity()
        digests = training_final.digest_files()
        raw_manifest = training_final.read_small("manifest.json")
        try:
            manifest = parse_job_manifest(raw_manifest)
            if training_canonical_json_bytes(manifest) + b"\n" != raw_manifest:
                raise ValueError("noncanonical training manifest")
        except Exception as error:
            raise AdapterRegistryQueueError("training_job_artifact_mismatch") from error
        job = session.execute(
            select(TrainingJob)
            .where(
                TrainingJob.id == claim.training_job_id,
                TrainingJob.department_id == claim.department_id,
                TrainingJob.status == "succeeded",
                TrainingJob.review_status == "approved",
                TrainingJob.archived_at.is_(None),
                TrainingJob.purged_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()
        job_attempt = session.execute(
            select(TrainingJobAttempt)
            .where(
                TrainingJobAttempt.training_job_id == claim.training_job_id,
                TrainingJobAttempt.department_id == claim.department_id,
                TrainingJobAttempt.status == "succeeded",
                TrainingJobAttempt.publication_attempt_id
                == claim.training_job_publication_attempt_id,
                TrainingJobAttempt.attempt_number == claim.training_job_attempt_number,
                TrainingJobAttempt.version == claim.training_job_attempt_version,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if job is None or job_attempt is None:
            raise AdapterRegistryQueueError("training_job_authority_changed")
        expected = {
            "manifest.json": (
                claim.training_job_manifest_sha256,
                claim.training_job_manifest_byte_size,
            ),
            "training.yaml": (
                claim.training_job_config_sha256,
                claim.training_job_config_byte_size,
            ),
            "dataset_info.json": (
                claim.training_job_dataset_info_sha256,
                claim.training_job_dataset_info_byte_size,
            ),
            "train.jsonl": (claim.training_job_train_sha256, claim.training_job_train_byte_size),
            "validation.jsonl": (
                claim.training_job_validation_sha256,
                claim.training_job_validation_byte_size,
            ),
        }
        manifest_files = manifest.get("files")
        if not isinstance(manifest_files, dict) or set(manifest_files) != {
            "training.yaml",
            "dataset_info.json",
            "train.jsonl",
            "validation.jsonl",
        }:
            raise AdapterRegistryQueueError("training_job_artifact_mismatch")
        for name, (digest, size) in expected.items():
            actual = digests.get(name)
            descriptor = manifest_files.get(name)
            if (
                not isinstance(digest, str)
                or type(size) is not int
                or actual != (digest, size)
                or not isinstance(descriptor, dict)
                or descriptor.get("sha256") != digest
                or descriptor.get("byte_size") != size
            ):
                raise AdapterRegistryQueueError("training_job_artifact_mismatch")
        if (
            job.result_manifest_sha256 != claim.training_job_manifest_sha256
            or job.training_config_sha256 != claim.training_job_config_sha256
            or job.training_config_byte_size != claim.training_job_config_byte_size
            or job.dataset_info_sha256 != claim.training_job_dataset_info_sha256
            or job.dataset_info_byte_size != claim.training_job_dataset_info_byte_size
            or job.train_sha256 != claim.training_job_train_sha256
            or job.train_byte_size != claim.training_job_train_byte_size
            or job.validation_sha256 != claim.training_job_validation_sha256
            or job.validation_byte_size != claim.training_job_validation_byte_size
            or job.execution_scope_id != claim.training_job_execution_scope_id
            or job.attempt_number != claim.training_job_attempt_number
            or job.publication_attempt_id != claim.training_job_publication_attempt_id
            or job.version != claim.training_job_version
            or job_attempt.execution_scope_id != claim.training_job_execution_scope_id
            or job_attempt.code_revision != claim.training_job_code_revision
            or job_attempt.ownership_manifest != manifest
            or manifest.get("department_id") != str(claim.department_id)
            or manifest.get("training_job_id") != str(claim.training_job_id)
            or manifest.get("publication_attempt_id")
            != str(claim.training_job_publication_attempt_id)
            or manifest.get("execution_scope_id") != str(claim.training_job_execution_scope_id)
            or manifest.get("attempt_number") != claim.training_job_attempt_number
            or manifest.get("code_revision") != claim.training_job_code_revision
            or manifest.get("dataset_build_id") != str(claim.dataset_build_id)
            or manifest.get("dataset_build_version") != claim.dataset_build_version
            or manifest.get("dataset_manifest_sha256") != claim.dataset_manifest_sha256
            or manifest.get("profile_id") != claim.training_job_profile_id
            or manifest.get("artifact_contract_version")
            != claim.training_job_artifact_contract_version
            or manifest.get("manifest_contract_version")
            != claim.training_job_manifest_contract_version
            or manifest.get("configuration_contract_version")
            != claim.training_configuration_contract_version
            or manifest.get("dataset_info_contract_version")
            != claim.training_dataset_info_contract_version
            or manifest.get("execution_profile_contract_version")
            != claim.training_execution_profile_contract_version
            or manifest.get("base_model_id") != "Qwen/Qwen3-0.6B"
            or manifest.get("base_model_revision") != "c1899de289a04d12100db370d81485cdf75e47ca"
            or manifest.get("base_model_license") != "Apache-2.0"
            or manifest.get("llamafactory_version") != claim.llamafactory_version
            or manifest.get("dataset_rights_attested") is not True
            or manifest.get("evaluation_contamination_reviewed") is not True
            or manifest.get("maximum_record_content_bytes") != 7680
            or manifest.get("tokenizer_preflight_required") is not True
            or job.publication_manifest != manifest
        ):
            raise AdapterRegistryQueueError("training_job_authority_changed")

    if isinstance(factory_or_session, Session):
        check(factory_or_session)
    else:
        try:
            with factory_or_session.begin() as session:
                check(session)
        except SQLAlchemyError as error:
            raise AdapterRegistryQueueError("database_unavailable") from error


def _same_identity(first, second) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_uid == second.st_uid
        and first.st_nlink == second.st_nlink
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _verify_current_authority(
    session: Session,
    claim: ClaimedAdapter,
    adapter: Adapter,
    *,
    context_locked: bool = False,
) -> None:
    """Revalidate every Phase 10/11/12 snapshot before a success mutation."""

    if not context_locked:
        _lock_claim_context(session, claim)
    if not _adapter_snapshot_matches(adapter, claim):
        raise AdapterRegistryQueueError("adapter_registry_authority_changed")
    source = session.execute(
        select(AdapterImportSource)
        .where(
            AdapterImportSource.id == claim.source_bundle_id,
            AdapterImportSource.department_id == claim.department_id,
            AdapterImportSource.status == "claimed",
            AdapterImportSource.claimed_adapter_id == claim.id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    source_attempt = session.execute(
        select(AdapterImportAttempt)
        .where(
            AdapterImportAttempt.id == claim.source_authoritative_attempt_id,
            AdapterImportAttempt.department_id == claim.department_id,
            AdapterImportAttempt.source_bundle_id == claim.source_bundle_id,
            AdapterImportAttempt.status == "committed",
            AdapterImportAttempt.publication_attempt_id == claim.source_publication_attempt_id,
            AdapterImportAttempt.attempt_number == claim.source_attempt_number,
        )
        .with_for_update()
    ).scalar_one_or_none()
    job = session.execute(
        select(TrainingJob)
        .where(
            TrainingJob.id == claim.training_job_id,
            TrainingJob.department_id == claim.department_id,
            TrainingJob.status == "succeeded",
            TrainingJob.review_status == "approved",
            TrainingJob.archived_at.is_(None),
            TrainingJob.purged_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    job_attempt = session.execute(
        select(TrainingJobAttempt)
        .where(
            TrainingJobAttempt.training_job_id == claim.training_job_id,
            TrainingJobAttempt.department_id == claim.department_id,
            TrainingJobAttempt.status == "succeeded",
            TrainingJobAttempt.publication_attempt_id == claim.training_job_publication_attempt_id,
            TrainingJobAttempt.attempt_number == claim.training_job_attempt_number,
        )
        .with_for_update()
    ).scalar_one_or_none()
    dataset = session.execute(
        select(SftDatasetBuild)
        .where(
            SftDatasetBuild.id == claim.dataset_build_id,
            SftDatasetBuild.department_id == claim.department_id,
            SftDatasetBuild.status == "succeeded",
            SftDatasetBuild.review_status == "approved",
            SftDatasetBuild.purged_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    dataset_attempt = session.execute(
        select(SftDatasetBuildAttempt)
        .where(
            SftDatasetBuildAttempt.build_id == claim.dataset_build_id,
            SftDatasetBuildAttempt.department_id == claim.department_id,
            SftDatasetBuildAttempt.status == "succeeded",
            SftDatasetBuildAttempt.publication_attempt_id == claim.dataset_publication_attempt_id,
            SftDatasetBuildAttempt.attempt_number == claim.dataset_publication_attempt_number,
        )
        .with_for_update()
    ).scalar_one_or_none()
    registry_attempt = session.execute(
        select(AdapterRegistryAttempt)
        .where(
            AdapterRegistryAttempt.id == claim.registry_attempt_id,
            AdapterRegistryAttempt.adapter_id == claim.id,
            AdapterRegistryAttempt.department_id == claim.department_id,
            AdapterRegistryAttempt.attempt_number == claim.attempt_number,
            AdapterRegistryAttempt.publication_attempt_id == claim.publication_attempt_id,
            AdapterRegistryAttempt.execution_scope_id == claim.execution_scope_id,
            AdapterRegistryAttempt.status == claim.registry_attempt_status,
            AdapterRegistryAttempt.version == claim.registry_attempt_version,
        )
        .with_for_update()
    ).scalar_one_or_none()
    dependency = session.execute(
        select(AdapterUpstreamDependency)
        .where(
            AdapterUpstreamDependency.adapter_id == claim.id,
            AdapterUpstreamDependency.department_id == claim.department_id,
            AdapterUpstreamDependency.training_job_id == claim.training_job_id,
            AdapterUpstreamDependency.dataset_build_id == claim.dataset_build_id,
            AdapterUpstreamDependency.status == "active",
            AdapterUpstreamDependency.id == claim.dependency_id,
            AdapterUpstreamDependency.version == claim.dependency_version,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if (
        source is None
        or source_attempt is None
        or job is None
        or job_attempt is None
        or dataset is None
        or dataset_attempt is None
        or registry_attempt is None
        or dependency is None
        or not _source_snapshot_matches(source, source_attempt, claim)
        or not _job_snapshot_matches(job, job_attempt, claim)
        or not _dataset_snapshot_matches(dataset, dataset_attempt, claim)
        or _has_active_purge_reservation(session, claim)
    ):
        raise AdapterRegistryQueueError("adapter_registry_authority_changed")


def _adapter_snapshot_matches(adapter: Adapter, claim: ClaimedAdapter) -> bool:
    return (
        adapter.id == claim.id
        and adapter.department_id == claim.department_id
        and adapter.requested_by_user_id == claim.requested_by_user_id
        and adapter.source_bundle_id == claim.source_bundle_id
        and adapter.source_authoritative_attempt_id == claim.source_authoritative_attempt_id
        and adapter.source_publication_attempt_id == claim.source_publication_attempt_id
        and adapter.source_attempt_number == claim.source_attempt_number
        and adapter.source_version == claim.source_version
        and adapter.source_attempt_version == claim.source_attempt_version
        and str(adapter.source_imported_by_user_id) == claim.source["imported_by_user_id"]
        and adapter.source_code_revision == claim.source_code_revision
        and adapter.source_contract_version == claim.source["source_contract_version"]
        and adapter.intake_contract_version == claim.source["intake_contract_version"]
        and adapter.config_contract_version == claim.source["config_contract_version"]
        and adapter.tensor_contract_version == claim.source["tensor_contract_version"]
        and adapter.source_intake_manifest_sha256 == claim.source_intake_manifest_sha256
        and adapter.source_intake_manifest_byte_size == claim.source_intake_manifest_byte_size
        and adapter.source_adapter_config_sha256 == claim.source_adapter_config_sha256
        and adapter.source_adapter_config_byte_size == claim.source_adapter_config_byte_size
        and adapter.source_adapter_model_sha256 == claim.source_adapter_model_sha256
        and adapter.source_adapter_model_byte_size == claim.source_adapter_model_byte_size
        and adapter.peft_version == claim.source["peft_version"]
        and adapter.safetensors_format == claim.source["safetensors_format"]
        and adapter.tensor_dtype == claim.tensor_dtype
        and adapter.tensor_count == claim.tensor_count
        and adapter.tensor_element_count == claim.tensor_element_count
        and adapter.tensor_payload_byte_size == claim.tensor_payload_byte_size
        and adapter.training_job_id == claim.training_job_id
        and adapter.training_job_version == claim.training_job_version
        and adapter.training_job_publication_attempt_id == claim.training_job_publication_attempt_id
        and adapter.training_job_attempt_number == claim.training_job_attempt_number
        and adapter.training_job_attempt_version == claim.training_job_attempt_version
        and adapter.training_job_code_revision == claim.training_job_code_revision
        and adapter.training_job_execution_scope_id == claim.training_job_execution_scope_id
        and adapter.training_job_manifest_sha256 == claim.training_job_manifest_sha256
        and adapter.training_job_manifest_byte_size == claim.training_job_manifest_byte_size
        and adapter.training_job_config_sha256 == claim.training_job_config_sha256
        and adapter.training_job_config_byte_size == claim.training_job_config_byte_size
        and adapter.training_job_dataset_info_sha256 == claim.training_job_dataset_info_sha256
        and adapter.training_job_dataset_info_byte_size == claim.training_job_dataset_info_byte_size
        and adapter.training_job_train_sha256 == claim.training_job_train_sha256
        and adapter.training_job_train_byte_size == claim.training_job_train_byte_size
        and adapter.training_job_validation_sha256 == claim.training_job_validation_sha256
        and adapter.training_job_validation_byte_size == claim.training_job_validation_byte_size
        and adapter.dataset_build_id == claim.dataset_build_id
        and adapter.dataset_build_version == claim.dataset_build_version
        and adapter.dataset_publication_attempt_id == claim.dataset_publication_attempt_id
        and adapter.dataset_publication_attempt_number == claim.dataset_publication_attempt_number
        and adapter.dataset_attempt_version == claim.dataset_attempt_version
        and adapter.dataset_code_revision == claim.dataset_code_revision
        and adapter.dataset_manifest_sha256 == claim.dataset_manifest_sha256
        and adapter.dataset_source_bundle_id == claim.dataset_source_bundle_id
        and adapter.dataset_artifact_contract_version
        == claim.governance_lineage["dataset_artifact_contract_version"]
        and adapter.dataset_example_contract_version
        == claim.governance_lineage["dataset_example_contract_version"]
        and adapter.dataset_normalization_version
        == claim.governance_lineage["dataset_normalization_version"]
        and adapter.dataset_split_version == claim.governance_lineage["dataset_split_version"]
        and adapter.dataset_train_sha256 == claim.governance_lineage["dataset_train_sha256"]
        and adapter.dataset_train_byte_size == claim.governance_lineage["dataset_train_byte_size"]
        and adapter.dataset_validation_sha256
        == claim.governance_lineage["dataset_validation_sha256"]
        and adapter.dataset_validation_byte_size
        == claim.governance_lineage["dataset_validation_byte_size"]
        and adapter.dataset_provenance_sha256
        == claim.governance_lineage["dataset_provenance_sha256"]
        and adapter.dataset_provenance_byte_size
        == claim.governance_lineage["dataset_provenance_byte_size"]
        and adapter.dataset_train_example_count
        == claim.governance_lineage["dataset_train_example_count"]
        and adapter.dataset_validation_example_count
        == claim.governance_lineage["dataset_validation_example_count"]
        and adapter.dataset_source_example_count
        == claim.governance_lineage["dataset_source_example_count"]
        and adapter.dataset_source_group_count
        == claim.governance_lineage["dataset_source_group_count"]
        and adapter.dataset_source_reference_count
        == claim.governance_lineage["dataset_source_reference_count"]
        and adapter.dataset_rights_attested is True
        and adapter.evaluation_contamination_reviewed is True
        and adapter.code_revision == claim.code_revision
        and adapter.execution_scope_id == claim.execution_scope_id
        and adapter.attempt_number == claim.attempt_number
        and adapter.version == claim.adapter_version
        and adapter.status == claim.adapter_status
        and adapter.worker_id == claim.worker_id
        and adapter.claim_token == claim.claim_token
        and adapter.publication_attempt_id == claim.publication_attempt_id
        and adapter.base_model_id == BASE_MODEL_ID
        and adapter.base_model_revision == BASE_MODEL_REVISION
        and adapter.base_model_license == BASE_MODEL_LICENSE
        and adapter.artifact_contract_version == "phase12-adapter-artifact-v1"
        and adapter.registry_manifest_contract_version == "phase12-adapter-manifest-v1"
        and adapter.declared_external_training_association is True
        and adapter.training_provenance_verified is False
    )


def _source_snapshot_matches(
    source: AdapterImportSource, attempt: AdapterImportAttempt, claim: ClaimedAdapter
) -> bool:
    snapshot = claim.source
    return (
        source.id == claim.source_bundle_id
        and source.department_id == claim.department_id
        and source.authoritative_attempt_id == claim.source_authoritative_attempt_id
        and source.version == claim.source_version
        and source.status == "claimed"
        and source.claimed_adapter_id == claim.id
        and source.claimed_at is not None
        and source.consumed_at is None
        and source.purged_at is None
        and source.error_code is None
        and source.code_revision == claim.source_code_revision
        and str(source.imported_by_user_id) == snapshot["imported_by_user_id"]
        and source.intake_manifest_sha256 == claim.source_intake_manifest_sha256
        and source.intake_manifest_byte_size == claim.source_intake_manifest_byte_size
        and source.adapter_config_sha256 == claim.source_adapter_config_sha256
        and source.adapter_config_byte_size == claim.source_adapter_config_byte_size
        and source.adapter_model_sha256 == claim.source_adapter_model_sha256
        and source.adapter_model_byte_size == claim.source_adapter_model_byte_size
        and source.source_contract_version == snapshot["source_contract_version"]
        and source.intake_contract_version == snapshot["intake_contract_version"]
        and source.config_contract_version == snapshot["config_contract_version"]
        and source.tensor_contract_version == snapshot["tensor_contract_version"]
        and source.peft_version == snapshot["peft_version"]
        and source.safetensors_format == snapshot["safetensors_format"]
        and source.base_model_id == snapshot["base_model_id"]
        and source.base_model_revision == snapshot["base_model_revision"]
        and source.base_model_license == snapshot["base_model_license"]
        and source.tensor_dtype == snapshot["tensor_dtype"]
        and source.tensor_count == snapshot["tensor_count"]
        and source.tensor_element_count == snapshot["tensor_element_count"]
        and source.tensor_payload_byte_size == snapshot["tensor_payload_byte_size"]
        and attempt.id == claim.source_authoritative_attempt_id
        and attempt.department_id == claim.department_id
        and attempt.source_bundle_id == claim.source_bundle_id
        and attempt.status == "committed"
        and attempt.publication_attempt_id == claim.source_publication_attempt_id
        and attempt.attempt_number == claim.source_attempt_number
        and attempt.code_revision == claim.source_code_revision
        and attempt.version == claim.source_attempt_version
        and attempt.committed_at is not None
        and _manifest_digest_matches(
            attempt.ownership_manifest,
            claim.source_intake_manifest_sha256,
            claim.source_intake_manifest_byte_size,
        )
    )


def _job_snapshot_matches(
    job: TrainingJob, attempt: TrainingJobAttempt, claim: ClaimedAdapter
) -> bool:
    governance = claim.governance_lineage
    return (
        job.id == claim.training_job_id
        and job.department_id == claim.department_id
        and job.status == "succeeded"
        and job.review_status == "approved"
        and job.archived_at is None
        and job.purged_at is None
        and job.version == claim.training_job_version
        and job.execution_scope_id == claim.training_job_execution_scope_id
        and job.attempt_number == claim.training_job_attempt_number
        and job.publication_attempt_id == claim.training_job_publication_attempt_id
        and job.code_revision == claim.training_job_code_revision
        and job.result_manifest_sha256 == claim.training_job_manifest_sha256
        and job.training_config_sha256 == claim.training_job_config_sha256
        and job.training_config_byte_size == claim.training_job_config_byte_size
        and job.dataset_info_sha256 == claim.training_job_dataset_info_sha256
        and job.dataset_info_byte_size == claim.training_job_dataset_info_byte_size
        and job.train_sha256 == claim.training_job_train_sha256
        and job.train_byte_size == claim.training_job_train_byte_size
        and job.validation_sha256 == claim.training_job_validation_sha256
        and job.validation_byte_size == claim.training_job_validation_byte_size
        and job.profile_id == claim.training_job_profile_id
        and job.base_model_id == BASE_MODEL_ID
        and job.base_model_revision == BASE_MODEL_REVISION
        and job.base_model_license == BASE_MODEL_LICENSE
        and job.llamafactory_version == governance["llamafactory_version"]
        and job.artifact_contract_version == governance["training_job_artifact_contract_version"]
        and job.manifest_contract_version == governance["training_job_manifest_contract_version"]
        and job.configuration_contract_version
        == governance["training_configuration_contract_version"]
        and job.dataset_info_contract_version
        == governance["training_dataset_info_contract_version"]
        and job.execution_profile_contract_version
        == governance["training_execution_profile_contract_version"]
        and job.dataset_build_id == claim.dataset_build_id
        and job.dataset_build_version == claim.dataset_build_version
        and job.dataset_status == "succeeded"
        and job.dataset_review_status == "approved"
        and job.dataset_publication_attempt_id == claim.dataset_publication_attempt_id
        and job.dataset_publication_attempt_number == claim.dataset_publication_attempt_number
        and job.dataset_code_revision == claim.dataset_code_revision
        and job.dataset_manifest_sha256 == claim.dataset_manifest_sha256
        and job.dataset_source_bundle_id == claim.dataset_source_bundle_id
        and job.dataset_artifact_contract_version == governance["dataset_artifact_contract_version"]
        and job.dataset_example_contract_version == governance["dataset_example_contract_version"]
        and job.dataset_normalization_version == governance["dataset_normalization_version"]
        and job.dataset_split_version == governance["dataset_split_version"]
        and job.dataset_train_sha256 == governance["dataset_train_sha256"]
        and job.dataset_train_byte_size == governance["dataset_train_byte_size"]
        and job.dataset_validation_sha256 == governance["dataset_validation_sha256"]
        and job.dataset_validation_byte_size == governance["dataset_validation_byte_size"]
        and job.dataset_provenance_sha256 == governance["dataset_provenance_sha256"]
        and job.dataset_provenance_byte_size == governance["dataset_provenance_byte_size"]
        and job.dataset_train_example_count == governance["dataset_train_example_count"]
        and job.dataset_validation_example_count == governance["dataset_validation_example_count"]
        and job.dataset_source_example_count == governance["dataset_source_example_count"]
        and job.dataset_source_group_count == governance["dataset_source_group_count"]
        and job.dataset_source_reference_count == governance["dataset_source_reference_count"]
        and job.dataset_rights_attested is True
        and job.evaluation_contamination_reviewed is True
        and attempt.attempt_number == claim.training_job_attempt_number
        and attempt.publication_attempt_id == claim.training_job_publication_attempt_id
        and attempt.department_id == claim.department_id
        and attempt.training_job_id == claim.training_job_id
        and attempt.status == "succeeded"
        and attempt.code_revision == claim.training_job_code_revision
        and attempt.execution_scope_id == claim.training_job_execution_scope_id
        and attempt.version == claim.training_job_attempt_version
        and attempt.ownership_manifest == job.publication_manifest
    )


def _dataset_snapshot_matches(
    dataset: SftDatasetBuild, attempt: SftDatasetBuildAttempt, claim: ClaimedAdapter
) -> bool:
    return (
        dataset.id == claim.dataset_build_id
        and dataset.department_id == claim.department_id
        and dataset.status == "succeeded"
        and dataset.review_status == "approved"
        and dataset.purged_at is None
        and dataset.version == claim.dataset_build_version
        and dataset.publication_attempt_id == claim.dataset_publication_attempt_id
        and dataset.attempt_number == claim.dataset_publication_attempt_number
        and dataset.code_revision == claim.dataset_code_revision
        and dataset.result_manifest_sha256 == claim.dataset_manifest_sha256
        and dataset.source_bundle_id == claim.dataset_source_bundle_id
        and dataset.train_sha256 == claim.governance_lineage["dataset_train_sha256"]
        and dataset.validation_sha256 == claim.governance_lineage["dataset_validation_sha256"]
        and dataset.provenance_sha256 == claim.governance_lineage["dataset_provenance_sha256"]
        and dataset.train_byte_size == claim.governance_lineage["dataset_train_byte_size"]
        and dataset.validation_byte_size == claim.governance_lineage["dataset_validation_byte_size"]
        and dataset.provenance_byte_size == claim.governance_lineage["dataset_provenance_byte_size"]
        and dataset.train_example_count == claim.governance_lineage["dataset_train_example_count"]
        and dataset.validation_example_count
        == claim.governance_lineage["dataset_validation_example_count"]
        and dataset.source_example_count == claim.governance_lineage["dataset_source_example_count"]
        and dataset.source_group_count == claim.governance_lineage["dataset_source_group_count"]
        and dataset.source_reference_count
        == claim.governance_lineage["dataset_source_reference_count"]
        and dataset.artifact_contract_version
        == claim.governance_lineage["dataset_artifact_contract_version"]
        and dataset.example_contract_version
        == claim.governance_lineage["dataset_example_contract_version"]
        and dataset.normalization_version
        == claim.governance_lineage["dataset_normalization_version"]
        and dataset.split_version == claim.governance_lineage["dataset_split_version"]
        and attempt.department_id == claim.department_id
        and attempt.build_id == claim.dataset_build_id
        and attempt.status == "succeeded"
        and attempt.publication_attempt_id == claim.dataset_publication_attempt_id
        and attempt.attempt_number == claim.dataset_publication_attempt_number
        and attempt.code_revision == claim.dataset_code_revision
        and attempt.version == claim.dataset_attempt_version
        and attempt.published_at is not None
        and attempt.finished_at is not None
    )


def _has_active_purge_reservation(session: Session, claim: ClaimedAdapter) -> bool:
    job_reserved = session.scalar(
        select(func.count(TrainingJobPurgeReservation.id)).where(
            TrainingJobPurgeReservation.department_id == claim.department_id,
            TrainingJobPurgeReservation.training_job_id == claim.training_job_id,
            TrainingJobPurgeReservation.status.in_(
                ("registered", "deletion_authorized", "tombstone_bound")
            ),
        )
    )
    dataset_reserved = session.scalar(
        select(func.count(SftArtifactReconciliationOperationItem.id))
        .join(
            SftArtifactReconciliationOperation,
            SftArtifactReconciliationOperation.id
            == SftArtifactReconciliationOperationItem.operation_id,
        )
        .where(
            SftArtifactReconciliationOperation.department_id == claim.department_id,
            SftArtifactReconciliationOperation.operation_type == "purge",
            SftArtifactReconciliationOperation.status == "registered",
            SftArtifactReconciliationOperationItem.department_id == claim.department_id,
            SftArtifactReconciliationOperationItem.resource_type == "dataset_final",
            SftArtifactReconciliationOperationItem.resource_id == claim.dataset_build_id,
            SftArtifactReconciliationOperationItem.status == "registered",
        )
    )
    return bool(job_reserved or dataset_reserved)


def _manifest_digest_matches(value: object, expected_sha256: str, expected_size: int) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        raw = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    return len(raw) == expected_size and hashlib.sha256(raw).hexdigest() == expected_sha256


def _finish_success(
    factory: sessionmaker[Session],
    claim: ClaimedAdapter,
    result: dict[str, object],
    *,
    source_final=None,
    training_final=None,
    stage: RegistryStage | None = None,
) -> None:
    if source_final is None or training_final is None or stage is None:
        raise AdapterRegistryQueueError("adapter_registry_authority_changed")
    with factory.begin() as session:
        # These are retained descriptors opened before the transaction.  The
        # final transaction may recheck their identity, but it never reopens a
        # payload pathname or performs filesystem mutation.
        source_final.verify_identity()
        training_final.verify_identity()
        stage.recheck()
        _verify_retained_registry(stage, result)
        adapter = _live_claim(session, claim)
        _verify_training_bundle_authority(session, claim, training_final)
        source = session.execute(
            select(AdapterImportSource)
            .where(
                AdapterImportSource.id == claim.source_bundle_id,
                AdapterImportSource.department_id == claim.department_id,
                AdapterImportSource.status == "claimed",
                AdapterImportSource.claimed_adapter_id == claim.id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        dependency = session.execute(
            select(AdapterUpstreamDependency)
            .where(
                AdapterUpstreamDependency.adapter_id == claim.id,
                AdapterUpstreamDependency.department_id == claim.department_id,
                AdapterUpstreamDependency.training_job_id == claim.training_job_id,
                AdapterUpstreamDependency.dataset_build_id == claim.dataset_build_id,
                AdapterUpstreamDependency.id == claim.dependency_id,
                AdapterUpstreamDependency.version == claim.dependency_version,
                AdapterUpstreamDependency.status == "active",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if source is None or dependency is None:
            raise AdapterRegistryQueueError("adapter_registry_authority_changed")
        attempt = _attempt(session, claim)
        if (
            attempt.status != "published"
            or attempt.ownership_manifest != result["publication_manifest"]
        ):
            raise AdapterRegistryQueueError("adapter_registry_authority_changed")
        now = session.scalar(select(func.clock_timestamp()))
        adapter.status = "validated"
        adapter.worker_id = None
        adapter.claim_token = None
        adapter.lease_expires_at = None
        adapter.validated_at = now
        adapter.finished_at = now
        adapter.registry_manifest_sha256 = result["registry_manifest_sha256"]
        adapter.registry_adapter_config_sha256 = result["registry_adapter_config_sha256"]
        adapter.registry_adapter_config_byte_size = result["registry_adapter_config_byte_size"]
        adapter.registry_adapter_model_sha256 = result["registry_adapter_model_sha256"]
        adapter.registry_adapter_model_byte_size = result["registry_adapter_model_byte_size"]
        adapter.verified_governance_lineage = True
        adapter.verified_artifact_compatibility = True
        adapter.training_provenance_verified = False
        adapter.version += 1
        attempt.status = "succeeded"
        attempt.finished_at = now
        attempt.version += 1
        source.status = "consumed"
        source.consumed_at = now
        source.version += 1
        session.add(
            PersistentAuditEvent(
                actor_subject=str(claim.worker_id),
                actor_user_id=adapter.requested_by_user_id,
                department_id=claim.department_id,
                action="adapter.registry.publish",
                resource_type="adapter",
                resource_id=str(adapter.id),
                result="allowed",
                reason_code="mutation_applied",
            )
        )


def _attempt(session: Session, claim: ClaimedAdapter) -> AdapterRegistryAttempt:
    attempt = session.execute(
        select(AdapterRegistryAttempt)
        .where(
            AdapterRegistryAttempt.id == claim.registry_attempt_id,
            AdapterRegistryAttempt.adapter_id == claim.id,
            AdapterRegistryAttempt.department_id == claim.department_id,
            AdapterRegistryAttempt.publication_attempt_id == claim.publication_attempt_id,
            AdapterRegistryAttempt.execution_scope_id == claim.execution_scope_id,
            AdapterRegistryAttempt.attempt_number == claim.attempt_number,
            AdapterRegistryAttempt.version == claim.registry_attempt_version,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if attempt is None:
        raise AdapterRegistryQueueError("adapter_registry_authority_changed")
    return attempt


def _record_failure(
    factory: sessionmaker[Session], claim: ClaimedAdapter, code: str, *, validation: bool
) -> None:
    code = (
        code
        if code in AdapterRegistryQueueError.SAFE_CODES
        else "adapter_registry_publication_failed"
    )
    try:
        with factory.begin() as session:
            try:
                adapter = _live_claim(session, claim)
                attempt = _attempt(session, claim)
            except AdapterRegistryQueueError:
                # A lost or expired claim is never allowed to mutate either
                # the adapter or its historical attempt.
                return
            if attempt.status != claim.registry_attempt_status or attempt.status not in {
                "registered",
                "running",
                "staged",
                "published",
            }:
                return
            now = session.scalar(select(func.clock_timestamp()))
            adapter.status = "validation_failed" if validation else "failed"
            adapter.error_code = code
            adapter.worker_id = None
            adapter.claim_token = None
            adapter.lease_expires_at = None
            adapter.finished_at = now
            adapter.version += 1
            attempt.status = "validation_failed" if validation else "failed"
            attempt.error_code = code
            previous_status = claim.registry_attempt_status
            if previous_status in ("registered", "running"):
                attempt.worker_id = None
                attempt.claimed_at = None
                attempt.ownership_manifest = None
            attempt.finished_at = now
            attempt.version += 1
    except (AdapterRegistryQueueError, SQLAlchemyError):
        return


def _terminal_claim_failure(
    session: Session,
    adapter: Adapter,
    now,
    code: str,
    *,
    attempt: AdapterRegistryAttempt | None = None,
) -> None:
    code = (
        code
        if code in AdapterRegistryQueueError.SAFE_CODES
        else "adapter_registry_publication_failed"
    )
    targets: list[AdapterRegistryAttempt] = []
    if attempt is None:
        targets = (
            session.execute(
                select(AdapterRegistryAttempt)
                .where(
                    AdapterRegistryAttempt.adapter_id == adapter.id,
                    AdapterRegistryAttempt.department_id == adapter.department_id,
                    AdapterRegistryAttempt.publication_attempt_id == adapter.publication_attempt_id,
                    AdapterRegistryAttempt.execution_scope_id == adapter.execution_scope_id,
                    AdapterRegistryAttempt.attempt_number == adapter.attempt_number,
                    AdapterRegistryAttempt.code_revision == adapter.code_revision,
                    AdapterRegistryAttempt.status.in_(
                        ("registered", "running", "staged", "published")
                    ),
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
    else:
        targets = [attempt]
    if attempt is None and len(targets) > 1:
        # A duplicate active surface cannot be safely assigned to one
        # terminal transition.  Leave all rows and bytes untouched.
        return
    for target in targets:
        previous_status = target.status
        if (
            previous_status not in ("registered", "running", "staged", "published")
            or target.adapter_id != adapter.id
            or target.department_id != adapter.department_id
            or target.publication_attempt_id != adapter.publication_attempt_id
            or target.execution_scope_id != adapter.execution_scope_id
            or target.attempt_number != adapter.attempt_number
            or target.code_revision != adapter.code_revision
        ):
            return
        target.status = "failed"
        target.error_code = code
        if previous_status in ("registered", "running"):
            target.worker_id = None
            target.claimed_at = None
            target.ownership_manifest = None
        target.finished_at = now
        target.version += 1
    adapter.status = "failed"
    adapter.error_code = code
    adapter.worker_id = None
    adapter.claim_token = None
    adapter.lease_expires_at = None
    adapter.finished_at = now
    adapter.version += 1


__all__ = [
    "AdapterRegistryQueueError",
    "ClaimedAdapter",
    "claim_next_adapter",
    "renew_adapter_lease",
    "process_adapter_registry",
    "terminal_failure",
]

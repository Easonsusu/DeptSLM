"""Offline, model-free worker for Phase 12.3 deployment governance.

The worker verifies the exact registry final through retained descriptor handles
before taking the short PostgreSQL deployment lock.  It never loads PEFT or a
model and it never changes production RAG routing; the deployment pointer is
metadata-only until a later reviewed phase.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_governance_services import _validate_approved_target
from app.adapter_registry_artifacts import (
    AdapterRegistryArtifactError,
    AdapterRegistryArtifactStore,
    RetainedFinal,
)
from app.adapter_registry_domain import ADAPTER_ARTIFACT_CONTRACT_VERSION, parse_registry_manifest
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.models import (
    ADAPTER_GOVERNANCE_ERROR_CODES,
    Adapter,
    AdapterDeploymentEvent,
    AdapterDeploymentOperation,
    AdapterEvaluationRun,
    AdapterRegistryAttempt,
    AdapterReview,
    AdapterRollbackRetention,
    AdapterUpstreamDependency,
    DepartmentAdapterDeployment,
    EvaluationSuite,
    UserIdentity,
)
from app.services import ServiceError, append_mutation_audit
from app.settings import Settings

LEASE_SECONDS = 300
POLL_SECONDS = 5
_BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
_BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"


class AdapterGovernanceWorkerError(RuntimeError):
    """Safe worker failure without exception text persistence."""


def verify_registry_final(
    data_dir: Path,
    *,
    department_id: UUID,
    adapter_id: UUID,
    expected_manifest_sha256: str,
    expected_config_sha256: str,
    expected_config_size: int,
    expected_model_sha256: str,
    expected_model_size: int,
    expected_publication_attempt_id: UUID,
    expected_attempt_number: int,
    expected_execution_scope_id: UUID,
    expected_code_revision: str,
    expected_base_model_id: str = _BASE_MODEL_ID,
    expected_base_model_revision: str = _BASE_MODEL_REVISION,
    expected_artifact_contract_version: str | None = None,
    expected_runner_contract_version: str | None = None,
    expected_metric_contract_version: str | None = None,
    expected_gate_policy_version: str | None = None,
    expected_seed_policy_version: str | None = None,
    heartbeat: Callable[[], None] | None = None,
    retain_open: bool = False,
) -> RetainedFinal | None:
    """Verify the exact allowlisted final without reopening a pathname.

    Adapter bytes remain in external storage and are read only through the
    retained descriptor object.  This function intentionally imports no model
    or tensor runtime.  A deployment operation must re-run it immediately
    before its final transaction.
    """

    retained = None
    try:
        if expected_execution_scope_id.int == 0:
            raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
        with AdapterRegistryArtifactStore(data_dir) as store:
            retained = store.open_registry_final(DepartmentScope(department_id), adapter_id)
            if heartbeat is not None:
                heartbeat()
            raw_manifest = retained.read_small("manifest.json")
            manifest = parse_registry_manifest(raw_manifest)
            manifest_digest = hashlib.sha256(raw_manifest).hexdigest()
            if manifest_digest != expected_manifest_sha256:
                raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            if (
                manifest.get("department_id") != str(department_id)
                or manifest.get("adapter_id") != str(adapter_id)
                or manifest.get("publication_attempt_id") != str(expected_publication_attempt_id)
                or manifest.get("attempt_number") != expected_attempt_number
                or manifest.get("code_revision") != expected_code_revision
            ):
                raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            compatibility = manifest.get("compatibility")
            if (
                not isinstance(compatibility, dict)
                or compatibility.get("base_model_id") != expected_base_model_id
                or compatibility.get("base_model_revision") != expected_base_model_revision
            ):
                raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            if (
                expected_artifact_contract_version is not None
                and manifest.get("artifact_contract_version") != expected_artifact_contract_version
            ):
                raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            if heartbeat is not None:
                heartbeat()
            digests = retained.digest_files()
            files = manifest.get("files")
            if (
                not isinstance(files, dict)
                or set(files)
                != {"manifest.json", "adapter_config.json", "adapter_model.safetensors"}
                or files["manifest.json"].get("sha256") != expected_manifest_sha256
                or files["manifest.json"].get("byte_size") != len(raw_manifest)
            ):
                raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            if digests.get("adapter_config.json") != (expected_config_sha256, expected_config_size):
                raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            if digests.get("adapter_model.safetensors") != (
                expected_model_sha256,
                expected_model_size,
            ):
                raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            for name, digest in (
                ("adapter_config.json", expected_config_sha256),
                ("adapter_model.safetensors", expected_model_sha256),
            ):
                descriptor = files.get(name)
                if (
                    not isinstance(descriptor, dict)
                    or descriptor.get("sha256") != digest
                    or descriptor.get("byte_size") != digests[name][1]
                ):
                    raise AdapterGovernanceWorkerError("registry_artifact_mismatch")
            retained.verify_identity()
            if heartbeat is not None:
                heartbeat()
            if retain_open:
                return retained
            retained.close()
            retained = None
            return None
    except AdapterGovernanceWorkerError:
        if retained is not None:
            retained.close()
        raise
    except AdapterRegistryArtifactError as error:
        if retained is not None:
            retained.close()
        code = (
            error.args[0]
            if error.args
            and error.args[0]
            in {"adapter_registry_authority_changed", "adapter_registry_manifest_invalid"}
            else "registry_artifact_missing"
        )
        raise AdapterGovernanceWorkerError(code) from None
    except (OSError, ValueError):
        if retained is not None:
            retained.close()
        raise AdapterGovernanceWorkerError("registry_artifact_unsafe") from None


def _heartbeat_owned(factory: sessionmaker[Session], operation: AdapterDeploymentOperation) -> None:
    with factory.begin() as session:
        row = session.scalar(
            select(AdapterDeploymentOperation)
            .where(
                AdapterDeploymentOperation.id == operation.id,
                AdapterDeploymentOperation.department_id == operation.department_id,
            )
            .with_for_update()
        )
        if row is None or not _claim_is_live(session, row):
            raise AdapterGovernanceWorkerError("claim_lost")
        now = session.scalar(select(func.clock_timestamp()))
        row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        row.version += 1


def _operation_authority_matches(
    operation: AdapterDeploymentOperation,
    adapter: Adapter,
    review: AdapterReview,
    run: AdapterEvaluationRun,
    suite: EvaluationSuite,
    registry: AdapterRegistryAttempt,
    dependency: AdapterUpstreamDependency,
) -> bool:
    return (
        operation.target_adapter_id == adapter.id
        and operation.target_adapter_version == adapter.version
        and operation.target_review_id == review.id
        and operation.target_review_version == review.version
        and operation.target_evaluation_id == review.evaluation_id
        and operation.target_evaluation_version == review.evaluation_version
        and operation.registry_attempt_id == registry.id
        and operation.registry_attempt_version == registry.version
        and operation.registry_publication_attempt_id == registry.publication_attempt_id
        and operation.registry_attempt_number == registry.attempt_number
        and operation.registry_execution_scope_id == registry.execution_scope_id
        and operation.registry_manifest_sha256 == adapter.registry_manifest_sha256
        and operation.registry_adapter_config_sha256 == adapter.registry_adapter_config_sha256
        and operation.registry_adapter_config_byte_size == adapter.registry_adapter_config_byte_size
        and operation.registry_adapter_model_sha256 == adapter.registry_adapter_model_sha256
        and operation.registry_adapter_model_byte_size == adapter.registry_adapter_model_byte_size
        and operation.dependency_id == dependency.id
        and operation.dependency_version == dependency.version
        and operation.suite_id == suite.id
        and operation.suite_version == suite.version
        and operation.base_model_id == run.base_model_id
        and operation.base_model_revision == run.base_model_revision
        and operation.runner_contract_version == run.runner_contract_version
        and operation.artifact_contract_version == run.artifact_contract_version
        and operation.metric_contract_version == run.metric_contract_version
        and operation.gate_policy_version == run.gate_policy_version
        and operation.seed_policy_version == run.seed_policy_version
        and operation.code_revision == run.code_revision
        and operation.suite_artifact_manifest_sha256 == run.suite_artifact_manifest_sha256
        and operation.suite_canonical_cases_sha256 == run.suite_canonical_cases_sha256
        and operation.suite_canonical_cases_byte_size == run.suite_canonical_cases_byte_size
        and operation.result_manifest_sha256 == run.result_manifest_sha256
        and operation.result_summary_sha256 == run.result_summary_sha256
        and operation.case_results_sha256 == run.case_results_sha256
        and operation.case_results_byte_size == run.case_results_byte_size
    )


def claim_next_operation(
    factory: sessionmaker[Session], *, worker_id: UUID, lease_seconds: int = LEASE_SECONDS
) -> AdapterDeploymentOperation | None:
    if worker_id.int == 0 or lease_seconds <= 0:
        raise ValueError("invalid worker claim")
    with factory.begin() as session:
        candidate = session.scalar(
            select(AdapterDeploymentOperation.id)
            .where(
                (AdapterDeploymentOperation.status == "queued")
                | (
                    (AdapterDeploymentOperation.status == "running")
                    & (AdapterDeploymentOperation.lease_expires_at <= func.clock_timestamp())
                )
            )
            .order_by(AdapterDeploymentOperation.created_at, AdapterDeploymentOperation.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            return None
        operation = session.scalar(
            select(AdapterDeploymentOperation)
            .where(AdapterDeploymentOperation.id == candidate)
            .with_for_update()
        )
        if operation is None:
            return None
        now = session.scalar(select(func.clock_timestamp()))
        was_started = operation.started_at is not None
        operation.status = "running"
        operation.worker_id = worker_id
        operation.claim_token = uuid4()
        operation.claimed_at = now
        operation.lease_expires_at = now + timedelta(seconds=lease_seconds)
        operation.started_at = operation.started_at or now
        operation.attempt_number += 1 if was_started else 0
        operation.version += 1
        session.flush()
        session.expunge(operation)
        return operation


def _claim_is_live(session: Session, operation: AdapterDeploymentOperation) -> bool:
    return (
        session.scalar(
            select(AdapterDeploymentOperation.id).where(
                AdapterDeploymentOperation.id == operation.id,
                AdapterDeploymentOperation.department_id == operation.department_id,
                AdapterDeploymentOperation.status == "running",
                AdapterDeploymentOperation.worker_id == operation.worker_id,
                AdapterDeploymentOperation.claim_token == operation.claim_token,
                AdapterDeploymentOperation.cancellation_requested_at.is_(None),
                AdapterDeploymentOperation.lease_expires_at > func.clock_timestamp(),
            )
        )
        is not None
    )


def _retention_for_outgoing(
    session: Session,
    *,
    department_id: UUID,
    adapter_id: UUID,
    adapter_version: int,
    review_id: UUID,
    review_version: int,
    evaluation_id: UUID,
    evaluation_version: int,
    suite_id: UUID,
    event_id: UUID,
) -> AdapterRollbackRetention:
    existing = session.scalar(
        select(AdapterRollbackRetention)
        .where(
            AdapterRollbackRetention.department_id == department_id,
            AdapterRollbackRetention.adapter_id == adapter_id,
            AdapterRollbackRetention.adapter_version == adapter_version,
            AdapterRollbackRetention.status == "active",
        )
        .with_for_update()
    )
    if existing is not None:
        return existing
    row = AdapterRollbackRetention(
        id=uuid4(),
        department_id=department_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        approved_review_id=review_id,
        review_version=review_version,
        evaluation_id=evaluation_id,
        evaluation_version=evaluation_version,
        suite_id=suite_id,
        creation_event_id=event_id,
        status="active",
        version=1,
    )
    session.add(row)
    session.flush()
    return row


def finalize_owned_operation(
    factory: sessionmaker[Session],
    operation: AdapterDeploymentOperation,
    *,
    data_dir: Path,
) -> bool:
    """Verify and commit one claimed operation exactly once."""

    retained_final: RetainedFinal | None = None
    with factory.begin() as session:
        claimed = session.get(AdapterDeploymentOperation, operation.id)
        if claimed is None or not _claim_is_live(session, claimed):
            raise AdapterGovernanceWorkerError("claim_lost")

    if operation.target_adapter_id is not None:
        if any(
            value is None
            for value in (
                operation.registry_publication_attempt_id,
                operation.registry_attempt_number,
                operation.registry_execution_scope_id,
                operation.code_revision,
                operation.registry_manifest_sha256,
                operation.registry_adapter_config_sha256,
                operation.registry_adapter_config_byte_size,
                operation.registry_adapter_model_sha256,
                operation.registry_adapter_model_byte_size,
            )
        ):
            raise AdapterGovernanceWorkerError("adapter_authority_changed")
        retained_final = verify_registry_final(
            data_dir,
            department_id=operation.department_id,
            adapter_id=operation.target_adapter_id,
            expected_manifest_sha256=operation.registry_manifest_sha256 or "",
            expected_config_sha256=operation.registry_adapter_config_sha256 or "",
            expected_config_size=operation.registry_adapter_config_byte_size or 0,
            expected_model_sha256=operation.registry_adapter_model_sha256 or "",
            expected_model_size=operation.registry_adapter_model_byte_size or 0,
            expected_publication_attempt_id=operation.registry_publication_attempt_id,
            expected_attempt_number=operation.registry_attempt_number,
            expected_execution_scope_id=operation.registry_execution_scope_id,
            expected_code_revision=operation.code_revision,
            expected_artifact_contract_version=ADAPTER_ARTIFACT_CONTRACT_VERSION,
            expected_runner_contract_version=operation.runner_contract_version,
            expected_metric_contract_version=operation.metric_contract_version,
            expected_gate_policy_version=operation.gate_policy_version,
            expected_seed_policy_version=operation.seed_policy_version,
            heartbeat=lambda: _heartbeat_owned(factory, operation),
            retain_open=True,
        )
    with factory.begin() as session:
        current_operation = session.scalar(
            select(AdapterDeploymentOperation)
            .where(
                AdapterDeploymentOperation.id == operation.id,
                AdapterDeploymentOperation.department_id == operation.department_id,
            )
            .with_for_update()
        )
        if current_operation is None:
            if retained_final is not None:
                retained_final.close()
            raise AdapterGovernanceWorkerError("deployment_operation_conflict")
        if current_operation.status == "succeeded":
            if retained_final is not None:
                retained_final.close()
            return False
        if current_operation.status != "running" or not _claim_is_live(session, current_operation):
            if retained_final is not None:
                retained_final.close()
            raise AdapterGovernanceWorkerError("claim_lost")
        if retained_final is not None:
            retained_final.verify_identity()
        current = session.scalar(
            select(DepartmentAdapterDeployment)
            .where(DepartmentAdapterDeployment.department_id == current_operation.department_id)
            .with_for_update()
        )
        current_version = current.deployment_version if current is not None else 0
        if current_version != current_operation.expected_deployment_version:
            raise AdapterGovernanceWorkerError("deployment_version_conflict")
        if current_operation.operation_type == "promote":
            authority = _validate_approved_target(
                session,
                current_operation.department_id,
                current_operation.target_adapter_id,
                current_operation.target_adapter_version,
                current_operation.target_review_id,
                current_operation.target_review_version,
            )
            adapter, review, run, suite, registry, dependency, _evidence = authority
            if not _operation_authority_matches(
                current_operation, adapter, review, run, suite, registry, dependency
            ):
                raise AdapterGovernanceWorkerError("adapter_authority_changed")
            event_type = "promote"
            target_kind = "adapter"
        elif current_operation.operation_type == "rollback_adapter":
            authority = _validate_approved_target(
                session,
                current_operation.department_id,
                current_operation.target_adapter_id,
                current_operation.target_adapter_version,
                current_operation.target_review_id,
                current_operation.target_review_version,
            )
            adapter, review, run, suite, registry, dependency, _evidence = authority
            if not _operation_authority_matches(
                current_operation, adapter, review, run, suite, registry, dependency
            ):
                raise AdapterGovernanceWorkerError("adapter_authority_changed")
            retention = session.scalar(
                select(AdapterRollbackRetention)
                .where(
                    AdapterRollbackRetention.id == current_operation.target_retention_id,
                    AdapterRollbackRetention.department_id == current_operation.department_id,
                    AdapterRollbackRetention.adapter_id == adapter.id,
                    AdapterRollbackRetention.status == "active",
                    AdapterRollbackRetention.version == current_operation.target_retention_version,
                )
                .with_for_update()
            )
            if (
                retention is None
                or retention.approved_review_id != review.id
                or retention.review_version != review.version
                or retention.evaluation_id != run.id
                or retention.evaluation_version != run.version
                or retention.suite_id != suite.id
            ):
                raise AdapterGovernanceWorkerError("rollback_target_unavailable")
            event_type = "rollback_adapter"
            target_kind = "adapter"
        else:
            adapter = review = run = None
            event_type = "rollback_base"
            target_kind = "base"
        now = session.scalar(select(func.clock_timestamp()))
        before_kind = current.target_kind if current is not None else "base"
        before_adapter_id = current.adapter_id if current is not None else None
        before_adapter_version = current.adapter_version if current is not None else None
        after_version = current_version + 1
        event = AdapterDeploymentEvent(
            id=uuid4(),
            department_id=current_operation.department_id,
            operation_id=current_operation.id,
            event_type=event_type,
            deployment_version_before=current_version,
            deployment_version_after=after_version,
            from_target_kind=before_kind,
            from_adapter_id=before_adapter_id,
            from_adapter_version=before_adapter_version,
            to_target_kind=target_kind,
            to_adapter_id=adapter.id if adapter is not None else None,
            to_adapter_version=adapter.version if adapter is not None else None,
            approved_review_id=review.id if review is not None else None,
            approved_review_version=review.version if review is not None else None,
            evaluation_id=run.id if run is not None else None,
            evaluation_version=run.version if run is not None else None,
            suite_id=run.suite_id if run is not None else None,
            base_model_id=_BASE_MODEL_ID,
            base_model_revision=_BASE_MODEL_REVISION,
            rollback_retention_id=current_operation.target_retention_id
            if event_type == "rollback_adapter"
            else None,
            actor_user_id=current_operation.requested_by_user_id,
        )
        session.add(event)
        session.flush()
        if (
            current is not None
            and current.target_kind == "adapter"
            and current.adapter_id is not None
        ):
            outgoing_review = session.scalar(
                select(AdapterReview).where(
                    AdapterReview.id == current.review_id,
                    AdapterReview.department_id == current_operation.department_id,
                    AdapterReview.adapter_id == current.adapter_id,
                    AdapterReview.adapter_version == current.adapter_version,
                    AdapterReview.version == current.review_version,
                    AdapterReview.status == "approved",
                    AdapterReview.archived_at.is_(None),
                )
            )
            if outgoing_review is None:
                raise AdapterGovernanceWorkerError("review_authority_changed")
            outgoing_authority = _validate_approved_target(
                session,
                current_operation.department_id,
                current.adapter_id,
                current.adapter_version,
                outgoing_review.id,
                outgoing_review.version,
            )
            (
                outgoing_adapter,
                _outgoing_review,
                outgoing_run,
                _outgoing_suite,
                _outgoing_registry,
                _outgoing_dependency,
                _outgoing_evidence,
            ) = outgoing_authority
            outgoing_retention = _retention_for_outgoing(
                session,
                department_id=current_operation.department_id,
                adapter_id=outgoing_adapter.id,
                adapter_version=outgoing_adapter.version,
                review_id=outgoing_review.id,
                review_version=outgoing_review.version,
                evaluation_id=outgoing_run.id,
                evaluation_version=outgoing_run.version,
                suite_id=outgoing_run.suite_id,
                event_id=event.id,
            )
            event.rollback_retention_id = outgoing_retention.id
        if event_type == "rollback_adapter":
            target_retention = session.scalar(
                select(AdapterRollbackRetention)
                .where(AdapterRollbackRetention.id == current_operation.target_retention_id)
                .with_for_update()
            )
            if target_retention is not None:
                target_retention.status = "released"
                target_retention.release_reason = "reactivated"
                target_retention.release_event_id = event.id
                target_retention.released_at = now
                target_retention.version += 1
        if current is None:
            current = DepartmentAdapterDeployment(
                id=uuid4(),
                department_id=current_operation.department_id,
                target_kind=target_kind,
                adapter_id=adapter.id if adapter is not None else None,
                adapter_version=adapter.version if adapter is not None else None,
                review_id=review.id if review is not None else None,
                review_version=review.version if review is not None else None,
                evaluation_id=run.id if run is not None else None,
                evaluation_version=run.version if run is not None else None,
                suite_id=run.suite_id if run is not None else None,
                base_model_id=_BASE_MODEL_ID,
                base_model_revision=_BASE_MODEL_REVISION,
                deployment_version=after_version,
                version=1,
            )
            session.add(current)
        else:
            current.target_kind = target_kind
            current.adapter_id = adapter.id if adapter is not None else None
            current.adapter_version = adapter.version if adapter is not None else None
            current.review_id = review.id if review is not None else None
            current.review_version = review.version if review is not None else None
            current.evaluation_id = run.id if run is not None else None
            current.evaluation_version = run.version if run is not None else None
            current.base_model_id = _BASE_MODEL_ID
            current.base_model_revision = _BASE_MODEL_REVISION
            current.deployment_version = after_version
            current.version += 1
        current_operation.status = "succeeded"
        current_operation.error_code = None
        current_operation.worker_id = None
        current_operation.claim_token = None
        current_operation.lease_expires_at = None
        current_operation.finished_at = now
        current_operation.version += 1
        actor = session.get(UserIdentity, current_operation.requested_by_user_id)
        if actor is not None:
            scope = DepartmentRequestScope(DepartmentScope(current_operation.department_id))
            append_mutation_audit(
                session,
                actor=actor,
                actor_subject=actor.subject,
                request_scope=scope,
                action="adapter.deployment.success",
                resource_type="adapter_deployment_operation",
                resource_id=current_operation.id,
            )
        if retained_final is not None:
            retained_final.close()
        return True


def fail_owned_operation(
    factory: sessionmaker[Session], operation: AdapterDeploymentOperation, code: str
) -> None:
    safe = code if code in ADAPTER_GOVERNANCE_ERROR_CODES else "database_unavailable"
    with factory.begin() as session:
        row = session.scalar(
            select(AdapterDeploymentOperation)
            .where(
                AdapterDeploymentOperation.id == operation.id,
                AdapterDeploymentOperation.status == "running",
            )
            .with_for_update()
        )
        if row is None or not _claim_is_live(session, row):
            return
        now = session.scalar(select(func.clock_timestamp()))
        row.status = "cancelled" if safe == "cancelled" else "failed"
        row.error_code = safe
        row.worker_id = None
        row.claim_token = None
        row.lease_expires_at = None
        row.finished_at = now
        row.version += 1


def run_once(factory: sessionmaker[Session], *, data_dir: Path, worker_id: UUID) -> bool:
    operation = claim_next_operation(factory, worker_id=worker_id)
    if operation is None:
        return False
    try:
        finalize_owned_operation(factory, operation, data_dir=data_dir)
    except AdapterGovernanceWorkerError as error:
        fail_owned_operation(factory, operation, str(error))
    except ServiceError:
        # Service-layer messages are intentionally not persisted.  A final
        # authority failure is represented by the closed worker error enum.
        fail_owned_operation(factory, operation, "adapter_authority_changed")
    except (AdapterRegistryArtifactError, OSError, SQLAlchemyError):
        fail_owned_operation(factory, operation, "database_unavailable")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="DeptSLM adapter governance worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    worker_id = UUID(
        os.getenv("DEPTSLM_ADAPTER_GOVERNANCE_WORKER_ID", "00000000-0000-0000-0000-000000000014")
    )
    try:
        if args.once or not args.poll:
            run_once(factory, data_dir=settings.data_dir, worker_id=worker_id)
        else:
            while True:
                did_work = run_once(factory, data_dir=settings.data_dir, worker_id=worker_id)
                if not did_work:
                    time.sleep(POLL_SECONDS)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()


__all__ = [
    "AdapterGovernanceWorkerError",
    "claim_next_operation",
    "fail_owned_operation",
    "finalize_owned_operation",
    "main",
    "run_once",
    "verify_registry_final",
]

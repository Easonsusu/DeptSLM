"""Exact Phase 12.3 deployment authority for Phase 12.4 admission."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_REVISION
from app.adapter_governance_services import _validate_approved_target
from app.adapter_runtime_contract import RuntimeTarget
from app.models import DepartmentAdapterDeployment
from app.services import ServiceError


def load_runtime_target(
    session: Session, department_id: UUID, *, lock: bool = True
) -> RuntimeTarget:
    """Load one exact deployment pointer and all of its reviewed authority.

    The caller must hold the department authorization fence first.  No latest
    review/evaluation lookup is permitted here; adapter targets resolve only
    the IDs persisted in the deployment pointer.
    """

    query = select(DepartmentAdapterDeployment).where(
        DepartmentAdapterDeployment.department_id == department_id
    )
    if lock:
        query = query.with_for_update()
    deployment = session.scalar(query)
    if deployment is None:
        return RuntimeTarget(
            department_id=department_id,
            target_kind="base",
            deployment_id=None,
            deployment_version=0,
            deployment_row_version=None,
            base_model_id=BASE_MODEL_ID,
            base_model_revision=BASE_MODEL_REVISION,
        )
    if (
        deployment.base_model_id != BASE_MODEL_ID
        or deployment.base_model_revision != BASE_MODEL_REVISION
    ):
        raise ServiceError(503, "Grounded answer unavailable")
    if deployment.version <= 0 or deployment.deployment_version <= 0:
        raise ServiceError(503, "Grounded answer unavailable")
    if deployment.target_kind == "base":
        if any(
            value is not None
            for value in (
                deployment.adapter_id,
                deployment.adapter_version,
                deployment.review_id,
                deployment.review_version,
                deployment.evaluation_id,
                deployment.evaluation_version,
                deployment.suite_id,
            )
        ):
            raise ServiceError(503, "Grounded answer unavailable")
        return RuntimeTarget(
            department_id=department_id,
            target_kind="base",
            deployment_id=deployment.id,
            deployment_version=deployment.deployment_version,
            deployment_row_version=deployment.version,
            base_model_id=deployment.base_model_id,
            base_model_revision=deployment.base_model_revision,
        )
    if deployment.target_kind != "adapter":
        raise ServiceError(503, "Grounded answer unavailable")
    if None in (
        deployment.adapter_id,
        deployment.adapter_version,
        deployment.review_id,
        deployment.review_version,
    ):
        raise ServiceError(503, "Grounded answer unavailable")
    try:
        adapter, review, run, suite, registry, dependency, _evidence = _validate_approved_target(
            session,
            department_id,
            deployment.adapter_id,
            deployment.adapter_version,
            deployment.review_id,
            deployment.review_version,
        )
    except ServiceError:
        raise ServiceError(503, "Grounded answer unavailable") from None
    if (
        deployment.evaluation_id != run.id
        or deployment.evaluation_version != run.version
        or deployment.suite_id != suite.id
    ):
        raise ServiceError(503, "Grounded answer unavailable")
    return RuntimeTarget(
        department_id=department_id,
        target_kind="adapter",
        deployment_id=deployment.id,
        deployment_version=deployment.deployment_version,
        deployment_row_version=deployment.version,
        base_model_id=run.base_model_id,
        base_model_revision=run.base_model_revision,
        adapter_id=adapter.id,
        adapter_version=adapter.version,
        review_id=review.id,
        review_version=review.version,
        evaluation_id=run.id,
        evaluation_version=run.version,
        suite_id=suite.id,
        suite_version=suite.version,
        registry_attempt_id=registry.id,
        registry_attempt_version=registry.version,
        registry_publication_attempt_id=registry.publication_attempt_id,
        registry_attempt_number=registry.attempt_number,
        registry_execution_scope_id=registry.execution_scope_id,
        registry_manifest_sha256=adapter.registry_manifest_sha256,
        adapter_config_sha256=adapter.registry_adapter_config_sha256,
        adapter_config_byte_size=adapter.registry_adapter_config_byte_size,
        adapter_model_sha256=adapter.registry_adapter_model_sha256,
        adapter_model_byte_size=adapter.registry_adapter_model_byte_size,
        dependency_id=dependency.id,
        dependency_version=dependency.version,
    )

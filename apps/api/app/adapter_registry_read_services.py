"""Department-scoped, content-free adapter registry read boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.models import Adapter, AdapterImportSource, AdapterUpstreamDependency
from app.services import ServiceError, authorize_transaction

ADAPTER_READ_ROLES = frozenset(
    {
        DepartmentRole.SYSTEM_ADMIN,
        DepartmentRole.DEPARTMENT_ADMIN,
        DepartmentRole.INSTRUCTOR,
    }
)


class _AdapterMetadataIntegrityError(RuntimeError):
    """Raised when committed PostgreSQL authority cannot be associated safely."""


@dataclass(frozen=True, slots=True)
class AdapterLineageProjection:
    source_bundle_id: UUID
    training_job_id: UUID
    training_job_version: int
    training_job_profile_id: str
    dataset_build_id: UUID
    dataset_build_version: int
    base_model_id: str
    base_model_revision: str
    base_model_license: str
    llamafactory_version: str


@dataclass(frozen=True, slots=True)
class AdapterContractProjection:
    source_contract_version: str
    intake_contract_version: str
    adapter_config_contract_version: str
    adapter_tensor_contract_version: str
    adapter_artifact_contract_version: str
    registry_manifest_contract_version: str
    training_job_artifact_contract_version: str
    training_job_manifest_contract_version: str
    dataset_artifact_contract_version: str
    dataset_example_contract_version: str
    dataset_normalization_version: str
    dataset_split_version: str
    peft_version: str
    safetensors_format: str


@dataclass(frozen=True, slots=True)
class AdapterVerificationProjection:
    declared_external_training_association: bool
    verified_governance_lineage: bool
    verified_artifact_compatibility: bool
    training_provenance_verified: bool


@dataclass(frozen=True, slots=True)
class AdapterRetentionProjection:
    source_status: str
    source_consumed_at: datetime | None
    source_purged_at: datetime | None
    upstream_dependency_status: str
    upstream_dependency_created_at: datetime
    upstream_dependency_released_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdapterMetadataProjection:
    id: UUID
    department_id: UUID
    status: str
    error_code: str | None
    lineage: AdapterLineageProjection
    contracts: AdapterContractProjection
    verification: AdapterVerificationProjection
    retention: AdapterRetentionProjection
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    validated_at: datetime | None
    purged_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    def public_data(self) -> dict[str, object]:
        """Return the closed public allowlist without serializing ORM state."""

        return {
            "id": self.id,
            "department_id": self.department_id,
            "status": self.status,
            "error_code": self.error_code,
            "lineage": {
                "source_bundle_id": self.lineage.source_bundle_id,
                "training_job_id": self.lineage.training_job_id,
                "training_job_version": self.lineage.training_job_version,
                "training_job_profile_id": self.lineage.training_job_profile_id,
                "dataset_build_id": self.lineage.dataset_build_id,
                "dataset_build_version": self.lineage.dataset_build_version,
                "base_model_id": self.lineage.base_model_id,
                "base_model_revision": self.lineage.base_model_revision,
                "base_model_license": self.lineage.base_model_license,
                "llamafactory_version": self.lineage.llamafactory_version,
            },
            "contracts": {
                "source_contract_version": self.contracts.source_contract_version,
                "intake_contract_version": self.contracts.intake_contract_version,
                "adapter_config_contract_version": self.contracts.adapter_config_contract_version,
                "adapter_tensor_contract_version": self.contracts.adapter_tensor_contract_version,
                "adapter_artifact_contract_version": (
                    self.contracts.adapter_artifact_contract_version
                ),
                "registry_manifest_contract_version": (
                    self.contracts.registry_manifest_contract_version
                ),
                "training_job_artifact_contract_version": (
                    self.contracts.training_job_artifact_contract_version
                ),
                "training_job_manifest_contract_version": (
                    self.contracts.training_job_manifest_contract_version
                ),
                "dataset_artifact_contract_version": (
                    self.contracts.dataset_artifact_contract_version
                ),
                "dataset_example_contract_version": self.contracts.dataset_example_contract_version,
                "dataset_normalization_version": self.contracts.dataset_normalization_version,
                "dataset_split_version": self.contracts.dataset_split_version,
                "peft_version": self.contracts.peft_version,
                "safetensors_format": self.contracts.safetensors_format,
            },
            "verification": {
                "declared_external_training_association": (
                    self.verification.declared_external_training_association
                ),
                "verified_governance_lineage": self.verification.verified_governance_lineage,
                "verified_artifact_compatibility": (
                    self.verification.verified_artifact_compatibility
                ),
                "training_provenance_verified": self.verification.training_provenance_verified,
            },
            "retention": {
                "source_status": self.retention.source_status,
                "source_consumed_at": self.retention.source_consumed_at,
                "source_purged_at": self.retention.source_purged_at,
                "upstream_dependency_status": self.retention.upstream_dependency_status,
                "upstream_dependency_created_at": self.retention.upstream_dependency_created_at,
                "upstream_dependency_released_at": self.retention.upstream_dependency_released_at,
            },
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "validated_at": self.validated_at,
            "purged_at": self.purged_at,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _page(limit: object, offset: object) -> None:
    if (
        isinstance(limit, bool)
        or isinstance(offset, bool)
        or not isinstance(limit, int)
        or not isinstance(offset, int)
        or not 1 <= limit <= 100
        or offset < 0
    ):
        raise ServiceError(422, "Invalid pagination")


def _invalid_authority() -> _AdapterMetadataIntegrityError:
    return _AdapterMetadataIntegrityError("adapter metadata authority is inconsistent")


def _unique_rows(rows: list[object], *, identifier: str) -> dict[UUID, object]:
    result: dict[UUID, object] = {}
    for row in rows:
        row_id = getattr(row, identifier)
        if row_id in result:
            raise _invalid_authority()
        result[row_id] = row
    return result


def _validate_associations(
    adapter: Adapter,
    source: AdapterImportSource | None,
    dependency: AdapterUpstreamDependency | None,
) -> tuple[AdapterImportSource, AdapterUpstreamDependency]:
    if source is None or dependency is None:
        raise _invalid_authority()
    if (
        source.id != adapter.source_bundle_id
        or source.department_id != adapter.department_id
        or source.claimed_adapter_id != adapter.id
    ):
        raise _invalid_authority()
    if (
        dependency.adapter_id != adapter.id
        or dependency.department_id != adapter.department_id
        or dependency.training_job_id != adapter.training_job_id
        or dependency.dataset_build_id != adapter.dataset_build_id
    ):
        raise _invalid_authority()
    if dependency.status not in {"active", "released"}:
        raise _invalid_authority()
    if (dependency.status == "active") != (dependency.released_at is None):
        raise _invalid_authority()
    if adapter.status != "purged" and dependency.status != "active":
        raise _invalid_authority()
    return source, dependency


def _project(
    adapter: Adapter,
    source: AdapterImportSource | None,
    dependency: AdapterUpstreamDependency | None,
) -> AdapterMetadataProjection:
    source, dependency = _validate_associations(adapter, source, dependency)
    return AdapterMetadataProjection(
        id=adapter.id,
        department_id=adapter.department_id,
        status=adapter.status,
        error_code=adapter.error_code,
        lineage=AdapterLineageProjection(
            source_bundle_id=adapter.source_bundle_id,
            training_job_id=adapter.training_job_id,
            training_job_version=adapter.training_job_version,
            training_job_profile_id=adapter.training_job_profile_id,
            dataset_build_id=adapter.dataset_build_id,
            dataset_build_version=adapter.dataset_build_version,
            base_model_id=adapter.base_model_id,
            base_model_revision=adapter.base_model_revision,
            base_model_license=adapter.base_model_license,
            llamafactory_version=adapter.llamafactory_version,
        ),
        contracts=AdapterContractProjection(
            source_contract_version=adapter.source_contract_version,
            intake_contract_version=adapter.intake_contract_version,
            adapter_config_contract_version=adapter.config_contract_version,
            adapter_tensor_contract_version=adapter.tensor_contract_version,
            adapter_artifact_contract_version=adapter.artifact_contract_version,
            registry_manifest_contract_version=adapter.registry_manifest_contract_version,
            training_job_artifact_contract_version=adapter.training_job_artifact_contract_version,
            training_job_manifest_contract_version=adapter.training_job_manifest_contract_version,
            dataset_artifact_contract_version=adapter.dataset_artifact_contract_version,
            dataset_example_contract_version=adapter.dataset_example_contract_version,
            dataset_normalization_version=adapter.dataset_normalization_version,
            dataset_split_version=adapter.dataset_split_version,
            peft_version=adapter.peft_version,
            safetensors_format=adapter.safetensors_format,
        ),
        verification=AdapterVerificationProjection(
            declared_external_training_association=adapter.declared_external_training_association,
            verified_governance_lineage=adapter.verified_governance_lineage,
            verified_artifact_compatibility=adapter.verified_artifact_compatibility,
            training_provenance_verified=adapter.training_provenance_verified,
        ),
        retention=AdapterRetentionProjection(
            source_status=source.status,
            source_consumed_at=source.consumed_at,
            source_purged_at=source.purged_at,
            upstream_dependency_status=dependency.status,
            upstream_dependency_created_at=dependency.created_at,
            upstream_dependency_released_at=dependency.released_at,
        ),
        queued_at=adapter.queued_at,
        started_at=adapter.started_at,
        finished_at=adapter.finished_at,
        validated_at=adapter.validated_at,
        purged_at=adapter.purged_at,
        version=adapter.version,
        created_at=adapter.created_at,
        updated_at=adapter.updated_at,
    )


def _associated_rows(
    session: Session, adapters: tuple[Adapter, ...]
) -> tuple[dict[UUID, AdapterImportSource], dict[UUID, AdapterUpstreamDependency]]:
    if not adapters:
        return {}, {}
    department_id = adapters[0].department_id
    adapter_ids = tuple(adapter.id for adapter in adapters)
    source_ids = tuple(adapter.source_bundle_id for adapter in adapters)
    sources = list(
        session.scalars(
            select(AdapterImportSource).where(
                AdapterImportSource.department_id == department_id,
                AdapterImportSource.id.in_(source_ids),
            )
        )
    )
    dependencies = list(
        session.scalars(
            select(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.department_id == department_id,
                AdapterUpstreamDependency.adapter_id.in_(adapter_ids),
            )
        )
    )
    return (
        _unique_rows(sources, identifier="id"),
        _unique_rows(dependencies, identifier="adapter_id"),
    )


def _read_projection_page(
    session: Session, adapters: tuple[Adapter, ...]
) -> tuple[AdapterMetadataProjection, ...]:
    sources, dependencies = _associated_rows(session, adapters)
    return tuple(
        _project(adapter, sources.get(adapter.source_bundle_id), dependencies.get(adapter.id))
        for adapter in adapters
    )


def list_adapters(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    offset: int,
) -> tuple[AdapterMetadataProjection, ...]:
    _page(limit, offset)
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ADAPTER_READ_ROLES,
            lock=False,
            audit_action="adapter.registry.list.authorization",
        )
        adapters = tuple(
            session.scalars(
                select(Adapter)
                .where(Adapter.department_id == request_scope.department.value)
                .order_by(Adapter.created_at.desc(), Adapter.id)
                .offset(offset)
                .limit(limit)
            )
        )
        return _read_projection_page(session, adapters)
    except ServiceError:
        raise
    except _AdapterMetadataIntegrityError as error:
        raise ServiceError(503, "Adapter metadata unavailable") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_adapter(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    adapter_id: UUID,
) -> AdapterMetadataProjection:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ADAPTER_READ_ROLES,
            lock=False,
            audit_action="adapter.registry.read.authorization",
        )
        adapter = session.execute(
            select(Adapter).where(
                Adapter.id == adapter_id,
                Adapter.department_id == request_scope.department.value,
            )
        ).scalar_one_or_none()
        if adapter is None:
            raise ServiceError(404, "Adapter not found")
        return _read_projection_page(session, (adapter,))[0]
    except ServiceError:
        raise
    except _AdapterMetadataIntegrityError as error:
        raise ServiceError(503, "Adapter metadata unavailable") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


__all__ = [
    "ADAPTER_READ_ROLES",
    "AdapterContractProjection",
    "AdapterLineageProjection",
    "AdapterMetadataProjection",
    "AdapterRetentionProjection",
    "AdapterVerificationProjection",
    "list_adapters",
    "read_adapter",
]

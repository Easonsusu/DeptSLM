"""Pure contract tests for the Phase 12.1D adapter metadata read service."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

import app.adapter_registry_read_services as read_services
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.services import ServiceError

FORBIDDEN_KEYS = {
    "requested_by_user_id",
    "imported_by_user_id",
    "source_attempt_id",
    "registry_attempt_id",
    "publication_attempt_id",
    "execution_scope_id",
    "attempt_number",
    "source_hash",
    "source_sha256",
    "adapter_hash",
    "adapter_sha256",
    "byte_size",
    "sha256",
    "code_revision",
    "worker_id",
    "claim_token",
    "lease_expires_at",
    "ownership_manifest",
    "publication_manifest",
    "tensor_dtype",
    "tensor_count",
    "tensor_names",
    "tensor_shapes",
    "tensor_element_count",
    "tensor_payload_byte_size",
    "filename",
    "path",
    "storage_path",
    "approved",
    "deployable",
    "runtime_eligible",
    "safe",
    "quality_verified",
    "artifact_present",
}


def _authority_rows(*, adapter_status: str = "validated", dependency_status: str = "active"):
    now = datetime.now(UTC)
    department_id = uuid4()
    adapter_id = uuid4()
    source_id = uuid4()
    training_job_id = uuid4()
    dataset_build_id = uuid4()
    adapter = SimpleNamespace(
        id=adapter_id,
        department_id=department_id,
        source_bundle_id=source_id,
        training_job_id=training_job_id,
        training_job_version=2,
        training_job_profile_id="phase11-qwen3-0.6b-lora-v1",
        dataset_build_id=dataset_build_id,
        dataset_build_version=3,
        base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        base_model_license="Apache-2.0",
        llamafactory_version="0.9.5",
        source_contract_version="phase12-adapter-source-v1",
        intake_contract_version="phase12-adapter-intake-v1",
        config_contract_version="phase12-adapter-config-v1",
        tensor_contract_version="phase12-adapter-tensors-v1",
        artifact_contract_version="phase12-adapter-artifact-v1",
        registry_manifest_contract_version="phase12-adapter-manifest-v1",
        training_job_artifact_contract_version="phase11-training-job-v1",
        training_job_manifest_contract_version="phase11-training-job-manifest-v1",
        dataset_artifact_contract_version="phase10-sft-dataset-v1",
        dataset_example_contract_version="phase10-sft-example-v1",
        dataset_normalization_version="phase10-sft-normalization-v1",
        dataset_split_version="phase10-sft-group-split-v1",
        peft_version="0.18.1",
        safetensors_format="0.7.0",
        declared_external_training_association=True,
        verified_governance_lineage=True,
        verified_artifact_compatibility=True,
        training_provenance_verified=False,
        status=adapter_status,
        error_code=None,
        queued_at=now,
        started_at=now,
        finished_at=now,
        validated_at=now,
        purged_at=now if adapter_status == "purged" else None,
        version=4,
        created_at=now,
        updated_at=now,
    )
    source = SimpleNamespace(
        id=source_id,
        department_id=department_id,
        claimed_adapter_id=adapter_id,
        status="consumed",
        consumed_at=now,
        purged_at=None,
    )
    dependency = SimpleNamespace(
        adapter_id=adapter_id,
        department_id=department_id,
        training_job_id=training_job_id,
        dataset_build_id=dataset_build_id,
        status=dependency_status,
        created_at=now,
        released_at=now if dependency_status == "released" else None,
    )
    return adapter, source, dependency


def _nested_keys(value: object) -> set[str]:
    if not isinstance(value, dict):
        return set()
    result = set(value)
    for child in value.values():
        result.update(_nested_keys(child))
    return result


def test_exact_response_key_allowlist_and_nested_allowlists() -> None:
    adapter, source, dependency = _authority_rows()
    response = read_services._project(adapter, source, dependency).public_data()

    assert set(response) == {
        "id",
        "department_id",
        "status",
        "error_code",
        "lineage",
        "contracts",
        "verification",
        "retention",
        "queued_at",
        "started_at",
        "finished_at",
        "validated_at",
        "purged_at",
        "version",
        "created_at",
        "updated_at",
    }
    assert set(response["lineage"]) == {
        "source_bundle_id",
        "training_job_id",
        "training_job_version",
        "training_job_profile_id",
        "dataset_build_id",
        "dataset_build_version",
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "llamafactory_version",
    }
    assert set(response["contracts"]) == {
        "source_contract_version",
        "intake_contract_version",
        "adapter_config_contract_version",
        "adapter_tensor_contract_version",
        "adapter_artifact_contract_version",
        "registry_manifest_contract_version",
        "training_job_artifact_contract_version",
        "training_job_manifest_contract_version",
        "dataset_artifact_contract_version",
        "dataset_example_contract_version",
        "dataset_normalization_version",
        "dataset_split_version",
        "peft_version",
        "safetensors_format",
    }
    assert set(response["verification"]) == {
        "declared_external_training_association",
        "verified_governance_lineage",
        "verified_artifact_compatibility",
        "training_provenance_verified",
    }
    assert set(response["retention"]) == {
        "source_status",
        "source_consumed_at",
        "source_purged_at",
        "upstream_dependency_status",
        "upstream_dependency_created_at",
        "upstream_dependency_released_at",
    }


def test_forbidden_fields_never_serialize() -> None:
    adapter, source, dependency = _authority_rows()
    serialized_keys = _nested_keys(
        read_services._project(adapter, source, dependency).public_data()
    )
    assert serialized_keys.isdisjoint(FORBIDDEN_KEYS)
    assert serialized_keys.isdisjoint(
        {
            "filename",
            "file_name",
            "path",
            "storage_path",
            "source_path",
            "artifact_path",
        }
    )


@pytest.mark.parametrize(
    "field",
    [
        "department_id",
        "id",
        "claimed_adapter_id",
        "training_job_id",
        "dataset_build_id",
    ],
)
def test_substituted_association_fails_closed(field: str) -> None:
    adapter, source, dependency = _authority_rows()
    replacement = uuid4()
    if field in {"department_id", "id", "claimed_adapter_id"}:
        setattr(source, field, replacement)
    else:
        setattr(dependency, field, replacement)
    with pytest.raises(RuntimeError):
        read_services._project(adapter, source, dependency)


def test_purged_history_may_show_released_dependency() -> None:
    adapter, source, dependency = _authority_rows(
        adapter_status="purged", dependency_status="released"
    )
    projection = read_services._project(adapter, source, dependency)
    assert projection.status == "purged"
    assert projection.retention.upstream_dependency_status == "released"


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (-1, 0), (101, 0), (True, 0), (25, -1), (25, True), (25.0, 0)],
)
def test_pagination_rejects_invalid_exact_integer_bounds(limit: object, offset: object) -> None:
    with pytest.raises(ServiceError) as error:
        read_services.list_adapters(
            None,
            AuthenticatedPrincipal("subject", "issuer"),
            DepartmentRequestScope(DepartmentScope(uuid4())),
            limit=limit,
            offset=offset,
        )
    assert error.value.status_code == 422


def test_database_error_maps_to_safe_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_services, "authorize_transaction", lambda *args, **kwargs: None)

    class BrokenSession:
        def scalars(self, statement):
            del statement
            raise SQLAlchemyError("secret database details")

    with pytest.raises(ServiceError) as error:
        read_services.list_adapters(
            BrokenSession(),
            AuthenticatedPrincipal("subject", "issuer"),
            DepartmentRequestScope(DepartmentScope(uuid4())),
            limit=25,
            offset=0,
        )
    assert error.value.status_code == 503
    assert error.value.detail == "Database unavailable"
    assert "secret" not in error.value.detail


def test_read_service_has_no_storage_or_model_imports() -> None:
    source = open(read_services.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        module
        and (
            module.startswith(
                (
                    "adapter_registry_artifacts",
                    "adapter_source_artifacts",
                    "adapter_registry_child",
                    "adapter_source_child",
                    "torch",
                    "transformers",
                    "peft",
                    "safetensors",
                    "llamafactory",
                )
            )
            or module in {"fastapi.responses", "starlette.responses"}
        )
        for module in imported_modules
    )

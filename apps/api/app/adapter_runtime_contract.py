"""Closed, content-free Phase 12.4 adapter runtime routing contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_REVISION

ADAPTER_RUNTIME_CONTRACT_VERSION = "phase12-adapter-runtime-routing-v1"


@dataclass(frozen=True, slots=True)
class RuntimeTarget:
    """The complete immutable target captured for one RAG request."""

    department_id: UUID
    target_kind: str
    deployment_id: UUID | None
    deployment_version: int
    deployment_row_version: int | None
    base_model_id: str
    base_model_revision: str
    adapter_id: UUID | None = None
    adapter_version: int | None = None
    review_id: UUID | None = None
    review_version: int | None = None
    evaluation_id: UUID | None = None
    evaluation_version: int | None = None
    suite_id: UUID | None = None
    suite_version: int | None = None
    registry_attempt_id: UUID | None = None
    registry_attempt_version: int | None = None
    registry_publication_attempt_id: UUID | None = None
    registry_attempt_number: int | None = None
    registry_execution_scope_id: UUID | None = None
    registry_manifest_sha256: str | None = None
    adapter_config_sha256: str | None = None
    adapter_config_byte_size: int | None = None
    adapter_model_sha256: str | None = None
    adapter_model_byte_size: int | None = None
    dependency_id: UUID | None = None
    dependency_version: int | None = None

    def __post_init__(self) -> None:
        if self.target_kind not in {"base", "adapter"}:
            raise ValueError("invalid runtime target kind")
        if self.base_model_id != BASE_MODEL_ID or self.base_model_revision != BASE_MODEL_REVISION:
            raise ValueError("unsupported base model")
        if type(self.deployment_version) is not int or self.deployment_version < 0:
            raise ValueError("invalid deployment version")
        if self.deployment_row_version is not None and type(self.deployment_row_version) is not int:
            raise ValueError("invalid deployment row version")
        if self.target_kind == "base":
            if any(
                getattr(self, name) is not None
                for name in (
                    "adapter_id",
                    "adapter_version",
                    "review_id",
                    "review_version",
                    "evaluation_id",
                    "evaluation_version",
                    "suite_id",
                    "suite_version",
                    "registry_attempt_id",
                    "registry_attempt_version",
                    "registry_publication_attempt_id",
                    "registry_attempt_number",
                    "registry_execution_scope_id",
                    "registry_manifest_sha256",
                    "adapter_config_sha256",
                    "adapter_config_byte_size",
                    "adapter_model_sha256",
                    "adapter_model_byte_size",
                    "dependency_id",
                    "dependency_version",
                )
            ):
                raise ValueError("base target contains adapter authority")
            if self.deployment_id is None and (
                self.deployment_version != 0 or self.deployment_row_version is not None
            ):
                raise ValueError("invalid implicit base target")
            if self.deployment_id is not None and (
                self.deployment_version <= 0
                or self.deployment_row_version is None
                or self.deployment_row_version <= 0
            ):
                raise ValueError("invalid explicit base target")
        else:
            required = (
                self.deployment_id,
                self.adapter_id,
                self.adapter_version,
                self.review_id,
                self.review_version,
                self.evaluation_id,
                self.evaluation_version,
                self.suite_id,
                self.suite_version,
                self.registry_attempt_id,
                self.registry_attempt_version,
                self.registry_publication_attempt_id,
                self.registry_attempt_number,
                self.registry_execution_scope_id,
                self.registry_manifest_sha256,
                self.adapter_config_sha256,
                self.adapter_config_byte_size,
                self.adapter_model_sha256,
                self.adapter_model_byte_size,
                self.dependency_id,
                self.dependency_version,
            )
            if any(value is None for value in required) or self.deployment_version <= 0:
                raise ValueError("incomplete adapter target")
            if any(
                type(getattr(self, name)) is not int or getattr(self, name) <= 0
                for name in (
                    "deployment_row_version",
                    "adapter_version",
                    "review_version",
                    "evaluation_version",
                    "suite_version",
                    "registry_attempt_version",
                    "registry_attempt_number",
                    "adapter_config_byte_size",
                    "adapter_model_byte_size",
                    "dependency_version",
                )
            ):
                raise ValueError("invalid adapter target versions")

    def canonical_fields(self) -> dict[str, object]:
        """Return only the reviewed server-owned fingerprint fields."""

        values: dict[str, object] = {}
        for name in _TARGET_FIELDS:
            value = getattr(self, name)
            values[name] = str(value) if isinstance(value, UUID) else value
        return values

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.canonical_fields(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def adapter_request_fields(self) -> dict[str, object]:
        """Return the exact authority fields accepted by adapter-runtime."""

        if self.target_kind != "adapter":
            raise ValueError("base target has no adapter request")
        fields = self.canonical_fields()
        fields["runtime_contract_version"] = ADAPTER_RUNTIME_CONTRACT_VERSION
        fields["target_fingerprint"] = self.fingerprint
        return fields


_TARGET_FIELDS = (
    "department_id",
    "target_kind",
    "deployment_id",
    "deployment_version",
    "deployment_row_version",
    "base_model_id",
    "base_model_revision",
    "adapter_id",
    "adapter_version",
    "review_id",
    "review_version",
    "evaluation_id",
    "evaluation_version",
    "suite_id",
    "suite_version",
    "registry_attempt_id",
    "registry_attempt_version",
    "registry_publication_attempt_id",
    "registry_attempt_number",
    "registry_execution_scope_id",
    "registry_manifest_sha256",
    "adapter_config_sha256",
    "adapter_config_byte_size",
    "adapter_model_sha256",
    "adapter_model_byte_size",
    "dependency_id",
    "dependency_version",
)

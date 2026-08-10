"""Historical immutability checks for the Phase 12.1B migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest


def test_phase12_migration_is_self_contained() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0010_phase12_adapter_sources.py"
    source = path.read_text(encoding="utf-8")
    assert "app.adapter_contract" not in source
    spec = importlib.util.spec_from_file_location("phase12_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0010_phase12_adapter_sources"
    assert module.down_revision == "0009_phase11_training_jobs"


def test_phase12_1c_migration_is_self_contained_and_frozen() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0011_phase12_adapter_registry.py"
    source = path.read_text(encoding="utf-8")
    assert "app.models" not in source
    assert "app.adapter_registry" not in source
    spec = importlib.util.spec_from_file_location("phase12_1c_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0011_phase12_adapter_registry"
    assert module.down_revision == "0010_phase12_adapter_sources"
    assert "adapter_registry_attempts" in source
    assert "adapter_upstream_dependencies" in source
    assert "fk_adapter_import_source_claimed_adapter_scope" in source


def test_phase12_1e_a_migration_is_self_contained_and_has_exact_surfaces() -> None:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0012_phase12_adapter_reconciliation.py"
    )
    source = path.read_text(encoding="utf-8")
    assert "app.models" not in source
    assert "app.adapter_maintenance_artifacts" not in source
    spec = importlib.util.spec_from_file_location("phase12_1e_a_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0012_phase12_adapter_reconciliation"
    assert module.down_revision == "0011_phase12_adapter_registry"
    for surface in ("source_stage", "source_final", "registry_stage", "registry_final"):
        assert surface in source
    assert "adapter_artifact_operations" in source
    assert "adapter_artifact_operation_items" in source
    assert "fk_adapter_artifact_operation_requester_scope" in source
    assert "fk_adapter_artifact_operation_requester_identity" in source
    assert "fk_adapter_artifact_item_source_scope" in source
    assert "fk_adapter_artifact_item_adapter_scope" in source
    assert "uq_adapter_registry_attempt_exact" in source
    assert "move_authorized_at" in source
    assert "expected_tombstone_namespace" in source
    assert "ck_adapter_artifact_item_move_namespace_json" in source


def test_phase12_1e_b_migration_is_self_contained_and_has_purge_authority() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0014_phase12_adapter_purge.py"
    source = path.read_text(encoding="utf-8")
    assert "app.models" not in source
    assert "app.adapter_maintenance_artifacts" not in source
    spec = importlib.util.spec_from_file_location("phase12_1e_b_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0014_phase12_adapter_purge"
    assert module.down_revision == "0013_phase12_adapter_reconciliation_cursor"
    for table in (
        "adapter_purge_operations",
        "adapter_purge_reservations",
        "adapter_purge_items",
    ):
        assert table in source
    for field in (
        "authority_snapshot",
        "expected_tombstone_namespace",
        "in_flight_entry",
        "directory_unlink_started_at",
    ):
        assert field in source
    assert "fk_adapter_purge_operation_source_attempt_exact" in source
    assert "fk_adapter_purge_operation_registry_attempt_exact" in source
    assert "ck_adapter_purge_item_reason" in source


def test_phase12_1c_backfill_uses_canonical_manifest_authority() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0011_phase12_adapter_registry.py"
    spec = importlib.util.spec_from_file_location("phase12_1c_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = {
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "department_id": "11111111-1111-4111-8111-111111111111",
        "source_bundle_id": "22222222-2222-4222-8222-222222222222",
        "import_attempt_id": "33333333-3333-4333-8333-333333333333",
        "publication_attempt_id": "44444444-4444-4444-8444-444444444444",
        "attempt_number": 1,
        "imported_by_user_id": "55555555-5555-4555-8555-555555555555",
        "code_revision": "a" * 40,
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": "F16",
        "tensor_count": 392,
        "tensor_element_count": 10092544,
        "tensor_payload_byte_size": 20185088,
        "files": {
            "adapter_config.json": {"sha256": "a" * 64, "byte_size": 1},
            "adapter_model.safetensors": {"sha256": "b" * 64, "byte_size": 2},
        },
    }
    encoded = module._canonical_manifest_bytes(manifest)
    assert encoded is not None and encoded.endswith(b"\n")
    assert module.hashlib.sha256(encoded).hexdigest()
    malformed = dict(manifest)
    malformed["files"] = {"unknown": {"sha256": "a" * 64, "byte_size": 1}}
    assert module._canonical_manifest_bytes(malformed) is None


def _phase12_1c_migration_module():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0011_phase12_adapter_registry.py"
    spec = importlib.util.spec_from_file_location("phase12_1c_backfill_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backfill_row(module) -> dict[str, object]:
    department_id = uuid4()
    source_id = uuid4()
    attempt_id = uuid4()
    publication_attempt_id = uuid4()
    imported_by_user_id = uuid4()
    code_revision = "a" * 40
    manifest = {
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "department_id": str(department_id),
        "source_bundle_id": str(source_id),
        "import_attempt_id": str(attempt_id),
        "publication_attempt_id": str(publication_attempt_id),
        "attempt_number": 1,
        "imported_by_user_id": str(imported_by_user_id),
        "code_revision": code_revision,
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": "F16",
        "tensor_count": 392,
        "tensor_element_count": 10092544,
        "tensor_payload_byte_size": 20185088,
        "files": {
            "adapter_config.json": {"sha256": "a" * 64, "byte_size": 1},
            "adapter_model.safetensors": {"sha256": "b" * 64, "byte_size": 2},
        },
    }
    encoded = module._canonical_manifest_bytes(manifest)
    assert encoded is not None
    return {
        "source_id": source_id,
        "department_id": department_id,
        "authoritative_attempt_id": attempt_id,
        "code_revision": code_revision,
        "imported_by_user_id": imported_by_user_id,
        "intake_manifest_sha256": module.hashlib.sha256(encoded).hexdigest(),
        "attempt_id": attempt_id,
        "attempt_department_id": department_id,
        "attempt_source_bundle_id": source_id,
        "attempt_publication_attempt_id": publication_attempt_id,
        "attempt_number_value": 1,
        "attempt_code_revision": code_revision,
        "attempt_status": "committed",
        "ownership_manifest": manifest,
    }


class _BackfillResult:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def mappings(self):
        return [self.row]


class _BackfillBind:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.updated: dict[str, object] | None = None

    def execute(self, _statement, parameters=None):
        if parameters is None:
            return _BackfillResult(self.row)
        self.updated = parameters
        return None


def test_phase12_1c_backfill_accepts_one_exact_closed_authority() -> None:
    module = _phase12_1c_migration_module()
    row = _backfill_row(module)
    bind = _BackfillBind(row)
    module._backfill_intake_manifest_sizes(bind)
    assert bind.updated is not None
    assert bind.updated["byte_size"] > 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update({"authoritative_attempt_id": None}),
        lambda row: row.update({"attempt_department_id": uuid4()}),
        lambda row: row.update({"attempt_status": "registered"}),
        lambda row: row["ownership_manifest"].pop("files"),
        lambda row: row.update({"intake_manifest_sha256": "0" * 64}),
    ],
)
def test_phase12_1c_backfill_rejects_incomplete_or_changed_authority(mutation) -> None:
    module = _phase12_1c_migration_module()
    row = _backfill_row(module)
    mutation(row)
    bind = _BackfillBind(row)
    with pytest.raises(RuntimeError):
        module._backfill_intake_manifest_sizes(bind)
    assert bind.updated is None

"""Prepare and verify one synthetic Phase 12.3 container promotion.

This helper is invoked only by CI.  It creates a tiny metadata authority and a
sparse synthetic safetensors file under the runner temporary directory; it
never downloads or persists a real adapter or model.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from test_phase12_1c_child_integration import _model
from test_phase12_1e_a_postgres import _storage
from test_phase12_2_postgres import _evaluation_artifacts, _make_authority, _principal, _scope

from alembic import command
from app.adapter_contract import (
    EXPECTED_TENSOR_NAMES,
    EXPECTED_TENSOR_SHAPES,
    canonical_adapter_config_bytes,
)
from app.adapter_evaluation_queue import claim_next, finalize_success
from app.adapter_evaluation_services import enqueue_adapter_evaluation
from app.adapter_governance_services import enqueue_promotion, start_review, transition_review
from app.adapter_registry_domain import build_registry_manifest
from app.database import create_database_engine
from app.models import (
    Adapter,
    AdapterDeploymentEvent,
    AdapterDeploymentOperation,
    AdapterEvaluationRun,
    AdapterRegistryAttempt,
    DepartmentAdapterDeployment,
    PersistentAuditEvent,
)


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_TEST_URL is required")
    return value


def _lineage() -> tuple[dict[str, object], dict[str, object]]:
    source = {
        "source_bundle_id": str(uuid4()),
        "authoritative_import_attempt_id": str(uuid4()),
        "import_publication_attempt_id": str(uuid4()),
        "import_attempt_number": 1,
        "source_code_revision": "a" * 40,
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "intake_manifest_sha256": "a" * 64,
        "external_adapter_config_sha256": "a" * 64,
        "external_adapter_config_byte_size": 1,
        "external_adapter_model_sha256": "b" * 64,
        "external_adapter_model_byte_size": 1,
    }
    governance = {
        "training_job_id": str(uuid4()),
        "training_job_version": 1,
        "training_job_publication_attempt_id": str(uuid4()),
        "training_job_attempt_number": 1,
        "training_job_code_revision": "b" * 40,
        "training_job_manifest_sha256": "b" * 64,
        "profile_id": "phase11-qwen3-0.6b-lora-v1",
        "training_job_artifact_contract_version": "phase11-training-job-v1",
        "training_job_manifest_contract_version": "phase11-training-job-manifest-v1",
        "training_configuration_contract_version": "phase11-training-config-v1",
        "training_dataset_info_contract_version": "phase11-dataset-info-v1",
        "training_execution_profile_contract_version": "phase11-execution-profile-v1",
        "llamafactory_version": "0.9.5",
        "dataset_build_id": str(uuid4()),
        "dataset_build_version": 1,
        "dataset_publication_attempt_id": str(uuid4()),
        "dataset_publication_attempt_number": 1,
        "dataset_code_revision": "c" * 40,
        "dataset_manifest_sha256": "c" * 64,
        "dataset_source_bundle_id": str(uuid4()),
        "dataset_artifact_contract_version": "phase10-sft-dataset-v1",
        "dataset_example_contract_version": "phase10-sft-example-v1",
        "dataset_normalization_version": "phase10-sft-normalization-v1",
        "dataset_split_version": "phase10-sft-group-split-v1",
        "dataset_train_sha256": "d" * 64,
        "dataset_train_byte_size": 1,
        "dataset_validation_sha256": "e" * 64,
        "dataset_validation_byte_size": 1,
        "dataset_provenance_sha256": "f" * 64,
        "dataset_provenance_byte_size": 1,
        "dataset_train_example_count": 1,
        "dataset_validation_example_count": 1,
        "dataset_source_example_count": 2,
        "dataset_source_group_count": 2,
        "dataset_source_reference_count": 2,
        "dataset_rights_attested": True,
        "evaluation_contamination_reviewed": True,
    }
    return source, governance


def _prepare_registry_final(
    root: Path, factory: sessionmaker[Session], authority
) -> tuple[UUID, int]:
    with factory() as session:
        adapter = session.scalar(
            select(Adapter).where(Adapter.department_id == authority.department_id)
        )
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id
            )
        )
        if adapter is None or attempt is None:
            raise RuntimeError("synthetic registry authority is incomplete")
        adapter_id = adapter.id
        attempt_id = attempt.id
        publication_attempt_id = attempt.publication_attempt_id
        attempt_number = attempt.attempt_number

    final = root / "adapters" / "registry" / str(authority.department_id) / str(adapter_id)
    final.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(final.parent, 0o700)
    os.chmod(final, 0o700)
    config = final / "adapter_config.json"
    config_raw = canonical_adapter_config_bytes()
    config.write_bytes(config_raw)
    os.chmod(config, 0o600)
    model = final / "adapter_model.safetensors"
    model_size, model_sha, payload_size = _model(model, "F16")
    source, governance = _lineage()
    compatibility = {
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": "F16",
        "tensor_count": len(EXPECTED_TENSOR_NAMES),
        "tensor_element_count": sum(
            shape[0] * shape[1] for shape in EXPECTED_TENSOR_SHAPES.values()
        ),
        "tensor_payload_byte_size": payload_size,
        "adapter_config_contract_version": "phase12-adapter-config-v1",
        "adapter_tensor_contract_version": "phase12-adapter-tensors-v1",
    }
    manifest_raw = build_registry_manifest(
        department_id=authority.department_id,
        adapter_id=adapter_id,
        publication_attempt_id=publication_attempt_id,
        attempt_number=attempt_number,
        code_revision=authority.code_revision,
        source=source,
        governance_lineage=governance,
        files={
            "adapter_config.json": {
                "sha256": hashlib.sha256(config_raw).hexdigest(),
                "byte_size": len(config_raw),
            },
            "adapter_model.safetensors": {"sha256": model_sha, "byte_size": model_size},
        },
        compatibility=compatibility,
    )
    (final / "manifest.json").write_bytes(manifest_raw)
    os.chmod(final / "manifest.json", 0o600)
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    with factory.begin() as session:
        adapter = session.get(Adapter, adapter_id)
        attempt = session.get(AdapterRegistryAttempt, attempt_id)
        if adapter is None or attempt is None:
            raise RuntimeError("synthetic registry authority disappeared")
        config_sha = hashlib.sha256(config_raw).hexdigest()
        adapter.source_adapter_config_sha256 = config_sha
        adapter.source_adapter_config_byte_size = len(config_raw)
        adapter.source_adapter_model_sha256 = model_sha
        adapter.source_adapter_model_byte_size = model_size
        adapter.registry_manifest_sha256 = manifest_sha
        adapter.registry_adapter_config_sha256 = config_sha
        adapter.registry_adapter_config_byte_size = len(config_raw)
        adapter.registry_adapter_model_sha256 = model_sha
        adapter.registry_adapter_model_byte_size = model_size
        attempt.ownership_manifest = json.loads(manifest_raw[:-1].decode("utf-8"))
        return adapter.id, adapter.version


def prepare() -> None:
    root = Path(os.environ["DEPTSLM_DATA_DIR"]).resolve()
    _storage(root)
    engine = create_database_engine(_database_url())
    factory = sessionmaker(engine)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        authority, _adapter, suite = _make_authority(factory)
        adapter_id, adapter_version = _prepare_registry_final(root, factory, authority)
        with factory.begin() as session:
            enqueue_adapter_evaluation(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=adapter_id,
                suite_id=suite.id,
                expected_adapter_version=adapter_version,
                code_revision=authority.code_revision,
            )
        claim = claim_next(factory, uuid4(), 120, authority.code_revision)
        if claim is None:
            raise RuntimeError("synthetic evaluation was not claimed")
        store, manifest, rows, files = _evaluation_artifacts(factory, claim, root)
        from app.evaluation_domain import AggregateMetrics, GateEvaluation
        from app.evaluation_suites import GroundTruthAuthoritySnapshot

        metrics = AggregateMetrics(*(Decimal("0.5") for _ in AggregateMetrics.__dataclass_fields__))
        finalize_success(
            factory,
            claim,
            baseline_metrics=metrics,
            candidate_metrics=metrics,
            baseline_gate=GateEvaluation(True, 0, {}),
            candidate_gate=GateEvaluation(True, 0, {}),
            result_manifest_sha256=files["manifest.json"].sha256,
            result_summary_sha256=files["summary.json"].sha256,
            case_results_sha256=files["case_results.jsonl"].sha256,
            case_results_byte_size=files["case_results.jsonl"].byte_size,
            case_rows=tuple(rows),
            data_dir=root,
            suite_cases=(),
            suite_authority=GroundTruthAuthoritySnapshot({}, ()),
            result_store=store,
            result_manifest=manifest,
            result_files=files,
        )
        with factory.begin() as session:
            fresh = session.get(Adapter, adapter_id)
            run = session.scalar(
                select(AdapterEvaluationRun).where(AdapterEvaluationRun.adapter_id == adapter_id)
            )
            if fresh is None or run is None:
                raise RuntimeError("synthetic evaluation authority is incomplete")
            review = start_review(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=fresh.id,
                evaluation_id=run.id,
                expected_adapter_version=fresh.version,
                expected_evaluation_version=run.version,
            )
            approved = transition_review(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=fresh.id,
                review_id=review["id"],
                action="approve",
                expected_adapter_version=fresh.version,
                expected_review_version=review["version"],
            )
            operation = enqueue_promotion(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=fresh.id,
                review_id=approved["id"],
                expected_adapter_version=fresh.version,
                expected_review_version=approved["version"],
                expected_deployment_version=0,
            )
            print(operation["id"], flush=True)
    finally:
        engine.dispose()


def verify() -> None:
    engine = create_database_engine(_database_url())
    factory = sessionmaker(engine)
    try:
        with factory() as session:
            operation = session.scalar(
                select(AdapterDeploymentOperation)
                .where(AdapterDeploymentOperation.operation_type == "promote")
                .order_by(AdapterDeploymentOperation.created_at.desc())
            )
            if operation is None or operation.status != "succeeded":
                raise RuntimeError("governance container did not succeed")
            pointer = session.scalar(
                select(DepartmentAdapterDeployment).where(
                    DepartmentAdapterDeployment.department_id == operation.department_id
                )
            )
            events = session.scalar(
                select(func.count(AdapterDeploymentEvent.id)).where(
                    AdapterDeploymentEvent.operation_id == operation.id,
                    AdapterDeploymentEvent.event_type == "promote",
                )
            )
            audits = session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.action == "adapter.deployment.success",
                    PersistentAuditEvent.resource_id == str(operation.id),
                )
            )
            if (
                pointer is None
                or pointer.target_kind != "adapter"
                or pointer.adapter_id != operation.target_adapter_id
                or events != 1
                or audits != 1
            ):
                raise RuntimeError("governance promotion authority did not finalize exactly once")
    finally:
        engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "verify"}:
        raise SystemExit("usage: phase12_3_container_smoke.py prepare|verify")
    (prepare if sys.argv[1] == "prepare" else verify)()

"""PostgreSQL 16 integration coverage for Phase 12.1E-A reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_phase12_1c_integration import Authority, _enqueue, _seed_authority
from test_phase12_1d_postgres import _cleanup as cleanup_seed

import app.adapter_artifact_maintenance as maintenance
from alembic import command
from app.adapter_artifact_maintenance import (
    RECONCILIATION_SCAN_MULTIPLIER,
    _manifest_authority,
    reconcile_adapter_artifacts,
)
from app.adapter_maintenance_artifacts import AdapterMaintenanceArtifactError
from app.adapter_registry_domain import (
    build_registry_manifest,
    canonical_json_bytes,
    parse_registry_manifest,
)
from app.adapter_registry_queue import claim_next_adapter, terminal_failure
from app.adapter_source_artifacts import canonical_manifest_bytes, parse_source_manifest
from app.database import create_database_engine
from app.models import (
    Adapter,
    AdapterArtifactOperation,
    AdapterArtifactOperationItem,
    AdapterArtifactReconciliationCursor,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    PersistentAuditEvent,
)

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped")
    pytest.skip("PostgreSQL integration database is unavailable")


@pytest.fixture(scope="module")
def engine():
    value = create_database_engine(_database_url())
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(engine)


@pytest.fixture
def authority(factory):
    with factory() as session:
        value = _seed_authority(session)
    yield value
    _cleanup_test_rows(factory, value)
    cleanup_seed(factory, value)


def _storage(root: Path) -> Path:
    for relative in (
        "adapters",
        "adapters/.staging",
        "adapters/.deleting",
        "adapters/imports",
        "adapters/registry",
        "adapters/.staging/imports",
        "adapters/.staging/registry",
        "adapters/.deleting/source_stage",
        "adapters/.deleting/source_final",
        "adapters/.deleting/registry_stage",
        "adapters/.deleting/registry_final",
    ):
        path = root / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    return root


def _file(path: Path, value: bytes = b"partial") -> None:
    path.write_bytes(value)
    path.chmod(0o600)


def _cleanup_test_rows(factory: sessionmaker[Session], authority: Authority) -> None:
    """Remove reconciliation rows and restore the seed's closed source line."""

    with factory.begin() as session:
        now = datetime.now(UTC)
        source_attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert source_attempt is not None and isinstance(source_attempt.ownership_manifest, dict)
        manifest_bytes = (
            json.dumps(
                source_attempt.ownership_manifest,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        # Break the source-to-adapter claim before deleting the adapter row.
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == authority.department_id,
                AdapterImportSource.id == authority.source_id,
            )
            .values(
                status="committed",
                authoritative_attempt_id=authority.source_attempt_id,
                error_code=None,
                abandoned_at=None,
                committed_at=now,
                intake_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                intake_manifest_byte_size=len(manifest_bytes),
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
            )
        )
        session.execute(
            delete(AdapterArtifactReconciliationCursor).where(
                AdapterArtifactReconciliationCursor.department_id == authority.department_id
            )
        )
        session.execute(
            delete(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id
            )
        )
        session.execute(
            delete(AdapterArtifactOperation).where(
                AdapterArtifactOperation.department_id == authority.department_id
            )
        )
        session.execute(
            delete(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.department_id == authority.department_id
            )
        )
        session.execute(
            delete(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id
            )
        )
        session.execute(delete(Adapter).where(Adapter.department_id == authority.department_id))
        session.execute(
            update(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.department_id == authority.department_id,
                AdapterImportAttempt.id == authority.source_attempt_id,
            )
            .values(
                status="committed",
                error_code=None,
                committed_at=now,
                finished_at=now,
                cleanup_confirmed_at=None,
            )
        )
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == authority.department_id,
                AdapterImportSource.id == authority.source_id,
            )
            .values(
                status="committed",
                authoritative_attempt_id=authority.source_attempt_id,
                error_code=None,
                abandoned_at=None,
                committed_at=now,
                intake_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                intake_manifest_byte_size=len(manifest_bytes),
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
            )
        )
        session.execute(
            delete(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == authority.department_id,
                PersistentAuditEvent.action == "adapter.artifact.reconcile",
            )
        )


def _abandon_source(factory: sessionmaker[Session], authority: Authority) -> None:
    now = datetime.now(UTC)
    with factory.begin() as session:
        session.execute(
            update(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.department_id == authority.department_id,
                AdapterImportAttempt.id == authority.source_attempt_id,
            )
            .values(
                status="failed",
                error_code="adapter_source_publication_failed",
                committed_at=None,
                finished_at=now,
                cleanup_confirmed_at=None,
            )
        )
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == authority.department_id,
                AdapterImportSource.id == authority.source_id,
            )
            .values(
                status="abandoned",
                authoritative_attempt_id=None,
                error_code="adapter_source_publication_failed",
                abandoned_at=now,
                committed_at=None,
                intake_manifest_sha256=None,
                intake_manifest_byte_size=None,
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
            )
        )


def _source_stage(root: Path, authority: Authority) -> Path:
    stage = (
        root
        / "adapters"
        / ".staging"
        / "imports"
        / str(authority.department_id)
        / str(authority.source_id)
        / str(authority.source_attempt_id)
    )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    return stage


def _source_stage_for_attempt(root: Path, authority: Authority, attempt_id: UUID) -> Path:
    stage = (
        root
        / "adapters"
        / ".staging"
        / "imports"
        / str(authority.department_id)
        / str(authority.source_id)
        / str(attempt_id)
    )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    return stage


def _source_final(root: Path, authority: Authority, manifest: dict[str, object]) -> Path:
    final = root / "adapters" / "imports" / str(authority.department_id) / str(authority.source_id)
    final.mkdir(mode=0o700, parents=True)
    for path in (final.parent, final):
        path.chmod(0o700)
    config = b"{}"
    model = b"model"
    files = {
        "adapter_config.json": {
            "sha256": hashlib.sha256(config).hexdigest(),
            "byte_size": len(config),
        },
        "adapter_model.safetensors": {
            "sha256": hashlib.sha256(model).hexdigest(),
            "byte_size": len(model),
        },
    }
    manifest["files"] = files
    raw = canonical_manifest_bytes(manifest)
    parse_source_manifest(raw)
    _file(final / "adapter_config.json", config)
    _file(final / "adapter_model.safetensors", model)
    _file(final / "intake_manifest.json", raw)
    return final


def _prepare_source_crash(
    factory: sessionmaker[Session], authority: Authority, root: Path, state: str
) -> Path:
    now = datetime.now(UTC)
    with factory.begin() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert attempt is not None and source is not None
        manifest = dict(attempt.ownership_manifest or {})
        attempt.status = state
        attempt.validated_at = now
        attempt.staged_at = now
        attempt.published_at = now if state == "published" else None
        attempt.committed_at = None
        attempt.finished_at = None
        attempt.cleanup_confirmed_at = None
        attempt.error_code = None
        attempt.ownership_manifest = manifest
        attempt.version += 1
        source.status = "staging"
        source.authoritative_attempt_id = None
        source.committed_at = None
        source.abandoned_at = None
        source.error_code = None
        source.intake_manifest_sha256 = None
        source.intake_manifest_byte_size = None
        source.version += 1
    final = _source_final(root, authority, manifest)
    with factory.begin() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert attempt is not None and source is not None
        attempt.status = "failed"
        attempt.error_code = "adapter_source_publication_failed"
        attempt.finished_at = now
        attempt.committed_at = None
        attempt.published_at = attempt.published_at or now
        attempt.ownership_manifest = manifest
        attempt.version += 1
        source.status = "abandoned"
        source.abandoned_at = now
        source.error_code = "adapter_source_publication_failed"
        source.authoritative_attempt_id = None
        source.committed_at = None
        source.intake_manifest_sha256 = None
        source.intake_manifest_byte_size = None
        source.version += 1
    return final


def _registry_manifest_for_claim(
    claim, config: bytes, model: bytes
) -> tuple[dict[str, object], bytes]:
    source_snapshot = claim.source
    source = {
        "source_bundle_id": source_snapshot["source_bundle_id"],
        "authoritative_import_attempt_id": source_snapshot["authoritative_attempt_id"],
        "import_publication_attempt_id": source_snapshot["publication_attempt_id"],
        "import_attempt_number": source_snapshot["attempt_number"],
        "source_code_revision": source_snapshot["code_revision"],
        "source_contract_version": source_snapshot["source_contract_version"],
        "intake_contract_version": source_snapshot["intake_contract_version"],
        "intake_manifest_sha256": source_snapshot["intake_manifest_sha256"],
        "external_adapter_config_sha256": source_snapshot["adapter_config_sha256"],
        "external_adapter_config_byte_size": source_snapshot["adapter_config_byte_size"],
        "external_adapter_model_sha256": source_snapshot["adapter_model_sha256"],
        "external_adapter_model_byte_size": source_snapshot["adapter_model_byte_size"],
    }
    governance_snapshot = claim.governance_lineage
    governance = {
        "training_job_id": governance_snapshot["training_job_id"],
        "training_job_version": governance_snapshot["training_job_version"],
        "training_job_publication_attempt_id": governance_snapshot[
            "training_job_publication_attempt_id"
        ],
        "training_job_attempt_number": governance_snapshot["training_job_attempt_number"],
        "training_job_code_revision": governance_snapshot["training_job_code_revision"],
        "training_job_manifest_sha256": governance_snapshot["training_job_manifest_sha256"],
        "profile_id": governance_snapshot["training_job_profile_id"],
        "training_job_artifact_contract_version": governance_snapshot[
            "training_job_artifact_contract_version"
        ],
        "training_job_manifest_contract_version": governance_snapshot[
            "training_job_manifest_contract_version"
        ],
        "training_configuration_contract_version": governance_snapshot[
            "training_configuration_contract_version"
        ],
        "training_dataset_info_contract_version": governance_snapshot[
            "training_dataset_info_contract_version"
        ],
        "training_execution_profile_contract_version": governance_snapshot[
            "training_execution_profile_contract_version"
        ],
        "llamafactory_version": governance_snapshot["llamafactory_version"],
        "dataset_build_id": governance_snapshot["dataset_build_id"],
        "dataset_build_version": governance_snapshot["dataset_build_version"],
        "dataset_publication_attempt_id": governance_snapshot["dataset_publication_attempt_id"],
        "dataset_publication_attempt_number": governance_snapshot[
            "dataset_publication_attempt_number"
        ],
        "dataset_code_revision": governance_snapshot["dataset_code_revision"],
        "dataset_manifest_sha256": governance_snapshot["dataset_manifest_sha256"],
        "dataset_source_bundle_id": governance_snapshot["dataset_source_bundle_id"],
        "dataset_artifact_contract_version": governance_snapshot[
            "dataset_artifact_contract_version"
        ],
        "dataset_example_contract_version": governance_snapshot["dataset_example_contract_version"],
        "dataset_normalization_version": governance_snapshot["dataset_normalization_version"],
        "dataset_split_version": governance_snapshot["dataset_split_version"],
        "dataset_train_sha256": governance_snapshot["dataset_train_sha256"],
        "dataset_train_byte_size": governance_snapshot["dataset_train_byte_size"],
        "dataset_validation_sha256": governance_snapshot["dataset_validation_sha256"],
        "dataset_validation_byte_size": governance_snapshot["dataset_validation_byte_size"],
        "dataset_provenance_sha256": governance_snapshot["dataset_provenance_sha256"],
        "dataset_provenance_byte_size": governance_snapshot["dataset_provenance_byte_size"],
        "dataset_train_example_count": governance_snapshot["dataset_train_example_count"],
        "dataset_validation_example_count": governance_snapshot["dataset_validation_example_count"],
        "dataset_source_example_count": governance_snapshot["dataset_source_example_count"],
        "dataset_source_group_count": governance_snapshot["dataset_source_group_count"],
        "dataset_source_reference_count": governance_snapshot["dataset_source_reference_count"],
        "dataset_rights_attested": governance_snapshot["dataset_rights_attested"],
        "evaluation_contamination_reviewed": governance_snapshot[
            "evaluation_contamination_reviewed"
        ],
    }
    compatibility = {
        "base_model_id": source_snapshot["base_model_id"],
        "base_model_revision": source_snapshot["base_model_revision"],
        "base_model_license": source_snapshot["base_model_license"],
        "peft_version": source_snapshot["peft_version"],
        "safetensors_format": source_snapshot["safetensors_format"],
        "tensor_dtype": source_snapshot["tensor_dtype"],
        "tensor_count": source_snapshot["tensor_count"],
        "tensor_element_count": source_snapshot["tensor_element_count"],
        "tensor_payload_byte_size": source_snapshot["tensor_payload_byte_size"],
        "adapter_config_contract_version": source_snapshot["config_contract_version"],
        "adapter_tensor_contract_version": source_snapshot["tensor_contract_version"],
    }
    files = {
        "adapter_config.json": {
            "sha256": hashlib.sha256(config).hexdigest(),
            "byte_size": len(config),
        },
        "adapter_model.safetensors": {
            "sha256": hashlib.sha256(model).hexdigest(),
            "byte_size": len(model),
        },
    }
    raw = build_registry_manifest(
        department_id=claim.department_id,
        adapter_id=claim.id,
        publication_attempt_id=claim.publication_attempt_id,
        attempt_number=claim.attempt_number,
        code_revision=claim.code_revision,
        source=source,
        governance_lineage=governance,
        files=files,
        compatibility=compatibility,
    )
    return parse_registry_manifest(raw), raw


def _registry_final(root: Path, authority: Authority, claim, manifest_raw: bytes) -> Path:
    final = root / "adapters" / "registry" / str(authority.department_id) / str(claim.id)
    final.mkdir(mode=0o700, parents=True)
    for path in (final.parent, final):
        path.chmod(0o700)
    config = b"{}"
    model = b"model"
    _file(final / "adapter_config.json", config)
    _file(final / "adapter_model.safetensors", model)
    _file(final / "manifest.json", manifest_raw)
    return final


def _prepare_registry_crash(
    factory: sessionmaker[Session], authority: Authority, root: Path, state: str
) -> tuple[Path, UUID]:
    enqueue = _enqueue(factory, authority, apply=True)
    worker_id = uuid4()
    claim = claim_next_adapter(factory, worker_id, 30, authority.code_revision)
    assert claim is not None and enqueue.adapter_id == claim.id
    config = b"{}"
    model = b"model"
    manifest, manifest_raw = _registry_manifest_for_claim(claim, config, model)
    now = datetime.now(UTC)
    with factory.begin() as session:
        attempt = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        adapter = session.get(Adapter, claim.id)
        assert attempt is not None and adapter is not None
        attempt.status = state
        attempt.ownership_manifest = manifest
        attempt.staged_at = now
        attempt.published_at = now if state == "published" else None
        attempt.finished_at = None
        attempt.error_code = None
        attempt.cleanup_confirmed_at = None
        attempt.version += 1
        adapter.status = "running"
        adapter.finished_at = None
        adapter.registry_manifest_sha256 = None
        adapter.registry_adapter_config_sha256 = None
        adapter.registry_adapter_config_byte_size = None
        adapter.registry_adapter_model_sha256 = None
        adapter.registry_adapter_model_byte_size = None
        adapter.version += 1
    final = _registry_final(root, authority, claim, manifest_raw)
    with factory.begin() as session:
        attempt = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        adapter = session.get(Adapter, claim.id)
        assert attempt is not None and adapter is not None
        attempt.status = "failed"
        attempt.error_code = "adapter_registry_publication_failed"
        attempt.finished_at = now
        attempt.worker_id = None
        attempt.claimed_at = None
        attempt.version += 1
        adapter.status = "failed"
        adapter.error_code = "adapter_registry_publication_failed"
        adapter.worker_id = None
        adapter.claim_token = None
        adapter.lease_expires_at = None
        adapter.finished_at = now
        adapter.version += 1
    return final, claim.id


def _add_failed_source_attempt(
    factory: sessionmaker[Session],
    authority: Authority,
    *,
    attempt_number: int,
    manifest=None,
    publication_attempt_id: UUID | None = None,
    attempt_id: UUID | None = None,
    source_id: UUID | None = None,
    created_at: datetime | None = None,
) -> UUID:
    attempt_id = attempt_id or uuid4()
    publication_attempt_id = publication_attempt_id or uuid4()
    now = created_at or datetime.now(UTC)
    with factory.begin() as session:
        values = {
            "id": attempt_id,
            "department_id": authority.department_id,
            "source_bundle_id": source_id or authority.source_id,
            "attempt_number": attempt_number,
            "publication_attempt_id": publication_attempt_id,
            "status": "failed",
            "code_revision": authority.code_revision,
            "error_code": "adapter_source_publication_failed",
            "created_at": now,
            "finished_at": now,
            "version": 1,
        }
        if manifest is not None:
            values["ownership_manifest"] = manifest
        session.add(AdapterImportAttempt(**values))
    return attempt_id


def _add_staging_source(factory: sessionmaker[Session], authority: Authority) -> UUID:
    """Add a metadata-only source whose terminal attempt can be selected."""

    source_id = uuid4()
    with factory() as session:
        original = session.get(AdapterImportSource, authority.source_id)
        assert original is not None
        source = AdapterImportSource(
            id=source_id,
            department_id=authority.department_id,
            imported_by_user_id=original.imported_by_user_id,
            status="staging",
            source_contract_version=original.source_contract_version,
            intake_contract_version=original.intake_contract_version,
            config_contract_version=original.config_contract_version,
            tensor_contract_version=original.tensor_contract_version,
            base_model_id=original.base_model_id,
            base_model_revision=original.base_model_revision,
            base_model_license=original.base_model_license,
            peft_version=original.peft_version,
            safetensors_format=original.safetensors_format,
            code_revision=original.code_revision,
        )
        session.add(source)
        session.commit()
    return source_id


def _add_failed_registry_attempt(
    factory: sessionmaker[Session],
    authority: Authority,
    adapter_id: UUID,
    *,
    attempt_number: int,
    manifest=None,
    publication_attempt_id: UUID | None = None,
    attempt_id: UUID | None = None,
    created_at: datetime | None = None,
) -> UUID:
    attempt_id = attempt_id or uuid4()
    publication_attempt_id = publication_attempt_id or uuid4()
    now = created_at or datetime.now(UTC)
    with factory.begin() as session:
        values = {
            "id": attempt_id,
            "department_id": authority.department_id,
            "adapter_id": adapter_id,
            "attempt_number": attempt_number,
            "publication_attempt_id": publication_attempt_id,
            "execution_scope_id": uuid4(),
            "status": "failed",
            "code_revision": authority.code_revision,
            "error_code": "adapter_registry_publication_failed",
            "created_at": now,
            "finished_at": now,
            "version": 1,
        }
        if manifest is not None:
            values["ownership_manifest"] = manifest
        session.add(AdapterRegistryAttempt(**values))
    return attempt_id


def _reconcile(factory, authority: Authority, root: Path, *, apply: bool, limit: int = 1):
    return reconcile_adapter_artifacts(
        factory,
        data_dir=root,
        department_id=authority.department_id,
        actor_issuer=authority.issuer,
        actor_subject=authority.subject,
        limit=limit,
        minimum_age_seconds=300,
        apply=apply,
    )


def _seed_completed_stage_item(
    factory: sessionmaker[Session],
    authority: Authority,
    attempt_id: UUID,
    *,
    family: str,
    adapter_id: UUID | None = None,
) -> UUID:
    """Install only completed stage history for bounded-selection regressions."""

    now = datetime.now(UTC)
    with factory.begin() as session:
        if family == "source":
            attempt = session.get(AdapterImportAttempt, attempt_id)
            source = session.get(AdapterImportSource, authority.source_id)
            assert attempt is not None and source is not None
            resource_id = authority.source_id
            operation = AdapterArtifactOperation(
                id=uuid4(),
                department_id=authority.department_id,
                requested_by_user_id=authority.admin_id,
                operation_type="reconcile",
                status="completed",
                limit_value=1,
                minimum_age_seconds=300,
                eligible_count=1,
                completed_count=1,
                completed_at=now,
                version=1,
            )
            item = AdapterArtifactOperationItem(
                id=uuid4(),
                operation_id=operation.id,
                department_id=authority.department_id,
                surface_type="source_stage",
                source_bundle_id=resource_id,
                import_attempt_id=attempt.id,
                publication_attempt_id=attempt.publication_attempt_id,
                attempt_number=attempt.attempt_number,
                expected_resource_version=source.version,
                expected_attempt_version=attempt.version,
                status="completed",
                completed_at=now,
                version=1,
            )
        else:
            assert adapter_id is not None
            attempt = session.get(AdapterRegistryAttempt, attempt_id)
            adapter = session.get(Adapter, adapter_id)
            assert attempt is not None and adapter is not None
            resource_id = adapter.id
            operation = AdapterArtifactOperation(
                id=uuid4(),
                department_id=authority.department_id,
                requested_by_user_id=authority.admin_id,
                operation_type="reconcile",
                status="completed",
                limit_value=1,
                minimum_age_seconds=300,
                eligible_count=1,
                completed_count=1,
                completed_at=now,
                version=1,
            )
            item = AdapterArtifactOperationItem(
                id=uuid4(),
                operation_id=operation.id,
                department_id=authority.department_id,
                surface_type="registry_stage",
                adapter_id=resource_id,
                registry_attempt_id=attempt.id,
                publication_attempt_id=attempt.publication_attempt_id,
                attempt_number=attempt.attempt_number,
                expected_resource_version=adapter.version,
                expected_attempt_version=attempt.version,
                status="completed",
                completed_at=now,
                version=1,
            )
        session.add(operation)
        session.flush()
        session.add(item)
        item_id = item.id
    return item_id


def _seed_completed_final_item(
    factory: sessionmaker[Session],
    authority: Authority,
    attempt_id: UUID,
    *,
    family: str,
    manifest: dict[str, object],
    adapter_id: UUID | None = None,
) -> UUID:
    """Install completed final history for confirmation-priority regressions."""

    now = datetime.now(UTC)
    with factory.begin() as session:
        if family == "source":
            attempt = session.get(AdapterImportAttempt, attempt_id)
            source = session.get(AdapterImportSource, authority.source_id)
            assert attempt is not None and source is not None
            item = AdapterArtifactOperationItem(
                id=uuid4(),
                operation_id=uuid4(),
                department_id=authority.department_id,
                surface_type="source_final",
                source_bundle_id=authority.source_id,
                import_attempt_id=attempt.id,
                publication_attempt_id=attempt.publication_attempt_id,
                attempt_number=attempt.attempt_number,
                expected_resource_version=source.version,
                expected_attempt_version=attempt.version,
                ownership_manifest=manifest,
                status="completed",
                completed_at=now,
                version=1,
            )
        else:
            assert adapter_id is not None
            attempt = session.get(AdapterRegistryAttempt, attempt_id)
            adapter = session.get(Adapter, adapter_id)
            assert attempt is not None and adapter is not None
            item = AdapterArtifactOperationItem(
                id=uuid4(),
                operation_id=uuid4(),
                department_id=authority.department_id,
                surface_type="registry_final",
                adapter_id=adapter.id,
                registry_attempt_id=attempt.id,
                publication_attempt_id=attempt.publication_attempt_id,
                attempt_number=attempt.attempt_number,
                expected_resource_version=adapter.version,
                expected_attempt_version=attempt.version,
                ownership_manifest=manifest,
                status="completed",
                completed_at=now,
                version=1,
            )
        operation = AdapterArtifactOperation(
            id=item.operation_id,
            department_id=authority.department_id,
            requested_by_user_id=authority.admin_id,
            operation_type="reconcile",
            status="completed",
            limit_value=1,
            minimum_age_seconds=300,
            eligible_count=1,
            completed_count=1,
            completed_at=now,
            version=1,
        )
        session.add(operation)
        session.add(item)
        item_id = item.id
    return item_id


def _source_retry_manifest(
    factory: sessionmaker[Session],
    authority: Authority,
    attempt_id: UUID,
    attempt_number: int,
    publication_attempt_id: UUID,
) -> dict[str, object]:
    with factory() as session:
        original = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert original is not None and isinstance(original.ownership_manifest, dict)
        manifest = dict(original.ownership_manifest)
    manifest.update(
        {
            "import_attempt_id": str(attempt_id),
            "publication_attempt_id": str(publication_attempt_id),
            "attempt_number": attempt_number,
        }
    )
    parse_source_manifest(canonical_manifest_bytes(manifest))
    return manifest


def _unknown_tombstone(root: Path, surface_type: str, department_id: UUID) -> Path:
    item = (
        root
        / "adapters"
        / ".deleting"
        / surface_type
        / str(department_id)
        / str(uuid4())
        / str(uuid4())
    )
    item.mkdir(mode=0o700, parents=True)
    for path in (item.parent.parent, item.parent, item):
        path.chmod(0o700)
    return item


def test_real_dry_run_is_read_only(factory, authority: Authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    result = _reconcile(factory, authority, root, apply=False)
    assert result.eligible_count == 2
    assert result.completed_count == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperation.id)).where(
                    AdapterArtifactOperation.department_id == authority.department_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            )
            == 0
        )


def test_source_apply_commits_move_and_cleanup_once(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    stage = _source_stage(root, authority)
    result = _reconcile(factory, authority, root, apply=True)
    assert result.eligible_count == 2
    assert result.completed_count == 2
    assert not stage.exists()
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert attempt.status == "failed"
        operation = session.scalar(
            select(AdapterArtifactOperation).where(
                AdapterArtifactOperation.department_id == authority.department_id
            )
        )
        assert operation is not None and operation.status == "completed"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )
    second = _reconcile(factory, authority, root, apply=True)
    assert second.eligible_count == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_missing_after_verified_is_blocked_without_tombstone(
    factory, authority: Authority, tmp_path: Path, monkeypatch
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    from app import adapter_maintenance_artifacts as storage

    monkeypatch.setattr(
        storage.AdapterMaintenanceArtifactStore,
        "move_verified_surface_to_tombstone",
        lambda *_args, **_kwargs: None,
    )
    result = _reconcile(factory, authority, root, apply=True)
    assert result.blocked_count >= 1
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id
            )
        ).all()
        assert any(row.status == "blocked" for row in rows)
        assert any(row.blocked_reason_code == "artifact_authority_changed" for row in rows)


def test_unknown_resource_tombstone_blocks_cleanup_confirmation(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    unknown = (
        root
        / "adapters"
        / ".deleting"
        / "source_stage"
        / str(authority.department_id)
        / str(uuid4())
        / str(uuid4())
    )
    unknown.mkdir(mode=0o700, parents=True)
    for path in (unknown.parent.parent, unknown.parent, unknown):
        path.chmod(0o700)
    result = _reconcile(factory, authority, root, apply=True)
    assert result.completed_count == 2
    assert result.blocked_count == 0
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is None


def test_registry_stage_uses_publication_attempt_path(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    enqueue = _enqueue(factory, authority, apply=True)
    claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None and enqueue.adapter_id is not None
    terminal_failure(factory, claim, "adapter_registry_publication_failed")
    stage = (
        root
        / "adapters"
        / ".staging"
        / "registry"
        / str(authority.department_id)
        / str(enqueue.adapter_id)
        / str(claim.publication_attempt_id)
    )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    result = _reconcile(factory, authority, root, apply=True)
    assert result.completed_count == 1
    assert not stage.exists()
    with factory() as session:
        attempt = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        adapter = session.get(Adapter, enqueue.adapter_id)
        assert adapter is not None and adapter.status == "failed"


def _assert_source_final_crash_is_reconciled(
    factory, authority: Authority, tmp_path: Path, state: str
) -> None:
    root = _storage(tmp_path)
    final = _prepare_source_crash(factory, authority, root, state)
    assert final.exists()
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert attempt is not None and source is not None
        assert attempt.status == "failed"
        assert isinstance(attempt.ownership_manifest, dict)
        assert source.intake_manifest_sha256 is None
        assert source.intake_manifest_byte_size is None
    result = _reconcile(factory, authority, root, apply=True)
    assert result.eligible_count == 2
    assert result.completed_count == 2
    assert result.blocked_count == 0
    assert not final.exists()
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )
    second = _reconcile(factory, authority, root, apply=True)
    assert second.eligible_count == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_source_final_published_before_mark_published_is_reconciled(
    factory, authority: Authority, tmp_path: Path
) -> None:
    _assert_source_final_crash_is_reconciled(factory, authority, tmp_path, "staged")


def test_source_final_published_before_commit_is_reconciled(
    factory, authority: Authority, tmp_path: Path
) -> None:
    _assert_source_final_crash_is_reconciled(factory, authority, tmp_path, "published")


def _assert_registry_final_crash_is_reconciled(
    factory, authority: Authority, tmp_path: Path, state: str
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, state)
    assert final.exists()
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        adapter = session.get(Adapter, adapter_id)
        assert attempt is not None and adapter is not None
        assert attempt.status == "failed"
        assert isinstance(attempt.ownership_manifest, dict)
        assert adapter.registry_manifest_sha256 is None
    result = _reconcile(factory, authority, root, apply=True)
    assert result.eligible_count == 2
    assert result.completed_count == 2
    assert result.blocked_count == 0
    assert not final.exists()
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )
    second = _reconcile(factory, authority, root, apply=True)
    assert second.eligible_count == 0


def test_registry_final_published_before_mark_published_is_reconciled(
    factory, authority: Authority, tmp_path: Path
) -> None:
    _assert_registry_final_crash_is_reconciled(factory, authority, tmp_path, "staged")


def test_registry_final_published_before_finish_success_is_reconciled(
    factory, authority: Authority, tmp_path: Path
) -> None:
    _assert_registry_final_crash_is_reconciled(factory, authority, tmp_path, "published")


def test_non_null_registry_digest_mismatch_blocks_and_preserves_final(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory.begin() as session:
        adapter = session.get(Adapter, adapter_id)
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert adapter is not None and attempt is not None
        adapter.status = "validated"
        adapter.error_code = None
        adapter.worker_id = None
        adapter.claim_token = None
        adapter.lease_expires_at = None
        adapter.finished_at = datetime.now(UTC)
        adapter.validated_at = adapter.finished_at
        adapter.registry_manifest_sha256 = "0" * 64
        adapter.registry_adapter_config_sha256 = adapter.source_adapter_config_sha256
        adapter.registry_adapter_config_byte_size = adapter.source_adapter_config_byte_size
        adapter.registry_adapter_model_sha256 = adapter.source_adapter_model_sha256
        adapter.registry_adapter_model_byte_size = adapter.source_adapter_model_byte_size
        adapter.verified_governance_lineage = True
        adapter.verified_artifact_compatibility = True
        adapter.training_provenance_verified = False
        adapter.version += 1
        item = SimpleNamespace(
            surface_type="registry_final",
            adapter_id=adapter_id,
            department_id=authority.department_id,
            ownership_manifest=attempt.ownership_manifest,
        )
        with pytest.raises(AdapterMaintenanceArtifactError, match="artifact_authority_changed"):
            _manifest_authority(session, item)
    assert final.exists()


def test_blocked_source_stage_gets_a_fresh_retry_generation(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    stage = _source_stage(root, authority)
    stage.chmod(0o755)

    first = _reconcile(factory, authority, root, apply=True)
    assert first.blocked_count == 1
    assert first.completed_count == 1
    with factory() as session:
        blocked = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
            )
        ).all()
        assert len(blocked) == 1 and blocked[0].status == "blocked"
        blocked_id = blocked[0].id

    stage.chmod(0o700)
    second = _reconcile(factory, authority, root, apply=True)
    assert second.blocked_count == 0
    assert second.completed_count == 1
    assert not stage.exists()
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
            )
            .order_by(AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id)
        ).all()
        assert [row.status for row in rows] == ["blocked", "completed"]
        assert rows[0].id == blocked_id
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_blocked_source_final_gets_a_fresh_retry_generation(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final = _prepare_source_crash(factory, authority, root, "published")
    final.chmod(0o755)

    first = _reconcile(factory, authority, root, apply=True)
    assert first.blocked_count == 1
    final.chmod(0o700)
    second = _reconcile(factory, authority, root, apply=True)
    assert second.blocked_count == 0
    assert second.completed_count == 1
    assert not final.exists()
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_final",
            )
            .order_by(AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id)
        ).all()
        assert [row.status for row in rows] == ["blocked", "completed"]
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None


def test_blocked_registry_stage_gets_a_fresh_retry_generation(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None
        stage = (
            root
            / "adapters"
            / ".staging"
            / "registry"
            / str(authority.department_id)
            / str(adapter_id)
            / str(attempt.publication_attempt_id)
        )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    stage.chmod(0o755)

    first = _reconcile(factory, authority, root, apply=True)
    assert first.blocked_count == 1
    stage.chmod(0o700)
    second = _reconcile(factory, authority, root, apply=True)
    assert second.blocked_count == 0
    assert second.completed_count == 1
    assert not stage.exists()
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_stage",
            )
            .order_by(AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id)
        ).all()
        assert [row.status for row in rows] == ["blocked", "completed"]


def test_blocked_registry_final_gets_a_fresh_retry_generation(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    final.chmod(0o755)

    first = _reconcile(factory, authority, root, apply=True)
    assert first.blocked_count == 1
    final.chmod(0o700)
    second = _reconcile(factory, authority, root, apply=True)
    assert second.blocked_count == 0
    assert second.completed_count == 1
    assert not final.exists()
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_final",
            )
            .order_by(AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id)
        ).all()
        assert [row.status for row in rows] == ["blocked", "completed"]
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is not None


def test_blocked_source_final_sibling_rotates_to_repaired_generation(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final = _prepare_source_crash(factory, authority, root, "published")
    with factory() as session:
        first = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert first is not None and isinstance(first.ownership_manifest, dict)
        second_manifest = dict(first.ownership_manifest)
    second_publication = uuid4()
    second_manifest["publication_attempt_id"] = str(second_publication)
    second_manifest["attempt_number"] = 2
    parse_source_manifest(canonical_manifest_bytes(second_manifest))
    second_id = _add_failed_source_attempt(
        factory,
        authority,
        attempt_number=2,
        manifest=second_manifest,
        publication_attempt_id=second_publication,
    )
    final.chmod(0o755)
    first_result = _reconcile(factory, authority, root, apply=True)
    assert first_result.blocked_count == 1
    final.chmod(0o700)
    repaired = canonical_manifest_bytes(second_manifest)
    _file(final / "intake_manifest.json", repaired)
    second_result = _reconcile(factory, authority, root, apply=True)
    assert second_result.completed_count >= 1
    assert second_result.blocked_count == 0
    assert not final.exists()
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_final",
            )
            .order_by(AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id)
        ).all()
        assert rows[-1].import_attempt_id == second_id
        assert rows[-1].status == "completed"


def test_blocked_source_stage_does_not_starve_new_untried_stage(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    old_stage = _source_stage(root, authority)
    old_stage.chmod(0o755)
    first = _reconcile(factory, authority, root, apply=True, limit=1)
    assert first.blocked_count == 1
    second_id = _add_failed_source_attempt(factory, authority, attempt_number=2)
    with factory() as session:
        second = session.get(AdapterImportAttempt, second_id)
        assert second is not None
        second_publication = second.publication_attempt_id
    new_stage = (
        root
        / "adapters"
        / ".staging"
        / "imports"
        / str(authority.department_id)
        / str(authority.source_id)
        / str(second_id)
    )
    new_stage.mkdir(mode=0o700, parents=True)
    _file(new_stage / "adapter_config.json")
    second_result = _reconcile(factory, authority, root, apply=True, limit=1)
    assert second_result.blocked_count == 0
    with factory() as session:
        row = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
                AdapterArtifactOperationItem.publication_attempt_id == second_publication,
            )
        )
        assert row is not None and row.status == "completed"


def test_untried_source_stage_progresses_past_bounded_blocked_window(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    old_stages = [_source_stage(root, authority)]
    old_stages[0].chmod(0o755)
    old_attempt_ids: list[UUID] = []
    for attempt_number in range(2, 2 + RECONCILIATION_SCAN_MULTIPLIER):
        attempt_id = _add_failed_source_attempt(factory, authority, attempt_number=attempt_number)
        old_attempt_ids.append(attempt_id)
        stage = _source_stage_for_attempt(root, authority, attempt_id)
        stage.chmod(0o755)
    for _ in range(1 + RECONCILIATION_SCAN_MULTIPLIER):
        result = _reconcile(factory, authority, root, apply=True, limit=1)
        assert result.blocked_count == 1

    fresh_id = _add_failed_source_attempt(
        factory, authority, attempt_number=2 + RECONCILIATION_SCAN_MULTIPLIER
    )
    fresh_stage = _source_stage_for_attempt(root, authority, fresh_id)
    result = _reconcile(factory, authority, root, apply=True, limit=1)
    assert result.blocked_count == 0
    assert result.completed_count == 1
    with factory() as session:
        item = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
                AdapterArtifactOperationItem.import_attempt_id == fresh_id,
            )
        )
        assert item is not None and item.status == "completed"
        old_items = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
                AdapterArtifactOperationItem.import_attempt_id.in_(old_attempt_ids),
            )
        ).all()
        assert old_items and all(item.status == "blocked" for item in old_items)
    assert not fresh_stage.exists()


def test_keyset_window_bounds_history_evaluation_before_fairness(
    factory, authority: Authority, tmp_path: Path, monkeypatch
) -> None:
    """History probes receive only one fixed keyset window, never the backlog."""

    _storage(tmp_path)
    _abandon_source(factory, authority)
    total_attempts = RECONCILIATION_SCAN_MULTIPLIER * 3
    attempt_ids = [authority.source_attempt_id]
    for attempt_number in range(2, total_attempts + 1):
        attempt_ids.append(
            _add_failed_source_attempt(factory, authority, attempt_number=attempt_number)
        )
    observed: list[tuple[tuple[UUID, UUID], ...]] = []
    original = maintenance._surface_item_history_for_rows
    windows: list[tuple[UUID, ...]] = []
    original_window = maintenance._bounded_keyset_attempt_window

    def capture_window(*args, **kwargs):
        rows = tuple(original_window(*args, **kwargs))
        windows.append(tuple(attempt.id for attempt in rows))
        assert len(rows) <= maintenance._bounded_scan_limit(1)
        return rows

    def capture(*args, **kwargs):
        rows = tuple(kwargs["rows"])
        observed.append(rows)
        assert len(rows) <= maintenance._bounded_scan_limit(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(maintenance, "_bounded_keyset_attempt_window", capture_window)
    monkeypatch.setattr(maintenance, "_surface_item_history_for_rows", capture)
    dry_run = _reconcile(factory, authority, tmp_path, apply=False, limit=1)
    assert dry_run.eligible_count <= 2
    assert windows
    assert len(windows[0]) <= maintenance._bounded_scan_limit(1)
    nonempty = [row for row in observed if row]
    assert nonempty
    seen_attempts = {attempt_id for rows in nonempty for _resource, attempt_id in rows}
    assert len(seen_attempts) <= maintenance._bounded_scan_limit(1)
    assert seen_attempts < set(attempt_ids)


def test_apply_source_cursor_advances_through_structural_noop_window(
    factory, authority: Authority, tmp_path: Path
) -> None:
    """A protected source prefix advances without creating physical items."""

    _storage(tmp_path)
    base = datetime.now(UTC) - timedelta(days=1)
    first_id = _add_failed_source_attempt(factory, authority, attempt_number=2, created_at=base)
    second_id = _add_failed_source_attempt(
        factory, authority, attempt_number=3, created_at=base + timedelta(seconds=1)
    )
    valid_source_id = _add_staging_source(factory, authority)
    valid_id = _add_failed_source_attempt(
        factory,
        authority,
        attempt_number=1,
        source_id=valid_source_id,
        created_at=base + timedelta(seconds=2),
    )

    with factory.begin() as session:
        assert not maintenance._select_candidates(
            session, authority.department_id, 300, 1, apply=True
        )
    with factory() as session:
        cursor = session.get(
            AdapterArtifactReconciliationCursor,
            (authority.department_id, "source"),
        )
        assert cursor is not None
        assert cursor.cursor_attempt_id == second_id
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            )
            == 0
        )

    with factory.begin() as session:
        candidates = maintenance._select_candidates(
            session, authority.department_id, 300, 1, apply=True
        )
        assert [candidate.import_attempt_id for candidate in candidates] == [valid_id]
    with factory() as session:
        cursor = session.get(
            AdapterArtifactReconciliationCursor,
            (authority.department_id, "source"),
        )
        assert cursor is not None and cursor.cursor_attempt_id == valid_id
        snapshot = (cursor.cursor_created_at, cursor.cursor_attempt_id, cursor.version)

    with factory.begin() as session:
        maintenance._select_candidates(session, authority.department_id, 300, 1, apply=False)
    with factory() as session:
        cursor = session.get(
            AdapterArtifactReconciliationCursor,
            (authority.department_id, "source"),
        )
        assert cursor is not None
        assert (cursor.cursor_created_at, cursor.cursor_attempt_id, cursor.version) == snapshot
    assert first_id != second_id


def test_apply_registry_cursor_advances_through_structural_noop_window(
    factory, authority: Authority, tmp_path: Path
) -> None:
    """A nonterminal sibling blocks a bounded registry window without items."""

    root = _storage(tmp_path)
    _final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    base = datetime.now(UTC) - timedelta(days=1)
    with factory.begin() as session:
        first_attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
                AdapterRegistryAttempt.attempt_number == 1,
            )
        )
        assert first_attempt is not None
        first_attempt.created_at = base - timedelta(seconds=1)
    _add_failed_registry_attempt(factory, authority, adapter_id, attempt_number=2, created_at=base)
    _add_failed_registry_attempt(
        factory,
        authority,
        adapter_id,
        attempt_number=3,
        created_at=base + timedelta(seconds=1),
    )
    with factory.begin() as session:
        session.add(
            AdapterRegistryAttempt(
                id=uuid4(),
                department_id=authority.department_id,
                adapter_id=adapter_id,
                attempt_number=4,
                publication_attempt_id=uuid4(),
                execution_scope_id=uuid4(),
                code_revision=authority.code_revision,
                status="registered",
                created_at=base + timedelta(seconds=3),
            )
        )

    with factory.begin() as session:
        assert not maintenance._select_candidates(
            session, authority.department_id, 300, 1, apply=True
        )
    with factory() as session:
        cursor = session.get(
            AdapterArtifactReconciliationCursor,
            (authority.department_id, "registry"),
        )
        assert cursor is not None
        assert cursor.cursor_created_at == base + timedelta(seconds=1)
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            )
            == 0
        )


def test_keyset_window_wraps_to_oldest_rows_with_exact_boundary(
    factory, authority: Authority, tmp_path: Path
) -> None:
    """An exhausted suffix wraps without an unbounded history query."""

    _storage(tmp_path)
    base = datetime.now(UTC) - timedelta(days=1)
    first_id = _add_failed_source_attempt(factory, authority, attempt_number=2, created_at=base)
    second_id = _add_failed_source_attempt(
        factory, authority, attempt_number=3, created_at=base + timedelta(seconds=1)
    )
    with factory() as session:
        cursor = maintenance._AttemptCursor(base + timedelta(seconds=1), second_id)
        window = maintenance._bounded_keyset_attempt_window(
            session,
            model=AdapterImportAttempt,
            department_id=authority.department_id,
            statuses=("failed",),
            cursor=cursor,
            scan_limit=2,
        )
        assert window.wrapped
        assert [attempt.id for attempt in window.rows] == [first_id, second_id]
        assert window.boundary is not None
        assert window.boundary.attempt_id == second_id


@pytest.mark.parametrize(
    ("table_name", "index_name"),
    [
        (
            "adapter_import_attempts",
            "ix_adapter_import_attempt_department_status_created_id",
        ),
        (
            "adapter_registry_attempts",
            "ix_adapter_registry_attempt_department_status_created_id",
        ),
    ],
)
def test_attempt_keyset_indexes_support_tied_created_at_groups(
    factory, authority: Authority, table_name: str, index_name: str
) -> None:
    """The four-column indexes support deterministic tied-timestamp scans."""

    with factory.begin() as session:
        session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = session.execute(
            text(
                f"""
                EXPLAIN (FORMAT JSON)
                SELECT id
                FROM {table_name}
                WHERE department_id = :department_id
                  AND status = 'failed'
                  AND cleanup_confirmed_at IS NULL
                ORDER BY created_at, id
                LIMIT 8
                """
            ),
            {"department_id": str(authority.department_id)},
        ).scalar_one()
        assert isinstance(plan, list) and plan

    nodes: list[dict[str, object]] = []

    def collect(node: dict[str, object]) -> None:
        nodes.append(node)
        for child in node.get("Plans", ()):
            if isinstance(child, dict):
                collect(child)

    collect(plan[0]["Plan"])
    assert any(node.get("Index Name") == index_name for node in nodes)


def test_selection_locks_only_requested_attempts(
    factory, authority: Authority, tmp_path: Path
) -> None:
    """The final lock phase never widens beyond the public attempt limit."""

    _storage(tmp_path)
    _abandon_source(factory, authority)
    second_id = _add_failed_source_attempt(factory, authority, attempt_number=2)
    with factory() as first:
        candidates = maintenance._select_candidates(first, authority.department_id, 300, 1)
        selected = {
            candidate.import_attempt_id
            for candidate in candidates
            if candidate.import_attempt_id is not None
        }
        assert len(selected) == 1
        with factory() as second:
            with pytest.raises(OperationalError):
                second.execute(
                    select(AdapterImportAttempt)
                    .where(AdapterImportAttempt.id.in_(selected))
                    .with_for_update(nowait=True)
                ).all()
        if second_id not in selected:
            with factory() as unselected:
                unselected.execute(
                    select(AdapterImportAttempt)
                    .where(AdapterImportAttempt.id == second_id)
                    .with_for_update(nowait=True)
                ).all()


def test_untried_registry_stage_progresses_past_source_retry_backlog(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    _abandon_source(factory, authority)
    source_stage = _source_stage(root, authority)
    source_stage.chmod(0o755)
    with factory() as session:
        registry_attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert registry_attempt is not None
        registry_stage = (
            root
            / "adapters"
            / ".staging"
            / "registry"
            / str(authority.department_id)
            / str(adapter_id)
            / str(registry_attempt.publication_attempt_id)
        )
    registry_stage.mkdir(mode=0o700, parents=True)
    for path in (registry_stage.parent.parent, registry_stage.parent, registry_stage):
        path.chmod(0o700)
    _file(registry_stage / "adapter_config.json")
    registry_stage.chmod(0o755)

    first = _reconcile(factory, authority, root, apply=True, limit=1)
    assert first.blocked_count == 1
    second = _reconcile(factory, authority, root, apply=True, limit=1)
    assert second.blocked_count == 1
    with factory() as session:
        source_item = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
            )
        )
        registry_item = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_stage",
            )
        )
        assert source_item is not None and source_item.status == "blocked"
        assert registry_item is not None and registry_item.status == "blocked"


def test_blocked_registry_stage_does_not_starve_new_untried_stage(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory() as session:
        first = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert first is not None
        old_publication = first.publication_attempt_id
    old_stage = (
        root
        / "adapters"
        / ".staging"
        / "registry"
        / str(authority.department_id)
        / str(adapter_id)
        / str(old_publication)
    )
    old_stage.mkdir(mode=0o700, parents=True)
    for path in (old_stage.parent.parent, old_stage.parent):
        path.chmod(0o700)
    _file(old_stage / "adapter_config.json")
    old_stage.chmod(0o755)
    first_result = _reconcile(factory, authority, root, apply=True, limit=1)
    assert first_result.blocked_count == 1
    second_publication = uuid4()
    second_id = _add_failed_registry_attempt(
        factory,
        authority,
        adapter_id,
        attempt_number=2,
        publication_attempt_id=second_publication,
    )
    new_stage = (
        root
        / "adapters"
        / ".staging"
        / "registry"
        / str(authority.department_id)
        / str(adapter_id)
        / str(second_publication)
    )
    new_stage.mkdir(mode=0o700, parents=True)
    for path in (new_stage.parent.parent, new_stage.parent):
        path.chmod(0o700)
    _file(new_stage / "adapter_config.json")
    second_result = _reconcile(factory, authority, root, apply=True, limit=1)
    assert second_result.blocked_count == 0
    with factory() as session:
        row = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_stage",
                AdapterArtifactOperationItem.registry_attempt_id == second_id,
            )
        )
        assert row is not None and row.status == "completed"


def test_mixed_reconciliation_emits_one_success_audit_with_blocked_item(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    shutil.rmtree(final)
    second_publication = uuid4()
    second_id = _add_failed_registry_attempt(
        factory,
        authority,
        adapter_id,
        attempt_number=2,
        publication_attempt_id=second_publication,
    )
    blocked_stage = (
        root
        / "adapters"
        / ".staging"
        / "registry"
        / str(authority.department_id)
        / str(adapter_id)
        / str(second_publication)
    )
    blocked_stage.mkdir(mode=0o700, parents=True)
    for path in (blocked_stage.parent.parent, blocked_stage.parent):
        path.chmod(0o700)
    _file(blocked_stage / "adapter_config.json")
    blocked_stage.chmod(0o755)
    result = _reconcile(factory, authority, root, apply=True, limit=10)
    assert result.completed_count >= 1
    assert result.blocked_count == 1
    with factory() as session:
        operation = session.scalar(
            select(AdapterArtifactOperation)
            .where(AdapterArtifactOperation.department_id == authority.department_id)
            .order_by(AdapterArtifactOperation.created_at.desc())
        )
        assert operation is not None and operation.status == "completed_with_blocks"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
                AdapterRegistryAttempt.attempt_number == 1,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        blocked = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.registry_attempt_id == second_id,
                AdapterArtifactOperationItem.surface_type == "registry_stage",
            )
        )
        assert blocked is not None and blocked.status == "blocked"

    blocked_stage.chmod(0o700)
    retry = _reconcile(factory, authority, root, apply=True, limit=10)
    assert retry.completed_count == 1
    assert retry.blocked_count == 0
    assert not blocked_stage.exists()
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
                AdapterRegistryAttempt.attempt_number == 2,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_partial_attempt_cleanup_does_not_emit_premature_success_audit(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None
        stage = (
            root
            / "adapters"
            / ".staging"
            / "registry"
            / str(authority.department_id)
            / str(adapter_id)
            / str(attempt.publication_attempt_id)
        )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    final.chmod(0o755)

    first = _reconcile(factory, authority, root, apply=True, limit=10)
    assert first.completed_count == 1
    assert first.blocked_count == 1
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 0
        )

    final.chmod(0o700)
    second = _reconcile(factory, authority, root, apply=True, limit=10)
    assert second.completed_count == 1
    assert second.blocked_count == 0
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_blocked_final_sibling_history_progresses_before_retry(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory.begin() as session:
        first = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert first is not None and isinstance(first.ownership_manifest, dict)
        first_id = first.id
        second_manifest = dict(first.ownership_manifest)
        second_manifest["publication_attempt_id"] = str(uuid4())
        second_manifest["attempt_number"] = 2
        second = AdapterRegistryAttempt(
            id=uuid4(),
            department_id=authority.department_id,
            adapter_id=adapter_id,
            attempt_number=2,
            publication_attempt_id=UUID(second_manifest["publication_attempt_id"]),
            execution_scope_id=uuid4(),
            code_revision=first.code_revision,
            status="failed",
            ownership_manifest=second_manifest,
            error_code="adapter_registry_publication_failed",
            finished_at=datetime.now(UTC),
            version=1,
        )
        session.add(second)
        second_id = second.id
    final.chmod(0o755)
    first_result = _reconcile(factory, authority, root, apply=True)
    assert first_result.blocked_count == 1
    final.chmod(0o700)
    matching_raw = canonical_json_bytes(second_manifest)
    parse_registry_manifest(matching_raw)
    _file(final / "manifest.json", matching_raw)
    second_result = _reconcile(factory, authority, root, apply=True)
    assert second_result.blocked_count == 0
    assert not final.exists()
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_final",
            )
            .order_by(AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id)
        ).all()
        assert [row.registry_attempt_id for row in rows] == [first_id, second_id]
        assert [row.status for row in rows] == ["blocked", "completed"]


def test_all_blocked_final_siblings_are_bounded_and_retryable(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory.begin() as session:
        first = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert first is not None and isinstance(first.ownership_manifest, dict)
        second_manifest = dict(first.ownership_manifest)
        second_manifest["publication_attempt_id"] = str(uuid4())
        second_manifest["attempt_number"] = 2
        session.add(
            AdapterRegistryAttempt(
                id=uuid4(),
                department_id=authority.department_id,
                adapter_id=adapter_id,
                attempt_number=2,
                publication_attempt_id=UUID(second_manifest["publication_attempt_id"]),
                execution_scope_id=uuid4(),
                code_revision=first.code_revision,
                status="failed",
                ownership_manifest=second_manifest,
                error_code="adapter_registry_publication_failed",
                finished_at=datetime.now(UTC),
                version=1,
            )
        )
        third_manifest = dict(first.ownership_manifest)
        third_manifest["publication_attempt_id"] = str(uuid4())
        third_manifest["attempt_number"] = 3
        session.add(
            AdapterRegistryAttempt(
                id=uuid4(),
                department_id=authority.department_id,
                adapter_id=adapter_id,
                attempt_number=3,
                publication_attempt_id=UUID(third_manifest["publication_attempt_id"]),
                execution_scope_id=uuid4(),
                code_revision=first.code_revision,
                status="failed",
                ownership_manifest=third_manifest,
                error_code="adapter_registry_publication_failed",
                finished_at=datetime.now(UTC),
                version=1,
            )
        )
    final.chmod(0o755)
    results = [_reconcile(factory, authority, root, apply=True, limit=1) for _ in range(3)]
    assert all(result.eligible_count <= 2 for result in results)
    assert all(result.blocked_count == 1 for result in results)
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_final",
            )
        ).all()
        assert len(rows) == 3
        assert all(row.status == "blocked" for row in rows)
        assert len({row.registry_attempt_id for row in rows}) == 3
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperation.id)).where(
                    AdapterArtifactOperation.department_id == authority.department_id,
                    AdapterArtifactOperation.status == "registered",
                )
            )
            == 0
        )
    assert final.exists()


def test_all_blocked_operation_emits_no_success_audit(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    final.chmod(0o755)
    with factory() as session:
        first = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert first is not None
        first_stage = (
            root
            / "adapters"
            / ".staging"
            / "registry"
            / str(authority.department_id)
            / str(adapter_id)
            / str(first.publication_attempt_id)
        )
    first_stage.mkdir(mode=0o700, parents=True)
    _file(first_stage / "adapter_config.json")
    first_stage.chmod(0o755)
    result = _reconcile(factory, authority, root, apply=True, limit=10)
    assert result.completed_count == 0
    assert result.blocked_count == 2
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 0
        )


def test_blocked_retry_dry_run_is_read_only(factory, authority: Authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    stage = _source_stage(root, authority)
    stage.chmod(0o755)
    first = _reconcile(factory, authority, root, apply=True)
    assert first.blocked_count == 1
    stage.chmod(0o700)
    with factory() as session:
        operation_count = session.scalar(
            select(func.count(AdapterArtifactOperation.id)).where(
                AdapterArtifactOperation.department_id == authority.department_id
            )
        )
        item_count = session.scalar(
            select(func.count(AdapterArtifactOperationItem.id)).where(
                AdapterArtifactOperationItem.department_id == authority.department_id
            )
        )
    dry_run = _reconcile(factory, authority, root, apply=False, limit=1)
    assert dry_run.eligible_count == 1
    assert stage.exists()
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperation.id)).where(
                    AdapterArtifactOperation.department_id == authority.department_id
                )
            )
            == operation_count
        )
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            )
            == item_count
        )


def test_both_source_surfaces_absent_are_confirmed_once(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    result = _reconcile(factory, authority, root, apply=True)
    assert result.eligible_count == 2
    assert result.completed_count == 2
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None


def test_mismatched_registry_sibling_does_not_starve_matching_attempt(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory.begin() as session:
        first = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert first is not None and isinstance(first.ownership_manifest, dict)
        matching = dict(first.ownership_manifest)
        matching["publication_attempt_id"] = str(uuid4())
        matching["attempt_number"] = 2
        matching_raw = canonical_json_bytes(matching)
        parse_registry_manifest(matching_raw)
        second = AdapterRegistryAttempt(
            id=uuid4(),
            department_id=authority.department_id,
            adapter_id=adapter_id,
            attempt_number=2,
            publication_attempt_id=UUID(matching["publication_attempt_id"]),
            execution_scope_id=uuid4(),
            code_revision=first.code_revision,
            status="failed",
            ownership_manifest=matching,
            error_code="adapter_registry_publication_failed",
            finished_at=datetime.now(UTC),
            version=1,
        )
        session.add(second)
        session.flush()
    _file(final / "manifest.json", matching_raw)
    first_result = _reconcile(factory, authority, root, apply=True, limit=100)
    assert first_result.blocked_count >= 1
    assert first_result.eligible_count == 3
    assert final.exists()
    second_result = _reconcile(factory, authority, root, apply=True, limit=100)
    assert second_result.eligible_count == 1
    assert second_result.completed_count == 1
    assert not final.exists()
    with factory() as session:
        attempts = session.scalars(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        ).all()
        assert any(attempt.cleanup_confirmed_at is not None for attempt in attempts)


def test_physical_surface_indexes_reject_sibling_final_attempts(
    engine, factory, authority: Authority
) -> None:
    indexes = {
        row["name"]: row for row in inspect(engine).get_indexes("adapter_artifact_operation_items")
    }
    assert indexes["uq_adapter_artifact_item_active_source_stage"]["column_names"] == [
        "department_id",
        "surface_type",
        "source_bundle_id",
        "import_attempt_id",
    ]
    assert indexes["uq_adapter_artifact_item_active_registry_stage"]["column_names"] == [
        "department_id",
        "surface_type",
        "adapter_id",
        "publication_attempt_id",
    ]
    assert indexes["uq_adapter_artifact_item_active_source_final"]["column_names"] == [
        "department_id",
        "surface_type",
        "source_bundle_id",
    ]
    assert indexes["uq_adapter_artifact_item_active_registry_final"]["column_names"] == [
        "department_id",
        "surface_type",
        "adapter_id",
    ]

    now = datetime.now(UTC)
    sibling_attempt_id = uuid4()
    sibling_publication_id = uuid4()
    with factory.begin() as session:
        session.add(
            AdapterImportAttempt(
                id=sibling_attempt_id,
                department_id=authority.department_id,
                source_bundle_id=authority.source_id,
                attempt_number=2,
                publication_attempt_id=sibling_publication_id,
                status="failed",
                code_revision=authority.code_revision,
                error_code="adapter_source_publication_failed",
                finished_at=now,
                version=1,
            )
        )
        operation_one = AdapterArtifactOperation(
            id=uuid4(),
            department_id=authority.department_id,
            requested_by_user_id=authority.admin_id,
            operation_type="reconcile",
            status="registered",
            limit_value=1,
            minimum_age_seconds=300,
            eligible_count=1,
            version=1,
        )
        session.add(operation_one)
        session.flush()
        common = {
            "department_id": authority.department_id,
            "surface_type": "source_final",
            "source_bundle_id": authority.source_id,
            "adapter_id": None,
            "registry_attempt_id": None,
            "expected_resource_version": authority.source_version,
            "ownership_manifest": {},
            "status": "registered",
            "version": 1,
        }
        session.add(
            AdapterArtifactOperationItem(
                **common,
                id=uuid4(),
                operation_id=operation_one.id,
                import_attempt_id=authority.source_attempt_id,
                publication_attempt_id=authority.source_publication_attempt_id,
                attempt_number=1,
                expected_attempt_version=authority.source_attempt_version,
            )
        )
        session.flush()
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    AdapterArtifactOperationItem(
                        **common,
                        id=uuid4(),
                        operation_id=operation_one.id,
                        import_attempt_id=sibling_attempt_id,
                        publication_attempt_id=sibling_publication_id,
                        attempt_number=2,
                        expected_attempt_version=1,
                    )
                )
                session.flush()


def test_invalid_source_manifests_do_not_fill_bounded_selection_window(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    malformed = {"malformed": True}
    with factory.begin() as session:
        original = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert original is not None
        original.ownership_manifest = malformed
        original.version += 1
    old_attempt_ids = [authority.source_attempt_id]
    _seed_completed_stage_item(factory, authority, authority.source_attempt_id, family="source")
    for attempt_number in range(2, 2 + RECONCILIATION_SCAN_MULTIPLIER):
        attempt_id = _add_failed_source_attempt(
            factory, authority, attempt_number=attempt_number, manifest=malformed
        )
        old_attempt_ids.append(attempt_id)
        _seed_completed_stage_item(factory, authority, attempt_id, family="source")

    # Materialize one durable invalid-final quarantine for every old row.
    # Subsequent bounded scans must skip those unchanged attempt versions.
    for _ in old_attempt_ids:
        result = _reconcile(factory, authority, root, apply=True, limit=1)
        assert result.blocked_count == 1
    fresh_id = _add_failed_source_attempt(
        factory, authority, attempt_number=2 + RECONCILIATION_SCAN_MULTIPLIER
    )
    result = _reconcile(factory, authority, root, apply=True, limit=1)
    assert result.completed_count == 1
    assert result.blocked_count == 0
    with factory() as session:
        fresh = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
                AdapterArtifactOperationItem.import_attempt_id == fresh_id,
                AdapterArtifactOperationItem.status == "completed",
            )
        )
        assert fresh is not None
        old_attempts = session.scalars(
            select(AdapterImportAttempt).where(
                AdapterImportAttempt.department_id == authority.department_id,
                AdapterImportAttempt.id.in_(old_attempt_ids),
            )
        ).all()
        assert all(attempt.cleanup_confirmed_at is None for attempt in old_attempts)
        old_final_items = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_final",
                AdapterArtifactOperationItem.import_attempt_id.in_(old_attempt_ids),
            )
        ).all()
        assert len(old_final_items) == len(old_attempt_ids)
        assert all(
            item.status == "blocked" and item.blocked_reason_code == "artifact_manifest_invalid"
            for item in old_final_items
        )
        old_versions = {item.import_attempt_id: item.version for item in old_final_items}
    assert _reconcile(factory, authority, root, apply=False, limit=1).eligible_count == 0
    with factory() as session:
        unchanged = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_final",
                AdapterArtifactOperationItem.import_attempt_id.in_(old_attempt_ids),
            )
        ).all()
        assert {item.import_attempt_id: item.version for item in unchanged} == old_versions


def test_invalid_registry_manifests_do_not_fill_bounded_selection_window(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    enqueue = _enqueue(factory, authority, apply=True)
    claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None and enqueue.adapter_id == claim.id
    terminal_failure(factory, claim, "adapter_registry_publication_failed")
    malformed = {"malformed": True}
    with factory.begin() as session:
        original = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert original is not None
        original.ownership_manifest = malformed
        original.version += 1
    old_attempt_ids = [claim.registry_attempt_id]
    _seed_completed_stage_item(
        factory, authority, claim.registry_attempt_id, family="registry", adapter_id=claim.id
    )
    for attempt_number in range(2, 2 + RECONCILIATION_SCAN_MULTIPLIER):
        attempt_id = _add_failed_registry_attempt(
            factory,
            authority,
            claim.id,
            attempt_number=attempt_number,
            manifest=malformed,
        )
        old_attempt_ids.append(attempt_id)
        _seed_completed_stage_item(
            factory, authority, attempt_id, family="registry", adapter_id=claim.id
        )

    for _ in old_attempt_ids:
        result = _reconcile(factory, authority, root, apply=True, limit=1)
        assert result.blocked_count == 1
    fresh_id = _add_failed_registry_attempt(
        factory,
        authority,
        claim.id,
        attempt_number=2 + RECONCILIATION_SCAN_MULTIPLIER,
    )
    result = _reconcile(factory, authority, root, apply=True, limit=1)
    assert result.completed_count == 1
    assert result.blocked_count == 0
    with factory() as session:
        fresh = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_stage",
                AdapterArtifactOperationItem.registry_attempt_id == fresh_id,
                AdapterArtifactOperationItem.status == "completed",
            )
        )
        assert fresh is not None
        old_attempts = session.scalars(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.id.in_(old_attempt_ids),
            )
        ).all()
        assert all(attempt.cleanup_confirmed_at is None for attempt in old_attempts)
        old_final_items = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_final",
                AdapterArtifactOperationItem.registry_attempt_id.in_(old_attempt_ids),
            )
        ).all()
        assert len(old_final_items) == len(old_attempt_ids)
        assert all(
            item.status == "blocked" and item.blocked_reason_code == "artifact_manifest_invalid"
            for item in old_final_items
        )
        old_versions = {item.registry_attempt_id: item.version for item in old_final_items}
    assert _reconcile(factory, authority, root, apply=False, limit=1).eligible_count == 0
    with factory() as session:
        unchanged = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_final",
                AdapterArtifactOperationItem.registry_attempt_id.in_(old_attempt_ids),
            )
        ).all()
        assert {item.registry_attempt_id: item.version for item in unchanged} == old_versions


def test_invalid_source_final_quarantine_reactivates_after_version_repair(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    with factory.begin() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and isinstance(attempt.ownership_manifest, dict)
        repaired_manifest = dict(attempt.ownership_manifest)
        attempt.ownership_manifest = {"malformed": True}
        attempt.version += 1
        malformed_version = attempt.version
    _seed_completed_stage_item(factory, authority, authority.source_attempt_id, family="source")

    first = _reconcile(factory, authority, root, apply=True, limit=1)
    assert first.blocked_count == 1
    with factory() as session:
        blocked = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_final",
                AdapterArtifactOperationItem.import_attempt_id == authority.source_attempt_id,
                AdapterArtifactOperationItem.status == "blocked",
            )
        )
        assert blocked is not None
        assert blocked.blocked_reason_code == "artifact_manifest_invalid"
        assert blocked.expected_attempt_version == malformed_version

    # The unchanged malformed final is durably quarantined, so it no longer
    # consumes a selection slot or creates a no-op retry.
    assert _reconcile(factory, authority, root, apply=False, limit=1).eligible_count == 0

    with factory.begin() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None
        attempt.ownership_manifest = repaired_manifest
        attempt.version += 1
        repaired_version = attempt.version
    repaired = _reconcile(factory, authority, root, apply=True, limit=1)
    assert repaired.completed_count == 1
    with factory() as session:
        repaired_item = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_final",
                AdapterArtifactOperationItem.import_attempt_id == authority.source_attempt_id,
                AdapterArtifactOperationItem.status == "completed",
                AdapterArtifactOperationItem.expected_attempt_version == repaired_version,
            )
        )
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert repaired_item is not None
        assert attempt is not None and attempt.cleanup_confirmed_at is not None


def test_invalid_registry_final_quarantine_reactivates_after_version_repair(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory.begin() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and isinstance(attempt.ownership_manifest, dict)
        repaired_manifest = dict(attempt.ownership_manifest)
        attempt.ownership_manifest = {"malformed": True}
        attempt.version += 1
        malformed_version = attempt.version
        attempt_id = attempt.id
    _seed_completed_stage_item(
        factory, authority, attempt_id, family="registry", adapter_id=adapter_id
    )

    first = _reconcile(factory, authority, root, apply=True, limit=1)
    assert first.blocked_count == 1
    with factory() as session:
        blocked = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_final",
                AdapterArtifactOperationItem.registry_attempt_id == attempt_id,
                AdapterArtifactOperationItem.status == "blocked",
            )
        )
        assert blocked is not None
        assert blocked.blocked_reason_code == "artifact_manifest_invalid"
        assert blocked.expected_attempt_version == malformed_version
    assert _reconcile(factory, authority, root, apply=False, limit=1).eligible_count == 0

    with factory.begin() as session:
        attempt = session.get(AdapterRegistryAttempt, attempt_id)
        assert attempt is not None
        attempt.ownership_manifest = repaired_manifest
        attempt.version += 1
        repaired_version = attempt.version
    repaired = _reconcile(factory, authority, root, apply=True, limit=1)
    assert repaired.completed_count == 1
    assert not final.exists()
    with factory() as session:
        repaired_item = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_final",
                AdapterArtifactOperationItem.registry_attempt_id == attempt_id,
                AdapterArtifactOperationItem.status == "completed",
                AdapterArtifactOperationItem.expected_attempt_version == repaired_version,
            )
        )
        attempt = session.get(AdapterRegistryAttempt, attempt_id)
        assert repaired_item is not None
        assert attempt is not None and attempt.cleanup_confirmed_at is not None


def test_source_confirmation_backlog_yields_to_registry_physical_work(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _enqueue(factory, authority, apply=True)
    claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    registry_manifest, registry_manifest_raw = _registry_manifest_for_claim(claim, b"{}", b"model")
    terminal_failure(factory, claim, "adapter_registry_publication_failed")
    with factory.begin() as session:
        attempt = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert attempt is not None
        attempt.ownership_manifest = registry_manifest
        attempt.version += 1
    _registry_final(root, authority, claim, registry_manifest_raw)

    _abandon_source(factory, authority)
    with factory() as session:
        source_attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert source_attempt is not None and isinstance(source_attempt.ownership_manifest, dict)
        source_manifest = dict(source_attempt.ownership_manifest)
    _seed_completed_stage_item(factory, authority, authority.source_attempt_id, family="source")
    _seed_completed_final_item(
        factory,
        authority,
        authority.source_attempt_id,
        family="source",
        manifest=source_manifest,
    )
    _unknown_tombstone(root, "source_stage", authority.department_id)

    registry_stage = _reconcile(factory, authority, root, apply=True, limit=1)
    assert registry_stage.surface_counts["registry_stage"] == 1
    assert registry_stage.surface_counts["registry_final"] == 1
    assert registry_stage.completed_count == 2
    source_retry = _reconcile(factory, authority, root, apply=True, limit=1)
    assert source_retry.surface_counts["source_stage"] == 1
    assert source_retry.blocked_count == 1
    with factory() as session:
        source_marker = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
                AdapterArtifactOperationItem.import_attempt_id == authority.source_attempt_id,
                AdapterArtifactOperationItem.status == "blocked",
            )
        )
        assert source_marker is not None
        assert source_marker.blocked_reason_code == "artifact_authority_changed"


def test_registry_confirmation_backlog_yields_to_source_physical_work(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and isinstance(attempt.ownership_manifest, dict)
        registry_manifest = dict(attempt.ownership_manifest)
        attempt_id = attempt.id
    _seed_completed_stage_item(
        factory, authority, attempt_id, family="registry", adapter_id=adapter_id
    )
    _seed_completed_final_item(
        factory,
        authority,
        attempt_id,
        family="registry",
        adapter_id=adapter_id,
        manifest=registry_manifest,
    )
    _unknown_tombstone(root, "registry_stage", authority.department_id)

    _abandon_source(factory, authority)
    _source_stage(root, authority)
    source_stage = _reconcile(factory, authority, root, apply=True, limit=1)
    assert source_stage.surface_counts["source_stage"] == 1
    assert source_stage.surface_counts["source_final"] == 1
    assert source_stage.completed_count == 2
    registry_retry = _reconcile(factory, authority, root, apply=True, limit=1)
    assert registry_retry.surface_counts["registry_stage"] == 1
    assert registry_retry.blocked_count == 1
    assert final.exists()
    with factory() as session:
        registry_marker = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_stage",
                AdapterArtifactOperationItem.registry_attempt_id == attempt_id,
                AdapterArtifactOperationItem.status == "blocked",
            )
        )
        assert registry_marker is not None
        assert registry_marker.blocked_reason_code == "artifact_authority_changed"


def test_confirmation_marker_resume_completes_once_after_crash(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    marker = {"phase12_1e_a_confirmation_only": True}
    operation_id = uuid4()
    with factory() as session:
        source_attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert source_attempt is not None and isinstance(source_attempt.ownership_manifest, dict)
        source_manifest = dict(source_attempt.ownership_manifest)
    _seed_completed_final_item(
        factory,
        authority,
        authority.source_attempt_id,
        family="source",
        manifest=source_manifest,
    )
    _seed_completed_stage_item(factory, authority, authority.source_attempt_id, family="source")
    with factory.begin() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert attempt is not None and source is not None
        operation = AdapterArtifactOperation(
            id=operation_id,
            department_id=authority.department_id,
            requested_by_user_id=authority.admin_id,
            operation_type="reconcile",
            status="registered",
            limit_value=1,
            minimum_age_seconds=300,
            eligible_count=1,
            version=1,
        )
        session.add(operation)
        session.flush()
        session.add(
            AdapterArtifactOperationItem(
                id=uuid4(),
                operation_id=operation_id,
                department_id=authority.department_id,
                surface_type="source_stage",
                source_bundle_id=authority.source_id,
                import_attempt_id=authority.source_attempt_id,
                publication_attempt_id=attempt.publication_attempt_id,
                attempt_number=attempt.attempt_number,
                expected_resource_version=source.version,
                expected_attempt_version=attempt.version,
                ownership_manifest=marker,
                status="registered",
                version=1,
            )
        )
    resumed = _reconcile(factory, authority, root, apply=True, limit=1)
    assert resumed.completed_count == 1
    with factory() as session:
        item = session.scalar(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.operation_id == operation_id
            )
        )
        operation = session.get(AdapterArtifactOperation, operation_id)
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        audits = session.scalars(
            select(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == authority.department_id,
                PersistentAuditEvent.action == "adapter.artifact.reconcile",
            )
        ).all()
        assert item is not None and item.status == "completed"
        assert operation is not None and operation.status == "completed"
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert len(audits) == 1


def test_persistent_source_confirmation_backlog_is_fair_and_repairable(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    with factory.begin() as session:
        original = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert original is not None and isinstance(original.ownership_manifest, dict)
        base_manifest = dict(original.ownership_manifest)
    old_attempt_ids = [authority.source_attempt_id]
    _seed_completed_stage_item(factory, authority, authority.source_attempt_id, family="source")
    _seed_completed_final_item(
        factory,
        authority,
        authority.source_attempt_id,
        family="source",
        manifest=base_manifest,
    )
    for attempt_number in range(2, 2 + RECONCILIATION_SCAN_MULTIPLIER - 1):
        attempt_id = uuid4()
        publication_attempt_id = uuid4()
        manifest = dict(base_manifest)
        manifest.update(
            {
                "import_attempt_id": str(attempt_id),
                "publication_attempt_id": str(publication_attempt_id),
                "attempt_number": attempt_number,
            }
        )
        parse_source_manifest(canonical_manifest_bytes(manifest))
        attempt_id = _add_failed_source_attempt(
            factory,
            authority,
            attempt_number=attempt_number,
            manifest=manifest,
            publication_attempt_id=publication_attempt_id,
            attempt_id=attempt_id,
        )
        old_attempt_ids.append(attempt_id)
        _seed_completed_stage_item(factory, authority, attempt_id, family="source")
        _seed_completed_final_item(
            factory, authority, attempt_id, family="source", manifest=manifest
        )

    unknown = _unknown_tombstone(root, "source_stage", authority.department_id)
    for _ in old_attempt_ids:
        result = _reconcile(factory, authority, root, apply=True, limit=1)
        assert result.completed_count == 0
        assert result.blocked_count == 1
    with factory() as session:
        markers = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "source_stage",
                AdapterArtifactOperationItem.import_attempt_id.in_(old_attempt_ids),
                AdapterArtifactOperationItem.status == "blocked",
            )
        ).all()
        assert len(markers) == len(old_attempt_ids)
        assert all(
            marker.status == "blocked"
            and marker.blocked_reason_code == "artifact_authority_changed"
            and marker.ownership_manifest == {"phase12_1e_a_confirmation_only": True}
            for marker in markers
        )
        assert all(
            session.get(AdapterImportAttempt, attempt_id).cleanup_confirmed_at is None
            for attempt_id in old_attempt_ids
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 0
        )

    # A second fenced pass rotates by durable marker count/time instead of
    # repeatedly selecting one oldest attempt.
    assert _reconcile(factory, authority, root, apply=True, limit=1).blocked_count == 1
    with factory() as session:
        marker_counts = {
            attempt_id: session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id,
                    AdapterArtifactOperationItem.surface_type == "source_stage",
                    AdapterArtifactOperationItem.import_attempt_id == attempt_id,
                    AdapterArtifactOperationItem.status == "blocked",
                )
            )
            for attempt_id in old_attempt_ids
        }
        assert sorted(marker_counts.values()) == [1] * (len(old_attempt_ids) - 1) + [2]

    new_id = uuid4()
    new_publication_id = uuid4()
    new_manifest = dict(base_manifest)
    new_manifest.update(
        {
            "import_attempt_id": str(new_id),
            "publication_attempt_id": str(new_publication_id),
            "attempt_number": len(old_attempt_ids) + 1,
        }
    )
    parse_source_manifest(canonical_manifest_bytes(new_manifest))
    _add_failed_source_attempt(
        factory,
        authority,
        attempt_number=len(old_attempt_ids) + 1,
        manifest=new_manifest,
        publication_attempt_id=new_publication_id,
        attempt_id=new_id,
    )
    final = _source_final(root, authority, new_manifest)
    with factory.begin() as session:
        attempt = session.get(AdapterImportAttempt, new_id)
        assert attempt is not None
        attempt.ownership_manifest = dict(new_manifest)
        attempt.version += 1
    _seed_completed_stage_item(factory, authority, new_id, family="source")
    final.chmod(0o755)
    blocked_final = _reconcile(factory, authority, root, apply=True, limit=1)
    assert blocked_final.blocked_count == 1
    final.chmod(0o700)
    repaired_final = _reconcile(factory, authority, root, apply=True, limit=1)
    assert repaired_final.completed_count == 1
    assert repaired_final.blocked_count == 0
    assert not final.exists()

    # Once the reviewed fence is removed, every confirmation remains
    # retryable and completes without a second success audit for the resource.
    shutil.rmtree(unknown.parent.parent)
    for _ in range(len(old_attempt_ids) + 2):
        result = _reconcile(factory, authority, root, apply=True, limit=1)
        if result.eligible_count == 0:
            break
        assert result.completed_count == 1
    with factory() as session:
        assert all(
            session.get(AdapterImportAttempt, attempt_id).cleanup_confirmed_at is not None
            for attempt_id in (*old_attempt_ids, new_id)
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_persistent_registry_confirmation_backlog_is_fair_and_repairable(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    old_final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory.begin() as session:
        original = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert original is not None and isinstance(original.ownership_manifest, dict)
        base_manifest = dict(original.ownership_manifest)
        original_id = original.id
    shutil.rmtree(old_final)
    old_attempt_ids = [original_id]
    _seed_completed_stage_item(
        factory, authority, original_id, family="registry", adapter_id=adapter_id
    )
    _seed_completed_final_item(
        factory,
        authority,
        original_id,
        family="registry",
        adapter_id=adapter_id,
        manifest=base_manifest,
    )
    for attempt_number in range(2, 2 + RECONCILIATION_SCAN_MULTIPLIER - 1):
        attempt_id = uuid4()
        publication_attempt_id = uuid4()
        manifest = dict(base_manifest)
        manifest.update(
            {
                "publication_attempt_id": str(publication_attempt_id),
                "attempt_number": attempt_number,
            }
        )
        manifest = parse_registry_manifest(canonical_json_bytes(manifest))
        attempt_id = _add_failed_registry_attempt(
            factory,
            authority,
            adapter_id,
            attempt_number=attempt_number,
            manifest=manifest,
            publication_attempt_id=publication_attempt_id,
            attempt_id=attempt_id,
        )
        old_attempt_ids.append(attempt_id)
        _seed_completed_stage_item(
            factory, authority, attempt_id, family="registry", adapter_id=adapter_id
        )
        _seed_completed_final_item(
            factory,
            authority,
            attempt_id,
            family="registry",
            adapter_id=adapter_id,
            manifest=manifest,
        )

    unknown = _unknown_tombstone(root, "registry_stage", authority.department_id)
    for _ in old_attempt_ids:
        result = _reconcile(factory, authority, root, apply=True, limit=1)
        assert result.completed_count == 0
        assert result.blocked_count == 1
    with factory() as session:
        markers = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id,
                AdapterArtifactOperationItem.surface_type == "registry_stage",
                AdapterArtifactOperationItem.registry_attempt_id.in_(old_attempt_ids),
                AdapterArtifactOperationItem.status == "blocked",
            )
        ).all()
        assert len(markers) == len(old_attempt_ids)
        assert all(
            marker.status == "blocked"
            and marker.blocked_reason_code == "artifact_authority_changed"
            and marker.ownership_manifest == {"phase12_1e_a_confirmation_only": True}
            for marker in markers
        )
        assert all(
            session.get(AdapterRegistryAttempt, attempt_id).cleanup_confirmed_at is None
            for attempt_id in old_attempt_ids
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 0
        )

    assert _reconcile(factory, authority, root, apply=True, limit=1).blocked_count == 1
    with factory() as session:
        marker_counts = {
            attempt_id: session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id,
                    AdapterArtifactOperationItem.surface_type == "registry_stage",
                    AdapterArtifactOperationItem.registry_attempt_id == attempt_id,
                    AdapterArtifactOperationItem.status == "blocked",
                )
            )
            for attempt_id in old_attempt_ids
        }
        assert sorted(marker_counts.values()) == [1] * (len(old_attempt_ids) - 1) + [2]

    new_id = uuid4()
    new_publication_id = uuid4()
    new_attempt_number = len(old_attempt_ids) + 1
    new_manifest = dict(base_manifest)
    new_manifest.update(
        {
            "publication_attempt_id": str(new_publication_id),
            "attempt_number": new_attempt_number,
        }
    )
    new_manifest_raw = canonical_json_bytes(new_manifest)
    new_manifest = parse_registry_manifest(new_manifest_raw)
    _add_failed_registry_attempt(
        factory,
        authority,
        adapter_id,
        attempt_number=new_attempt_number,
        manifest=new_manifest,
        publication_attempt_id=new_publication_id,
        attempt_id=new_id,
    )
    claim_like = SimpleNamespace(
        id=adapter_id,
        publication_attempt_id=new_publication_id,
        attempt_number=new_attempt_number,
    )
    final = _registry_final(root, authority, claim_like, new_manifest_raw)
    with factory.begin() as session:
        attempt = session.get(AdapterRegistryAttempt, new_id)
        assert attempt is not None
        attempt.ownership_manifest = dict(new_manifest)
        attempt.version += 1
    _seed_completed_stage_item(factory, authority, new_id, family="registry", adapter_id=adapter_id)
    final.chmod(0o755)
    blocked_final = _reconcile(factory, authority, root, apply=True, limit=1)
    assert blocked_final.blocked_count == 1
    final.chmod(0o700)
    repaired_final = _reconcile(factory, authority, root, apply=True, limit=1)
    assert repaired_final.completed_count == 1
    assert repaired_final.blocked_count == 0
    assert not final.exists()

    shutil.rmtree(unknown.parent.parent)
    for _ in range(len(old_attempt_ids) + 2):
        result = _reconcile(factory, authority, root, apply=True, limit=1)
        if result.eligible_count == 0:
            break
        assert result.completed_count == 1
    with factory() as session:
        assert all(
            session.get(AdapterRegistryAttempt, attempt_id).cleanup_confirmed_at is not None
            for attempt_id in (*old_attempt_ids, new_id)
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_source_confirmation_retry_is_read_only_and_idempotent(
    factory, authority: Authority, tmp_path: Path, monkeypatch
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    unknown = _unknown_tombstone(root, "source_stage", authority.department_id)
    first = _reconcile(factory, authority, root, apply=True, limit=10)
    assert first.completed_count == 2
    assert first.blocked_count == 0
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is None
        item_statuses = [
            row.status
            for row in session.scalars(
                select(AdapterArtifactOperationItem).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            ).all()
        ]
        operation_count = session.scalar(
            select(func.count(AdapterArtifactOperation.id)).where(
                AdapterArtifactOperation.department_id == authority.department_id
            )
        )
        item_count = session.scalar(
            select(func.count(AdapterArtifactOperationItem.id)).where(
                AdapterArtifactOperationItem.department_id == authority.department_id
            )
        )
        audit_count = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.department_id == authority.department_id,
                PersistentAuditEvent.action == "adapter.artifact.reconcile",
            )
        )
        assert item_statuses == ["completed", "completed"]
        assert audit_count == 0
    shutil.rmtree(unknown.parent.parent)

    dry_run = _reconcile(factory, authority, root, apply=False, limit=1)
    assert dry_run.eligible_count == 1
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperation.id)).where(
                    AdapterArtifactOperation.department_id == authority.department_id
                )
            )
            == operation_count
        )
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            )
            == item_count
        )
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is None

    from app import adapter_maintenance_artifacts as storage

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("confirmation-only retry must not mutate the filesystem")

    for method in (
        "move_verified_surface_to_tombstone",
        "unlink_committed_tombstone_entry",
        "remove_committed_tombstone_directory",
    ):
        monkeypatch.setattr(storage.AdapterMaintenanceArtifactStore, method, fail_if_called)
    confirmed = _reconcile(factory, authority, root, apply=True, limit=1)
    assert confirmed.completed_count == 1
    assert confirmed.blocked_count == 0
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )
    repeated = _reconcile(factory, authority, root, apply=True, limit=1)
    assert repeated.eligible_count == 0


def test_registry_confirmation_retry_is_read_only_and_idempotent(
    factory, authority: Authority, tmp_path: Path, monkeypatch
) -> None:
    root = _storage(tmp_path)
    final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None
        stage = (
            root
            / "adapters"
            / ".staging"
            / "registry"
            / str(authority.department_id)
            / str(adapter_id)
            / str(attempt.publication_attempt_id)
        )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    unknown = _unknown_tombstone(root, "registry_stage", authority.department_id)
    first = _reconcile(factory, authority, root, apply=True, limit=10)
    assert first.completed_count == 2
    assert first.blocked_count == 0
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is None
        operation_count = session.scalar(
            select(func.count(AdapterArtifactOperation.id)).where(
                AdapterArtifactOperation.department_id == authority.department_id
            )
        )
        item_count = session.scalar(
            select(func.count(AdapterArtifactOperationItem.id)).where(
                AdapterArtifactOperationItem.department_id == authority.department_id
            )
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 0
        )
    shutil.rmtree(unknown.parent.parent)
    dry_run = _reconcile(factory, authority, root, apply=False, limit=1)
    assert dry_run.eligible_count == 1
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperation.id)).where(
                    AdapterArtifactOperation.department_id == authority.department_id
                )
            )
            == operation_count
        )
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            )
            == item_count
        )

    from app import adapter_maintenance_artifacts as storage

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("confirmation-only retry must not mutate the filesystem")

    for method in (
        "move_verified_surface_to_tombstone",
        "unlink_committed_tombstone_entry",
        "remove_committed_tombstone_directory",
    ):
        monkeypatch.setattr(storage.AdapterMaintenanceArtifactStore, method, fail_if_called)
    confirmed = _reconcile(factory, authority, root, apply=True, limit=1)
    assert confirmed.completed_count == 1
    assert confirmed.blocked_count == 0
    with factory() as session:
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )
    assert not final.exists()
    assert not stage.exists()
    repeated = _reconcile(factory, authority, root, apply=True, limit=1)
    assert repeated.eligible_count == 0


def test_cross_family_audit_coverage_deduplicates_covered_source_and_audits_registry(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    registry_final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    registry_final.chmod(0o755)
    first = _reconcile(factory, authority, root, apply=True, limit=10)
    assert first.blocked_count == 1
    with factory() as session:
        source_attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        registry_attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert source_attempt is not None and source_attempt.cleanup_confirmed_at is not None
        assert registry_attempt is not None and registry_attempt.cleanup_confirmed_at is None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )

    retry_publication = uuid4()
    retry_id = uuid4()
    retry_manifest = _source_retry_manifest(factory, authority, retry_id, 2, retry_publication)
    retry_id = _add_failed_source_attempt(
        factory,
        authority,
        attempt_number=2,
        manifest=retry_manifest,
        publication_attempt_id=retry_publication,
    )
    registry_final.chmod(0o700)
    second = _reconcile(factory, authority, root, apply=True, limit=10)
    assert second.blocked_count == 0
    with factory() as session:
        retry_attempt = session.get(AdapterImportAttempt, retry_id)
        registry_attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert retry_attempt is not None and retry_attempt.cleanup_confirmed_at is not None
        assert registry_attempt is not None and registry_attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 2
        )
    assert not registry_final.exists()


def test_cross_family_audit_coverage_deduplicates_registry_and_later_audits_source(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    registry_final, adapter_id = _prepare_registry_crash(factory, authority, root, "published")
    _abandon_source(factory, authority)
    source_stage = _source_stage(root, authority)
    source_stage.chmod(0o755)
    first = _reconcile(factory, authority, root, apply=True, limit=10)
    assert first.blocked_count == 1
    with factory() as session:
        registry_attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        source_attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert registry_attempt is not None and registry_attempt.cleanup_confirmed_at is not None
        assert source_attempt is not None and source_attempt.cleanup_confirmed_at is None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )

    retry_id = _add_failed_registry_attempt(factory, authority, adapter_id, attempt_number=2)
    retry = _reconcile(factory, authority, root, apply=True, limit=1)
    assert retry.blocked_count == 0
    with factory() as session:
        retry_attempt = session.get(AdapterRegistryAttempt, retry_id)
        assert retry_attempt is not None and retry_attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )

    source_stage.chmod(0o700)
    repaired = _reconcile(factory, authority, root, apply=True, limit=10)
    assert repaired.blocked_count == 0
    with factory() as session:
        source_attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert source_attempt is not None and source_attempt.cleanup_confirmed_at is not None
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 2
        )
    assert not registry_final.exists()

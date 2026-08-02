# ruff: noqa: E501
"""Add the immutable Phase 12.1C adapter registry authority.

This migration is intentionally self-contained.  It freezes the registry
schema and its contracts in literals so historical upgrades do not import the
application model or any runtime code.
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa

from alembic import op

revision = "0011_phase12_adapter_registry"
down_revision = "0010_phase12_adapter_sources"
branch_labels = None
depends_on = None

_ERRORS = (
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
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join("'" + value + "'" for value in values)


def _source_lifecycle() -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "(status = 'staging' AND authoritative_attempt_id IS NULL AND committed_at IS NULL "
        "AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL "
        "AND error_code IS NULL AND claimed_adapter_id IS NULL AND claimed_at IS NULL "
        "AND consumed_at IS NULL) OR "
        "(status = 'committed' AND authoritative_attempt_id IS NOT NULL "
        "AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 "
        "AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 "
        "AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL "
        "AND tensor_count = 392 AND tensor_element_count = 10092544 "
        "AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL "
        "AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL "
        "AND error_code IS NULL AND claimed_adapter_id IS NULL AND claimed_at IS NULL "
        "AND consumed_at IS NULL) OR "
        "(status = 'rejected' AND rejected_at IS NOT NULL AND committed_at IS NULL "
        "AND abandoned_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL "
        "AND claimed_adapter_id IS NULL AND claimed_at IS NULL AND consumed_at IS NULL "
        "AND error_code IN ('adapter_config_invalid','adapter_config_unsupported',"
        "'adapter_header_invalid','adapter_header_too_large','adapter_file_too_large',"
        "'adapter_tensor_set_invalid','adapter_tensor_shape_invalid','adapter_tensor_dtype_invalid',"
        "'adapter_tensor_offsets_invalid','adapter_tensor_size_invalid','adapter_input_invalid',"
        "'adapter_input_unsafe')) OR "
        "(status = 'abandoned' AND abandoned_at IS NOT NULL AND rejected_at IS NULL "
        "AND committed_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL "
        "AND claimed_adapter_id IS NULL AND claimed_at IS NULL AND consumed_at IS NULL "
        "AND error_code IN ('adapter_source_changed','adapter_source_publication_failed',"
        "'adapter_source_authority_changed','department_unavailable','requester_unauthorized',"
        "'database_unavailable')) OR "
        "(status = 'claimed' AND "
        "authoritative_attempt_id IS NOT NULL AND "
        "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
        "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
        "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
        "tensor_count = 392 AND tensor_element_count = 10092544 AND "
        "tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
        "rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND "
        "error_code IS NULL AND claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL "
        "AND consumed_at IS NULL) OR "
        "(status = 'consumed' AND authoritative_attempt_id IS NOT NULL "
        "AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
        "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
        "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
        "tensor_count = 392 AND tensor_element_count = 10092544 "
        "AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL "
        "AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL "
        "AND error_code IS NULL AND claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL "
        "AND consumed_at IS NOT NULL) OR "
        "(status = 'purge_pending' AND authoritative_attempt_id IS NOT NULL "
        "AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
        "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
        "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
        "tensor_count = 392 AND tensor_element_count = 10092544 AND "
        "tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
        "purged_at IS NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND "
        "error_code IS NULL AND "
        "((claimed_adapter_id IS NULL AND claimed_at IS NULL AND consumed_at IS NULL) OR "
        "(claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL))) OR "
        "(status = 'purged' AND authoritative_attempt_id IS NOT NULL "
        "AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
        "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
        "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
        "tensor_count = 392 AND tensor_element_count = 10092544 AND "
        "tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
        "purged_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND "
        "error_code IS NULL AND "
        "((claimed_adapter_id IS NULL AND claimed_at IS NULL AND consumed_at IS NULL) OR "
        "(claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL)))",
        name="ck_adapter_import_source_lifecycle",
    )


_IMPORT_MANIFEST_KEYS = frozenset(
    {
        "source_contract_version",
        "intake_contract_version",
        "config_contract_version",
        "tensor_contract_version",
        "department_id",
        "source_bundle_id",
        "import_attempt_id",
        "publication_attempt_id",
        "attempt_number",
        "imported_by_user_id",
        "code_revision",
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "peft_version",
        "safetensors_format",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
        "files",
    }
)


def _canonical_manifest_bytes(value: object) -> bytes | None:
    """Reproduce the frozen Phase 12.1B manifest encoding without app imports."""

    if not isinstance(value, dict) or set(value) != _IMPORT_MANIFEST_KEYS:
        return None
    if (
        value.get("source_contract_version") != "phase12-adapter-source-v1"
        or value.get("intake_contract_version") != "phase12-adapter-intake-v1"
        or value.get("config_contract_version") != "phase12-adapter-config-v1"
        or value.get("tensor_contract_version") != "phase12-adapter-tensors-v1"
        or value.get("base_model_id") != "Qwen/Qwen3-0.6B"
        or value.get("base_model_revision") != "c1899de289a04d12100db370d81485cdf75e47ca"
        or value.get("base_model_license") != "Apache-2.0"
        or value.get("peft_version") != "0.18.1"
        or value.get("safetensors_format") != "0.7.0"
        or value.get("tensor_dtype") not in {"F16", "BF16", "F32"}
        or value.get("tensor_count") != 392
        or value.get("tensor_element_count") != 10092544
        or value.get("tensor_payload_byte_size")
        != {"F16": 20185088, "BF16": 20185088, "F32": 40370176}.get(value.get("tensor_dtype"))
        or type(value.get("attempt_number")) is not int
        or value.get("attempt_number", 0) <= 0
    ):
        return None
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != {
        "adapter_config.json",
        "adapter_model.safetensors",
    }:
        return None
    for descriptor in files.values():
        if not isinstance(descriptor, dict) or set(descriptor) != {"sha256", "byte_size"}:
            return None
        digest = descriptor.get("sha256")
        size = descriptor.get("byte_size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
            or type(size) is not int
            or size <= 0
        ):
            return None
    try:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        return None


def _backfill_intake_manifest_sizes(bind: sa.Connection) -> None:
    """Backfill 0010 committed rows from their exact closed attempt authority.

    This deliberately runs before the new lifecycle checks are installed.  A
    malformed or incomplete historical authority aborts the migration instead
    of inventing a size or silently weakening the source contract.
    """

    result = bind.execute(
        sa.text(
            "SELECT s.id AS source_id, s.department_id, s.authoritative_attempt_id, "
            "s.code_revision, s.imported_by_user_id, s.intake_manifest_sha256, "
            "a.id AS attempt_id, a.department_id AS attempt_department_id, "
            "a.source_bundle_id AS attempt_source_bundle_id, "
            "a.publication_attempt_id AS attempt_publication_attempt_id, "
            "a.attempt_number AS attempt_number_value, a.code_revision AS attempt_code_revision, "
            "a.status AS attempt_status, a.ownership_manifest "
            "FROM adapter_import_sources AS s "
            "LEFT JOIN adapter_import_attempts AS a "
            "ON a.id = s.authoritative_attempt_id "
            "WHERE s.status IN ('committed','claimed','consumed','purge_pending','purged')"
        )
    )
    # Alembic's offline ``MockConnection`` cannot inspect existing rows.  The
    # authoritative backfill is therefore performed by the online migration;
    # offline SQL generation still emits the schema changes without crashing.
    if result is None:
        return
    rows = result.mappings()
    for row in rows:
        source_id = row["source_id"]
        manifest = row["ownership_manifest"]
        if (
            row["authoritative_attempt_id"] is None
            or row["attempt_id"] != row["authoritative_attempt_id"]
            or row["attempt_department_id"] != row["department_id"]
            or row["attempt_source_bundle_id"] != source_id
            or row["attempt_code_revision"] != row["code_revision"]
            or row["attempt_status"] != "committed"
            or not isinstance(manifest, dict)
        ):
            raise RuntimeError(
                f"0011 cannot backfill intake_manifest_byte_size for source {source_id}: "
                "authoritative import attempt is incomplete"
            )
        if (
            manifest.get("department_id") != str(row["department_id"])
            or manifest.get("source_bundle_id") != str(source_id)
            or manifest.get("import_attempt_id") != str(row["authoritative_attempt_id"])
            or manifest.get("publication_attempt_id") != str(row["attempt_publication_attempt_id"])
            or manifest.get("attempt_number") != row["attempt_number_value"]
            or manifest.get("imported_by_user_id") != str(row["imported_by_user_id"])
            or manifest.get("code_revision") != row["code_revision"]
        ):
            raise RuntimeError(
                f"0011 cannot backfill intake_manifest_byte_size for source {source_id}: "
                "manifest authority does not match source metadata"
            )
        encoded = _canonical_manifest_bytes(manifest)
        if encoded is None:
            raise RuntimeError(
                f"0011 cannot backfill intake_manifest_byte_size for source {source_id}: "
                "ownership manifest is malformed"
            )
        digest = hashlib.sha256(encoded).hexdigest()
        byte_size = len(encoded)
        if (
            byte_size <= 0
            or not isinstance(row["intake_manifest_sha256"], str)
            or row["intake_manifest_sha256"] != digest
        ):
            raise RuntimeError(
                f"0011 cannot backfill intake_manifest_byte_size for source {source_id}: "
                "manifest digest authority is missing or inconsistent"
            )
        bind.execute(
            sa.text(
                "UPDATE adapter_import_sources "
                "SET intake_manifest_byte_size = :byte_size "
                "WHERE id = :source_id"
            ),
            {"byte_size": byte_size, "source_id": source_id},
        )


def upgrade() -> None:
    op.drop_constraint(
        "ck_adapter_import_source_lifecycle", "adapter_import_sources", type_="check"
    )
    op.add_column("adapter_import_sources", sa.Column("claimed_adapter_id", sa.Uuid()))
    op.add_column("adapter_import_sources", sa.Column("claimed_at", sa.DateTime(timezone=True)))
    op.add_column("adapter_import_sources", sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.add_column("adapter_import_sources", sa.Column("intake_manifest_byte_size", sa.BigInteger()))
    _backfill_intake_manifest_sizes(op.get_bind())
    op.create_check_constraint(
        "ck_adapter_import_source_lifecycle",
        "adapter_import_sources",
        _source_lifecycle().sqltext,
    )
    op.create_check_constraint(
        "ck_adapter_import_source_manifest_size",
        "adapter_import_sources",
        "((status IN ('committed','claimed','consumed','purge_pending','purged') AND intake_manifest_byte_size > 0) OR "
        "(status IN ('staging','rejected','abandoned') AND intake_manifest_byte_size IS NULL))",
    )

    op.create_unique_constraint(
        "uq_adapter_import_attempt_exact",
        "adapter_import_attempts",
        ["id", "department_id", "source_bundle_id", "publication_attempt_id", "attempt_number"],
    )
    op.create_unique_constraint(
        "uq_training_job_attempt_exact",
        "training_job_attempts",
        ["training_job_id", "department_id", "publication_attempt_id", "attempt_number"],
    )
    op.create_unique_constraint(
        "uq_sft_build_attempt_exact",
        "sft_dataset_build_attempts",
        ["build_id", "department_id", "publication_attempt_id", "attempt_number"],
    )

    op.create_table(
        "adapters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_scope_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("source_authoritative_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("source_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("source_attempt_number", sa.Integer(), nullable=False),
        sa.Column("source_attempt_version", sa.Integer(), nullable=False),
        sa.Column("source_imported_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_code_revision", sa.String(40), nullable=False),
        sa.Column("source_contract_version", sa.String(100), nullable=False),
        sa.Column("intake_contract_version", sa.String(100), nullable=False),
        sa.Column("config_contract_version", sa.String(100), nullable=False),
        sa.Column("tensor_contract_version", sa.String(100), nullable=False),
        sa.Column("source_intake_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_intake_manifest_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("source_adapter_config_sha256", sa.String(64), nullable=False),
        sa.Column("source_adapter_config_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("source_adapter_model_sha256", sa.String(64), nullable=False),
        sa.Column("source_adapter_model_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("peft_version", sa.String(32), nullable=False),
        sa.Column("safetensors_format", sa.String(32), nullable=False),
        sa.Column("tensor_dtype", sa.String(8), nullable=False),
        sa.Column("tensor_count", sa.Integer(), nullable=False),
        sa.Column("tensor_element_count", sa.BigInteger(), nullable=False),
        sa.Column("tensor_payload_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_version", sa.Integer(), nullable=False),
        sa.Column("training_job_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_attempt_number", sa.Integer(), nullable=False),
        sa.Column("training_job_attempt_version", sa.Integer(), nullable=False),
        sa.Column("training_job_code_revision", sa.String(40), nullable=False),
        sa.Column("training_job_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_manifest_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_execution_scope_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_config_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_config_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_dataset_info_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_dataset_info_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_train_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_train_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_validation_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_validation_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_profile_id", sa.String(80), nullable=False),
        sa.Column("training_job_artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("training_job_manifest_contract_version", sa.String(100), nullable=False),
        sa.Column("training_configuration_contract_version", sa.String(100), nullable=False),
        sa.Column("training_dataset_info_contract_version", sa.String(100), nullable=False),
        sa.Column("training_execution_profile_contract_version", sa.String(100), nullable=False),
        sa.Column("llamafactory_version", sa.String(32), nullable=False),
        sa.Column("dataset_build_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_build_version", sa.Integer(), nullable=False),
        sa.Column("dataset_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_publication_attempt_number", sa.Integer(), nullable=False),
        sa.Column("dataset_attempt_version", sa.Integer(), nullable=False),
        sa.Column("dataset_code_revision", sa.String(40), nullable=False),
        sa.Column("dataset_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_example_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_normalization_version", sa.String(100), nullable=False),
        sa.Column("dataset_split_version", sa.String(100), nullable=False),
        sa.Column("dataset_train_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_train_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("dataset_validation_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_validation_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("dataset_provenance_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_provenance_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("dataset_train_example_count", sa.Integer(), nullable=False),
        sa.Column("dataset_validation_example_count", sa.Integer(), nullable=False),
        sa.Column("dataset_source_example_count", sa.Integer(), nullable=False),
        sa.Column("dataset_source_group_count", sa.Integer(), nullable=False),
        sa.Column("dataset_source_reference_count", sa.Integer(), nullable=False),
        sa.Column("dataset_rights_attested", sa.Boolean(), nullable=False),
        sa.Column("evaluation_contamination_reviewed", sa.Boolean(), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("base_model_license", sa.String(40), nullable=False),
        sa.Column("artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("registry_manifest_contract_version", sa.String(100), nullable=False),
        sa.Column("declared_external_training_association", sa.Boolean(), nullable=False),
        sa.Column("verified_governance_lineage", sa.Boolean(), nullable=False),
        sa.Column("verified_artifact_compatibility", sa.Boolean(), nullable=False),
        sa.Column("training_provenance_verified", sa.Boolean(), nullable=False),
        sa.Column("registry_manifest_sha256", sa.String(64)),
        sa.Column("registry_adapter_config_sha256", sa.String(64)),
        sa.Column("registry_adapter_config_byte_size", sa.BigInteger()),
        sa.Column("registry_adapter_model_sha256", sa.String(64)),
        sa.Column("registry_adapter_model_byte_size", sa.BigInteger()),
        sa.CheckConstraint(
            "status IN ('queued','running','validated','validation_failed','failed','purge_pending','purged')",
            name="ck_adapter_status",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_quoted(_ERRORS)})",
            name="ck_adapter_error_code",
        ),
        sa.CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$' AND source_code_revision ~ '^[0-9a-f]{40}$' "
            "AND training_job_code_revision ~ '^[0-9a-f]{40}$' AND dataset_code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_adapter_code_revisions",
        ),
        sa.CheckConstraint(
            "source_contract_version = 'phase12-adapter-source-v1' AND intake_contract_version = 'phase12-adapter-intake-v1' "
            "AND config_contract_version = 'phase12-adapter-config-v1' AND tensor_contract_version = 'phase12-adapter-tensors-v1'",
            name="ck_adapter_source_contracts",
        ),
        sa.CheckConstraint(
            "artifact_contract_version = 'phase12-adapter-artifact-v1' AND registry_manifest_contract_version = 'phase12-adapter-manifest-v1' "
            "AND declared_external_training_association IS TRUE AND training_provenance_verified IS FALSE",
            name="ck_adapter_registry_contracts",
        ),
        sa.CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca' AND "
            "base_model_license = 'Apache-2.0' AND peft_version = '0.18.1' AND "
            "safetensors_format = '0.7.0' AND "
            "training_job_artifact_contract_version = 'phase11-training-job-v1' AND "
            "training_job_manifest_contract_version = 'phase11-training-job-manifest-v1' AND "
            "training_configuration_contract_version = 'phase11-training-config-v1' AND "
            "training_dataset_info_contract_version = 'phase11-dataset-info-v1' AND "
            "training_execution_profile_contract_version = 'phase11-execution-profile-v1' AND "
            "llamafactory_version = '0.9.5' AND "
            "training_job_profile_id IN ('phase11-qwen3-0.6b-lora-v1','phase11-qwen3-0.6b-qlora-nf4-v1') AND "
            "dataset_artifact_contract_version = 'phase10-sft-dataset-v1' AND "
            "dataset_example_contract_version = 'phase10-sft-example-v1' AND "
            "dataset_normalization_version = 'phase10-sft-normalization-v1' AND "
            "dataset_split_version = 'phase10-sft-group-split-v1' AND "
            "dataset_rights_attested IS TRUE AND evaluation_contamination_reviewed IS TRUE",
            name="ck_adapter_upstream_contracts",
        ),
        sa.CheckConstraint(
            "source_intake_manifest_byte_size > 0 AND source_adapter_config_byte_size > 0 AND "
            "source_adapter_model_byte_size > 0 AND training_job_manifest_byte_size > 0 AND "
            "training_job_config_byte_size > 0 AND training_job_dataset_info_byte_size > 0 AND "
            "training_job_train_byte_size > 0 AND training_job_validation_byte_size > 0 AND "
            "dataset_train_byte_size > 0 AND dataset_validation_byte_size > 0 AND "
            "dataset_provenance_byte_size > 0 AND dataset_train_example_count > 0 AND "
            "dataset_validation_example_count > 0 AND dataset_source_example_count >= 2 AND "
            "dataset_source_group_count >= 2 AND "
            "dataset_source_reference_count >= dataset_source_example_count AND "
            "tensor_count = 392 AND "
            "tensor_element_count = 10092544 AND "
            "((tensor_dtype IN ('F16','BF16') AND tensor_payload_byte_size = 20185088) OR "
            "(tensor_dtype = 'F32' AND tensor_payload_byte_size = 40370176))",
            name="ck_adapter_exact_sizes",
        ),
        sa.CheckConstraint(
            "tensor_dtype IN ('F16','BF16','F32') AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0",
            name="ck_adapter_tensor_contract",
        ),
        sa.CheckConstraint(
            "(registry_adapter_model_sha256 IS NULL AND registry_adapter_model_byte_size IS NULL) OR "
            "(source_adapter_model_sha256 = registry_adapter_model_sha256 AND "
            "source_adapter_model_byte_size = registry_adapter_model_byte_size)",
            name="ck_adapter_model_digest_match",
        ),
        sa.CheckConstraint(
            "source_adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND "
            "source_adapter_model_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_config_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_dataset_info_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_train_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_validation_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_train_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_validation_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_provenance_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_adapter_source_hashes",
        ),
        sa.CheckConstraint(
            "(registry_manifest_sha256 IS NULL OR registry_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(registry_adapter_config_sha256 IS NULL OR registry_adapter_config_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(registry_adapter_model_sha256 IS NULL OR registry_adapter_model_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_adapter_registry_hashes",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 AND version > 0 AND source_version > 0 AND source_attempt_version > 0 "
            "AND training_job_version > 0 AND training_job_attempt_version > 0 "
            "AND dataset_build_version > 0 AND dataset_attempt_version > 0",
            name="ck_adapter_versions",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND worker_id IS NULL AND claim_token IS NULL AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND validated_at IS NULL AND purged_at IS NULL AND error_code IS NULL "
            "AND verified_governance_lineage IS FALSE "
            "AND verified_artifact_compatibility IS FALSE AND registry_manifest_sha256 IS NULL "
            "AND registry_adapter_config_sha256 IS NULL AND registry_adapter_config_byte_size IS NULL "
            "AND registry_adapter_model_sha256 IS NULL AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND validated_at IS NULL AND purged_at IS NULL "
            "AND error_code IS NULL "
            "AND verified_governance_lineage IS FALSE AND verified_artifact_compatibility IS FALSE "
            "AND registry_manifest_sha256 IS NULL AND registry_adapter_config_sha256 IS NULL "
            "AND registry_adapter_config_byte_size IS NULL AND registry_adapter_model_sha256 IS NULL "
            "AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'validated' AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND validated_at IS NOT NULL AND finished_at IS NOT NULL AND purged_at IS NULL "
            "AND error_code IS NULL "
            "AND verified_governance_lineage IS TRUE AND verified_artifact_compatibility IS TRUE "
            "AND registry_manifest_sha256 IS NOT NULL AND registry_adapter_config_sha256 IS NOT NULL "
            "AND registry_adapter_config_byte_size > 0 AND registry_adapter_model_sha256 IS NOT NULL "
            "AND registry_adapter_model_byte_size > 0) OR "
            "(status IN ('validation_failed','failed') AND worker_id IS NULL AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND validated_at IS NULL AND finished_at IS NOT NULL "
            "AND purged_at IS NULL AND error_code IS NOT NULL AND verified_governance_lineage IS FALSE "
            "AND verified_artifact_compatibility IS FALSE AND registry_manifest_sha256 IS NULL "
            "AND registry_adapter_config_sha256 IS NULL AND registry_adapter_config_byte_size IS NULL "
            "AND registry_adapter_model_sha256 IS NULL AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'purge_pending' AND worker_id IS NULL AND claim_token IS NULL AND "
            "lease_expires_at IS NULL AND validated_at IS NOT NULL AND finished_at IS NOT NULL AND "
            "purged_at IS NULL AND error_code IS NULL AND verified_governance_lineage IS TRUE AND "
            "verified_artifact_compatibility IS TRUE AND registry_manifest_sha256 IS NOT NULL AND "
            "registry_adapter_config_sha256 IS NOT NULL AND registry_adapter_config_byte_size > 0 AND "
            "registry_adapter_model_sha256 IS NOT NULL AND registry_adapter_model_byte_size > 0) OR "
            "(status = 'purged' AND worker_id IS NULL AND claim_token IS NULL AND "
            "lease_expires_at IS NULL AND validated_at IS NOT NULL AND finished_at IS NOT NULL AND "
            "purged_at IS NOT NULL AND error_code IS NULL AND verified_governance_lineage IS TRUE AND "
            "verified_artifact_compatibility IS TRUE AND registry_manifest_sha256 IS NOT NULL AND "
            "registry_adapter_config_sha256 IS NOT NULL AND registry_adapter_config_byte_size > 0 AND "
            "registry_adapter_model_sha256 IS NOT NULL AND registry_adapter_model_byte_size > 0)",
            name="ck_adapter_lifecycle",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_source_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_adapter_training_job_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_build_id", "department_id"],
            ["sft_dataset_builds.id", "sft_dataset_builds.department_id"],
            name="fk_adapter_dataset_build_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_authoritative_attempt_id",
                "department_id",
                "source_bundle_id",
                "source_publication_attempt_id",
                "source_attempt_number",
            ],
            [
                "adapter_import_attempts.id",
                "adapter_import_attempts.department_id",
                "adapter_import_attempts.source_bundle_id",
                "adapter_import_attempts.publication_attempt_id",
                "adapter_import_attempts.attempt_number",
            ],
            name="fk_adapter_source_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "training_job_id",
                "department_id",
                "training_job_publication_attempt_id",
                "training_job_attempt_number",
            ],
            [
                "training_job_attempts.training_job_id",
                "training_job_attempts.department_id",
                "training_job_attempts.publication_attempt_id",
                "training_job_attempts.attempt_number",
            ],
            name="fk_adapter_training_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "dataset_build_id",
                "department_id",
                "dataset_publication_attempt_id",
                "dataset_publication_attempt_number",
            ],
            [
                "sft_dataset_build_attempts.build_id",
                "sft_dataset_build_attempts.department_id",
                "sft_dataset_build_attempts.publication_attempt_id",
                "sft_dataset_build_attempts.attempt_number",
            ],
            name="fk_adapter_dataset_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_department"),
        sa.UniqueConstraint("source_bundle_id", "department_id", name="uq_adapter_source_scope"),
        sa.UniqueConstraint(
            "id", "department_id", "source_bundle_id", name="uq_adapter_source_claim_scope"
        ),
        sa.UniqueConstraint(
            "id",
            "department_id",
            "training_job_id",
            "dataset_build_id",
            name="uq_adapter_governance_scope",
        ),
        sa.UniqueConstraint("publication_attempt_id", name="uq_adapter_publication_attempt"),
    )
    op.create_index(
        "ix_adapter_department_status_created",
        "adapters",
        ["department_id", "status", "created_at"],
    )
    op.create_index("ix_adapter_claim", "adapters", ["status", "lease_expires_at", "created_at"])

    op.create_table(
        "adapter_registry_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_scope_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("ownership_manifest", sa.JSON()),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("staged_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cleanup_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('registered','running','staged','published','succeeded','validation_failed','failed','reclaimed')",
            name="ck_adapter_registry_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 AND version > 0", name="ck_adapter_registry_attempt_versions"
        ),
        sa.CheckConstraint(
            "ownership_manifest IS NULL OR json_typeof(ownership_manifest) = 'object'",
            name="ck_adapter_registry_attempt_manifest_object",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_quoted(_ERRORS)})",
            name="ck_adapter_registry_attempt_error_code",
        ),
        sa.CheckConstraint(
            "(status IN ('staged','published','succeeded') AND ownership_manifest IS NOT NULL) "
            "OR status NOT IN ('staged','published','succeeded')",
            name="ck_adapter_registry_attempt_manifest_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND staged_at IS NOT NULL AND published_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('validation_failed','failed','reclaimed') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL) OR "
            "status NOT IN ('succeeded','validation_failed','failed','reclaimed')",
            name="ck_adapter_registry_attempt_lifecycle",
        ),
        sa.CheckConstraint(
            "cleanup_confirmed_at IS NULL AND "
            "((status = 'registered' AND worker_id IS NULL AND claimed_at IS NULL AND "
            "staged_at IS NULL AND published_at IS NULL AND finished_at IS NULL AND "
            "ownership_manifest IS NULL AND error_code IS NULL) OR "
            "(status = 'running' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL AND "
            "staged_at IS NULL AND published_at IS NULL AND finished_at IS NULL AND "
            "ownership_manifest IS NULL AND error_code IS NULL) OR "
            "(status = 'staged' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL AND "
            "staged_at IS NOT NULL AND published_at IS NULL AND finished_at IS NULL AND "
            "ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'published' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL AND "
            "staged_at IS NOT NULL AND published_at IS NOT NULL AND finished_at IS NULL AND "
            "ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND staged_at IS NOT NULL AND published_at IS NOT NULL AND "
            "finished_at IS NOT NULL AND ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('validation_failed','failed','reclaimed') AND finished_at IS NOT NULL AND "
            "error_code IS NOT NULL))",
            name="ck_adapter_registry_attempt_exact_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_registry_attempt_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "department_id", "adapter_id", name="uq_adapter_registry_attempt_scope"
        ),
        sa.UniqueConstraint(
            "adapter_id", "attempt_number", name="uq_adapter_registry_attempt_number"
        ),
        sa.UniqueConstraint(
            "publication_attempt_id", name="uq_adapter_registry_attempt_publication"
        ),
    )
    op.create_index(
        "ix_adapter_registry_attempt_department_status",
        "adapter_registry_attempts",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "uq_adapter_registry_attempt_active",
        "adapter_registry_attempts",
        ["adapter_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('registered','running','staged','published')"),
    )

    op.create_table(
        "adapter_upstream_dependencies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_build_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('active','released')", name="ck_adapter_dependency_status"),
        sa.CheckConstraint("version > 0", name="ck_adapter_dependency_version"),
        sa.CheckConstraint(
            "(status = 'active' AND released_at IS NULL) OR (status = 'released' AND released_at IS NOT NULL)",
            name="ck_adapter_dependency_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_dependency_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_adapter_dependency_training_job_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_build_id", "department_id"],
            ["sft_dataset_builds.id", "sft_dataset_builds.department_id"],
            name="fk_adapter_dependency_dataset_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id", "training_job_id", "dataset_build_id"],
            [
                "adapters.id",
                "adapters.department_id",
                "adapters.training_job_id",
                "adapters.dataset_build_id",
            ],
            name="fk_adapter_dependency_adapter_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adapter_id", name="uq_adapter_dependency_adapter"),
    )
    op.create_foreign_key(
        "fk_adapter_import_source_claimed_adapter_scope",
        "adapter_import_sources",
        "adapters",
        ["claimed_adapter_id", "department_id", "id"],
        ["id", "department_id", "source_bundle_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_adapter_import_source_claimed_adapter_scope",
        "adapter_import_sources",
        type_="foreignkey",
    )
    op.drop_table("adapter_upstream_dependencies")
    op.drop_index("uq_adapter_registry_attempt_active", table_name="adapter_registry_attempts")
    op.drop_index(
        "ix_adapter_registry_attempt_department_status", table_name="adapter_registry_attempts"
    )
    op.drop_table("adapter_registry_attempts")
    op.drop_index("ix_adapter_claim", table_name="adapters")
    op.drop_index("ix_adapter_department_status_created", table_name="adapters")
    op.drop_table("adapters")
    op.drop_constraint("uq_sft_build_attempt_exact", "sft_dataset_build_attempts", type_="unique")
    op.drop_constraint("uq_training_job_attempt_exact", "training_job_attempts", type_="unique")
    op.drop_constraint("uq_adapter_import_attempt_exact", "adapter_import_attempts", type_="unique")
    op.drop_constraint(
        "ck_adapter_import_source_lifecycle", "adapter_import_sources", type_="check"
    )
    op.drop_constraint(
        "ck_adapter_import_source_manifest_size", "adapter_import_sources", type_="check"
    )
    op.drop_column("adapter_import_sources", "consumed_at")
    op.drop_column("adapter_import_sources", "claimed_at")
    op.drop_column("adapter_import_sources", "claimed_adapter_id")
    op.drop_column("adapter_import_sources", "intake_manifest_byte_size")
    op.create_check_constraint(
        "ck_adapter_import_source_lifecycle",
        "adapter_import_sources",
        "(status = 'staging' AND authoritative_attempt_id IS NULL AND committed_at IS NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND error_code IS NULL) OR "
        "(status = 'committed' AND authoritative_attempt_id IS NOT NULL AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND error_code IS NULL) OR "
        "(status = 'rejected' AND rejected_at IS NOT NULL AND committed_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL AND error_code IN ('adapter_config_invalid','adapter_config_unsupported','adapter_header_invalid','adapter_header_too_large','adapter_file_too_large','adapter_tensor_set_invalid','adapter_tensor_shape_invalid','adapter_tensor_dtype_invalid','adapter_tensor_offsets_invalid','adapter_tensor_size_invalid','adapter_input_invalid','adapter_input_unsafe')) OR "
        "(status = 'abandoned' AND abandoned_at IS NOT NULL AND rejected_at IS NULL AND committed_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL AND error_code IN ('adapter_source_changed','adapter_source_publication_failed','adapter_source_authority_changed','department_unavailable','requester_unauthorized','database_unavailable')) OR "
        "(status IN ('claimed','consumed','purge_pending') AND authoritative_attempt_id IS NOT NULL AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND error_code IS NULL) OR "
        "(status = 'purged' AND authoritative_attempt_id IS NOT NULL AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND purged_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND error_code IS NULL)",
    )

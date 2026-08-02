# ruff: noqa: E501
"""Add the immutable Phase 12.1C adapter registry authority.

This migration is intentionally self-contained.  It freezes the registry
schema and its contracts in literals so historical upgrades do not import the
application model or any runtime code.
"""

from __future__ import annotations

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


def upgrade() -> None:
    op.drop_constraint(
        "ck_adapter_import_source_lifecycle", "adapter_import_sources", type_="check"
    )
    op.add_column("adapter_import_sources", sa.Column("claimed_adapter_id", sa.Uuid()))
    op.add_column("adapter_import_sources", sa.Column("claimed_at", sa.DateTime(timezone=True)))
    op.add_column("adapter_import_sources", sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_adapter_import_source_lifecycle",
        "adapter_import_sources",
        _source_lifecycle().sqltext,
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
        sa.Column("source_imported_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_code_revision", sa.String(40), nullable=False),
        sa.Column("source_contract_version", sa.String(100), nullable=False),
        sa.Column("intake_contract_version", sa.String(100), nullable=False),
        sa.Column("config_contract_version", sa.String(100), nullable=False),
        sa.Column("tensor_contract_version", sa.String(100), nullable=False),
        sa.Column("source_intake_manifest_sha256", sa.String(64), nullable=False),
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
        sa.Column("training_job_code_revision", sa.String(40), nullable=False),
        sa.Column("training_job_manifest_sha256", sa.String(64), nullable=False),
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
            "tensor_dtype IN ('F16','BF16','F32') AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0",
            name="ck_adapter_tensor_contract",
        ),
        sa.CheckConstraint(
            "source_adapter_model_sha256 = registry_adapter_model_sha256 AND source_adapter_model_byte_size = registry_adapter_model_byte_size",
            name="ck_adapter_model_digest_match",
        ),
        sa.CheckConstraint(
            "source_adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND source_adapter_model_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_adapter_source_hashes",
        ),
        sa.CheckConstraint(
            "registry_manifest_sha256 IS NULL OR registry_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_adapter_registry_hashes",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 AND version > 0 AND source_version > 0 AND training_job_version > 0 AND dataset_build_version > 0",
            name="ck_adapter_versions",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND worker_id IS NULL AND claim_token IS NULL AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND validated_at IS NULL AND error_code IS NULL AND verified_governance_lineage IS FALSE "
            "AND verified_artifact_compatibility IS FALSE AND registry_manifest_sha256 IS NULL "
            "AND registry_adapter_config_sha256 IS NULL AND registry_adapter_config_byte_size IS NULL "
            "AND registry_adapter_model_sha256 IS NULL AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND validated_at IS NULL AND error_code IS NULL "
            "AND verified_governance_lineage IS FALSE AND verified_artifact_compatibility IS FALSE "
            "AND registry_manifest_sha256 IS NULL AND registry_adapter_config_sha256 IS NULL "
            "AND registry_adapter_config_byte_size IS NULL AND registry_adapter_model_sha256 IS NULL "
            "AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'validated' AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND validated_at IS NOT NULL AND finished_at IS NOT NULL AND error_code IS NULL "
            "AND verified_governance_lineage IS TRUE AND verified_artifact_compatibility IS TRUE "
            "AND registry_manifest_sha256 IS NOT NULL AND registry_adapter_config_sha256 IS NOT NULL "
            "AND registry_adapter_config_byte_size > 0 AND registry_adapter_model_sha256 IS NOT NULL "
            "AND registry_adapter_model_byte_size > 0) OR "
            "(status IN ('validation_failed','failed') AND worker_id IS NULL AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND validated_at IS NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND verified_governance_lineage IS FALSE "
            "AND verified_artifact_compatibility IS FALSE) OR status IN ('purge_pending','purged')",
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_department"),
        sa.UniqueConstraint("source_bundle_id", "department_id", name="uq_adapter_source_scope"),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adapter_id", name="uq_adapter_dependency_adapter"),
    )
    op.create_foreign_key(
        "fk_adapter_import_source_claimed_adapter_scope",
        "adapter_import_sources",
        "adapters",
        ["claimed_adapter_id", "department_id"],
        ["id", "department_id"],
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
    op.drop_constraint(
        "ck_adapter_import_source_lifecycle", "adapter_import_sources", type_="check"
    )
    op.drop_column("adapter_import_sources", "consumed_at")
    op.drop_column("adapter_import_sources", "claimed_at")
    op.drop_column("adapter_import_sources", "claimed_adapter_id")
    op.create_check_constraint(
        "ck_adapter_import_source_lifecycle",
        "adapter_import_sources",
        "(status = 'staging' AND authoritative_attempt_id IS NULL AND committed_at IS NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND error_code IS NULL) OR "
        "(status = 'committed' AND authoritative_attempt_id IS NOT NULL AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND error_code IS NULL) OR "
        "(status = 'rejected' AND rejected_at IS NOT NULL AND committed_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL) OR "
        "(status = 'abandoned' AND abandoned_at IS NOT NULL AND rejected_at IS NULL AND committed_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL) OR "
        "(status IN ('claimed','consumed','purge_pending') AND authoritative_attempt_id IS NOT NULL AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND error_code IS NULL) OR "
        "(status = 'purged' AND authoritative_attempt_id IS NOT NULL AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND tensor_count = 392 AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND purged_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND error_code IS NULL)",
    )

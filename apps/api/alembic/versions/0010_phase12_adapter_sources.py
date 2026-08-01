"""Add metadata-only Phase 12.1B immutable adapter source intake state.

This revision deliberately creates source and import-attempt authority only.  It
does not create an adapter registry, training-job binding, or purge surface.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

_ADAPTER_CONFIG_CONTRACT_VERSION = "phase12-adapter-config-v1"
_ADAPTER_INTAKE_CONTRACT_VERSION = "phase12-adapter-intake-v1"
_ADAPTER_SOURCE_CONTRACT_VERSION = "phase12-adapter-source-v1"
_ADAPTER_TENSOR_CONTRACT_VERSION = "phase12-adapter-tensors-v1"
_BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
_BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
_BASE_MODEL_LICENSE = "Apache-2.0"
_PEFT_FORMAT_REFERENCE_VERSION = "0.18.1"
_SAFETENSORS_FORMAT_REFERENCE_VERSION = "0.7.0"
_EXPECTED_TENSOR_COUNT = 392
_EXPECTED_TENSOR_ELEMENTS = 10_092_544
_EXPECTED_TENSOR_BYTES = {
    "F16": 20_185_088,
    "BF16": 20_185_088,
    "F32": 40_370_176,
}

revision = "0010_phase12_adapter_sources"
down_revision = "0009_phase11_training_jobs"
branch_labels = None
depends_on = None

_SOURCE_ERRORS = (
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
    "adapter_input_invalid",
    "adapter_input_unsafe",
    "adapter_source_changed",
    "adapter_source_publication_failed",
    "adapter_source_authority_changed",
    "department_unavailable",
    "requester_unauthorized",
    "database_unavailable",
)
_VALIDATION_ERRORS = _SOURCE_ERRORS[:12]
_OPERATIONAL_ERRORS = _SOURCE_ERRORS[12:]


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join("'" + value + "'" for value in values)


def upgrade() -> None:
    op.create_table(
        "adapter_import_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("imported_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("authoritative_attempt_id", sa.Uuid()),
        sa.Column("source_contract_version", sa.String(100), nullable=False),
        sa.Column("intake_contract_version", sa.String(100), nullable=False),
        sa.Column("config_contract_version", sa.String(100), nullable=False),
        sa.Column("tensor_contract_version", sa.String(100), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("base_model_license", sa.String(40), nullable=False),
        sa.Column("peft_version", sa.String(32), nullable=False),
        sa.Column("safetensors_format", sa.String(32), nullable=False),
        sa.Column("adapter_config_sha256", sa.String(64)),
        sa.Column("adapter_config_byte_size", sa.BigInteger()),
        sa.Column("adapter_model_sha256", sa.String(64)),
        sa.Column("adapter_model_byte_size", sa.BigInteger()),
        sa.Column("intake_manifest_sha256", sa.String(64)),
        sa.Column("tensor_dtype", sa.String(8)),
        sa.Column("tensor_count", sa.Integer()),
        sa.Column("tensor_element_count", sa.BigInteger()),
        sa.Column("tensor_payload_byte_size", sa.BigInteger()),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("abandoned_at", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('staging','committed','claimed','consumed','rejected','abandoned',"
            "'purge_pending','purged')",
            name="ck_adapter_import_source_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_adapter_import_source_version"),
        sa.CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_adapter_import_source_code_revision",
        ),
        sa.CheckConstraint(
            "(adapter_config_sha256 IS NULL OR adapter_config_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(adapter_model_sha256 IS NULL OR adapter_model_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(intake_manifest_sha256 IS NULL OR intake_manifest_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_adapter_import_source_hashes",
        ),
        sa.CheckConstraint(
            "(adapter_config_byte_size IS NULL OR adapter_config_byte_size > 0) AND "
            "(adapter_model_byte_size IS NULL OR adapter_model_byte_size > 0) AND "
            "(tensor_payload_byte_size IS NULL OR tensor_payload_byte_size > 0)",
            name="ck_adapter_import_source_sizes",
        ),
        sa.CheckConstraint(
            f"tensor_count IS NULL OR tensor_count = {_EXPECTED_TENSOR_COUNT}",
            name="ck_adapter_import_source_tensor_count",
        ),
        sa.CheckConstraint(
            f"tensor_element_count IS NULL OR tensor_element_count = {_EXPECTED_TENSOR_ELEMENTS}",
            name="ck_adapter_import_source_tensor_elements",
        ),
        sa.CheckConstraint(
            "tensor_dtype IS NULL OR "
            f"(tensor_dtype = 'F16' AND tensor_payload_byte_size = "
            f"{_EXPECTED_TENSOR_BYTES['F16']}) "
            "OR "
            f"(tensor_dtype = 'BF16' AND tensor_payload_byte_size = "
            f"{_EXPECTED_TENSOR_BYTES['BF16']}) "
            "OR "
            f"(tensor_dtype = 'F32' AND tensor_payload_byte_size = "
            f"{_EXPECTED_TENSOR_BYTES['F32']})",
            name="ck_adapter_import_source_tensor_contract",
        ),
        sa.CheckConstraint(
            f"source_contract_version = '{_ADAPTER_SOURCE_CONTRACT_VERSION}' AND "
            f"intake_contract_version = '{_ADAPTER_INTAKE_CONTRACT_VERSION}' AND "
            f"config_contract_version = '{_ADAPTER_CONFIG_CONTRACT_VERSION}' AND "
            f"tensor_contract_version = '{_ADAPTER_TENSOR_CONTRACT_VERSION}' AND "
            f"base_model_id = '{_BASE_MODEL_ID}' AND "
            f"base_model_revision = '{_BASE_MODEL_REVISION}' AND "
            f"base_model_license = '{_BASE_MODEL_LICENSE}' AND "
            f"peft_version = '{_PEFT_FORMAT_REFERENCE_VERSION}' AND "
            f"safetensors_format = '{_SAFETENSORS_FORMAT_REFERENCE_VERSION}'",
            name="ck_adapter_import_source_contract",
        ),
        sa.CheckConstraint(
            "(status = 'staging' AND authoritative_attempt_id IS NULL AND committed_at IS NULL "
            "AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'committed' AND authoritative_attempt_id IS NOT NULL "
            "AND adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 "
            "AND adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 "
            "AND intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL "
            f"AND tensor_count = {_EXPECTED_TENSOR_COUNT} "
            f"AND tensor_element_count = {_EXPECTED_TENSOR_ELEMENTS} "
            "AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL "
            "AND rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'rejected' AND rejected_at IS NOT NULL AND committed_at IS NULL "
            "AND abandoned_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL "
            f"AND error_code IN ({_quoted(_VALIDATION_ERRORS)})) OR "
            "(status = 'abandoned' AND abandoned_at IS NOT NULL AND rejected_at IS NULL "
            "AND committed_at IS NULL AND purged_at IS NULL AND authoritative_attempt_id IS NULL "
            f"AND error_code IN ({_quoted(_OPERATIONAL_ERRORS)})) OR "
            "(status IN ('claimed','consumed','purge_pending') AND "
            "authoritative_attempt_id IS NOT NULL AND "
            "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
            "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
            "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
            f"tensor_count = {_EXPECTED_TENSOR_COUNT} AND "
            f"tensor_element_count = {_EXPECTED_TENSOR_ELEMENTS} AND "
            "tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
            "rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND "
            "error_code IS NULL) OR "
            "(status = 'purged' AND authoritative_attempt_id IS NOT NULL AND "
            "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
            "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
            "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
            f"tensor_count = {_EXPECTED_TENSOR_COUNT} AND "
            f"tensor_element_count = {_EXPECTED_TENSOR_ELEMENTS} AND "
            "tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
            "purged_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND "
            "error_code IS NULL)",
            name="ck_adapter_import_source_lifecycle",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_quoted(_SOURCE_ERRORS)})",
            name="ck_adapter_import_source_error_code",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_import_source_department"),
    )
    op.create_index(
        "ix_adapter_import_source_department_status_created",
        "adapter_import_sources",
        ["department_id", "status", "created_at"],
    )

    op.create_table(
        "adapter_import_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ownership_manifest", sa.JSON()),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("staged_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
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
            "status IN ('registered','validated','staged','published','committed','failed',"
            "'abandoned')",
            name="ck_adapter_import_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 AND version > 0",
            name="ck_adapter_import_attempt_versions",
        ),
        sa.CheckConstraint(
            "ownership_manifest IS NULL OR json_typeof(ownership_manifest) = 'object'",
            name="ck_adapter_import_attempt_manifest_object",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND validated_at IS NULL AND staged_at IS NULL "
            "AND published_at IS NULL AND committed_at IS NULL AND finished_at IS NULL "
            "AND cleanup_confirmed_at IS NULL AND ownership_manifest IS NULL AND "
            "error_code IS NULL) OR "
            "(status = 'validated' AND validated_at IS NOT NULL AND staged_at IS NULL "
            "AND published_at IS NULL AND committed_at IS NULL AND finished_at IS NULL "
            "AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
            "(status = 'staged' AND validated_at IS NOT NULL AND staged_at IS NOT NULL "
            "AND ownership_manifest IS NOT NULL AND published_at IS NULL AND committed_at IS NULL "
            "AND finished_at IS NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
            "(status = 'published' AND validated_at IS NOT NULL AND staged_at IS NOT NULL "
            "AND published_at IS NOT NULL AND ownership_manifest IS NOT NULL AND "
            "committed_at IS NULL "
            "AND finished_at IS NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
            "(status = 'committed' AND validated_at IS NOT NULL AND staged_at IS NOT NULL "
            "AND published_at IS NOT NULL AND committed_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND ownership_manifest IS NOT NULL AND cleanup_confirmed_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status IN ('failed','abandoned') AND finished_at IS NOT NULL AND "
            "committed_at IS NULL AND cleanup_confirmed_at IS NULL AND error_code IS NOT NULL)",
            name="ck_adapter_import_attempt_lifecycle",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_quoted(_SOURCE_ERRORS)})",
            name="ck_adapter_import_attempt_error_code",
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_import_attempt_source_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "department_id", "source_bundle_id", name="uq_adapter_import_attempt_scope"
        ),
        sa.UniqueConstraint(
            "source_bundle_id", "attempt_number", name="uq_adapter_import_attempt_number"
        ),
        sa.UniqueConstraint("publication_attempt_id", name="uq_adapter_import_publication_attempt"),
    )
    op.create_index(
        "ix_adapter_import_attempt_department_status_created",
        "adapter_import_attempts",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "uq_adapter_import_attempt_active",
        "adapter_import_attempts",
        ["source_bundle_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('registered','validated','staged','published')"),
    )
    op.create_foreign_key(
        "fk_adapter_import_source_authoritative_attempt_scope",
        "adapter_import_sources",
        "adapter_import_attempts",
        ["authoritative_attempt_id", "department_id", "id"],
        ["id", "department_id", "source_bundle_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_adapter_import_source_authoritative_attempt_scope",
        "adapter_import_sources",
        type_="foreignkey",
    )
    op.drop_index("uq_adapter_import_attempt_active", table_name="adapter_import_attempts")
    op.drop_index(
        "ix_adapter_import_attempt_department_status_created",
        table_name="adapter_import_attempts",
    )
    op.drop_table("adapter_import_attempts")
    op.drop_index(
        "ix_adapter_import_source_department_status_created",
        table_name="adapter_import_sources",
    )
    op.drop_table("adapter_import_sources")

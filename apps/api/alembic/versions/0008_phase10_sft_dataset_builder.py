"""Add metadata-only Phase 10 SFT source and dataset-build state.

Revision ID: 0008_phase10_sft_dataset_builder
Revises: 0007_phase9_evaluation_runner
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_phase10_sft_dataset_builder"
down_revision = "0007_phase9_evaluation_runner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sft_source_bundles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("imported_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("normalization_version", sa.String(100), nullable=False),
        sa.Column("example_contract_version", sa.String(100), nullable=False),
        sa.Column("example_count", sa.Integer(), nullable=False),
        sa.Column("group_count", sa.Integer(), nullable=False),
        sa.Column("source_reference_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("examples_sha256", sa.String(64), nullable=False),
        sa.Column("authority_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("examples_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active','archived','purged')", name="ck_sft_source_bundle_status"
        ),
        sa.CheckConstraint(
            "artifact_contract_version = 'phase10-sft-source-v1'",
            name="ck_sft_source_bundle_artifact_contract",
        ),
        sa.CheckConstraint(
            "normalization_version = 'phase10-sft-normalization-v1'",
            name="ck_sft_source_bundle_normalization_version",
        ),
        sa.CheckConstraint(
            "example_contract_version = 'phase10-sft-example-v1'",
            name="ck_sft_source_bundle_example_contract",
        ),
        sa.CheckConstraint(
            "example_count BETWEEN 2 AND 100000", name="ck_sft_source_bundle_examples"
        ),
        sa.CheckConstraint("group_count >= 2", name="ck_sft_source_bundle_groups"),
        sa.CheckConstraint(
            "source_reference_count >= example_count", name="ck_sft_source_bundle_references"
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$' AND examples_sha256 ~ '^[0-9a-f]{64}$' "
            "AND authority_snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sft_source_bundle_hashes",
        ),
        sa.CheckConstraint(
            "examples_byte_size BETWEEN 1 AND 536870912", name="ck_sft_source_bundle_size"
        ),
        sa.CheckConstraint("version > 0", name="ck_sft_source_bundle_version"),
        sa.CheckConstraint(
            "(status = 'active' AND archived_at IS NULL AND purged_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL AND purged_at IS NULL) OR "
            "(status = 'purged' AND purged_at IS NOT NULL)",
            name="ck_sft_source_bundle_lifecycle",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_sft_source_bundle_department"),
    )
    op.create_index(
        "ix_sft_source_bundle_department_status_created",
        "sft_source_bundles",
        ["department_id", "status", "created_at"],
    )

    op.create_table(
        "sft_source_import_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("import_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("stage_id", sa.Uuid(), nullable=False),
        sa.Column("imported_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest_sha256", sa.String(64)),
        sa.Column("examples_sha256", sa.String(64)),
        sa.Column("authority_snapshot_sha256", sa.String(64)),
        sa.Column("artifact_manifest", sa.JSON()),
        sa.Column("examples_byte_size", sa.BigInteger()),
        sa.Column("staged_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("abandoned_at", sa.DateTime(timezone=True)),
        sa.Column("cleanup_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('registered','staged','published','committed','failed','abandoned')",
            name="ck_sft_source_import_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_sft_source_import_version"),
        sa.CheckConstraint(
            "(status = 'registered' AND staged_at IS NULL AND published_at IS NULL "
            "AND committed_at IS NULL AND failed_at IS NULL AND abandoned_at IS NULL) OR "
            "(status = 'staged' AND staged_at IS NOT NULL AND published_at IS NULL "
            "AND committed_at IS NULL AND failed_at IS NULL AND abandoned_at IS NULL) OR "
            "(status = 'published' AND staged_at IS NOT NULL AND published_at IS NOT NULL "
            "AND committed_at IS NULL AND failed_at IS NULL AND abandoned_at IS NULL) OR "
            "(status = 'committed' AND committed_at IS NOT NULL) OR "
            "(status = 'failed' AND failed_at IS NOT NULL AND cleanup_confirmed_at IS NOT NULL) OR "
            "(status = 'abandoned' AND abandoned_at IS NOT NULL)",
            name="ck_sft_source_import_lifecycle",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", "source_bundle_id", name="uq_sft_import_scope"),
        sa.UniqueConstraint("import_attempt_id"),
    )
    op.create_index(
        "ix_sft_source_import_department_status",
        "sft_source_import_attempts",
        ["department_id", "status", "created_at"],
    )

    op.create_table(
        "sft_dataset_builds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("publication_attempt_id", sa.Uuid()),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("example_contract_version", sa.String(100), nullable=False),
        sa.Column("normalization_version", sa.String(100), nullable=False),
        sa.Column("split_version", sa.String(100), nullable=False),
        sa.Column("validation_ratio", sa.Numeric(3, 2), nullable=False),
        sa.Column("source_example_count", sa.Integer(), nullable=False),
        sa.Column("source_group_count", sa.Integer(), nullable=False),
        sa.Column("source_reference_count", sa.Integer(), nullable=False),
        sa.Column("train_example_count", sa.Integer()),
        sa.Column("validation_example_count", sa.Integer()),
        sa.Column("result_manifest_sha256", sa.String(64)),
        sa.Column("train_sha256", sa.String(64)),
        sa.Column("train_byte_size", sa.BigInteger()),
        sa.Column("validation_sha256", sa.String(64)),
        sa.Column("validation_byte_size", sa.BigInteger()),
        sa.Column("provenance_sha256", sa.String(64)),
        sa.Column("provenance_byte_size", sa.BigInteger()),
        sa.Column("publication_manifest", sa.JSON()),
        sa.Column("artifact_cleanup_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_user_id", sa.Uuid()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_sft_build_status",
        ),
        sa.CheckConstraint(
            "review_status IN ('not_ready','pending','approved','rejected','archived','purged')",
            name="ck_sft_build_review_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN "
            "('source_artifact_missing','source_artifact_mismatch','source_contract_invalid',"
            "'source_authority_changed','department_unavailable','requester_unauthorized',"
            "'dataset_publication_failed','claim_lost','cancelled','worker_shutdown',"
            "'worker_timeout','database_unavailable')",
            name="ck_sft_build_error_code",
        ),
        sa.CheckConstraint("attempt_number > 0 AND version > 0", name="ck_sft_build_versions"),
        sa.CheckConstraint(
            "artifact_contract_version = 'phase10-sft-dataset-v1'",
            name="ck_sft_build_artifact_contract",
        ),
        sa.CheckConstraint(
            "example_contract_version = 'phase10-sft-example-v1'",
            name="ck_sft_build_example_contract",
        ),
        sa.CheckConstraint(
            "normalization_version = 'phase10-sft-normalization-v1'",
            name="ck_sft_build_normalization_contract",
        ),
        sa.CheckConstraint(
            "split_version = 'phase10-sft-group-split-v1'",
            name="ck_sft_build_split_contract",
        ),
        sa.CheckConstraint("validation_ratio = 0.10", name="ck_sft_build_validation_ratio"),
        sa.CheckConstraint(
            "source_example_count BETWEEN 2 AND 100000 AND source_group_count >= 2 "
            "AND source_reference_count >= source_example_count",
            name="ck_sft_build_source_counts",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND review_status = 'not_ready' AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL AND publication_attempt_id IS NULL AND error_code IS NULL) "
            "OR status <> 'queued'",
            name="ck_sft_build_queued_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND review_status = 'not_ready' AND worker_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND publication_attempt_id IS NOT NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_sft_build_running_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND review_status IN "
            "('pending','approved','rejected','archived','purged') AND finished_at IS NOT NULL "
            "AND train_example_count > 0 AND validation_example_count > 0 "
            "AND result_manifest_sha256 IS NOT NULL AND train_sha256 IS NOT NULL "
            "AND validation_sha256 IS NOT NULL AND provenance_sha256 IS NOT NULL "
            "AND error_code IS NULL) OR status <> 'succeeded'",
            name="ck_sft_build_succeeded_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["sft_source_bundles.id", "sft_source_bundles.department_id"],
            name="fk_sft_build_source_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", "source_bundle_id", name="uq_sft_build_scope"),
    )
    op.create_index(
        "ix_sft_build_department_status_created",
        "sft_dataset_builds",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "ix_sft_build_claim", "sft_dataset_builds", ["status", "lease_expires_at", "created_at"]
    )

    op.create_table(
        "sft_artifact_reconciliation_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('registered','completed','completed_with_blocks')",
            name="ck_sft_reconciliation_operation_status",
        ),
        sa.CheckConstraint(
            "operation_type IN ('reconcile','purge')",
            name="ck_sft_reconciliation_operation_type",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL) OR "
            "(status IN ('completed','completed_with_blocks') AND completed_at IS NOT NULL)",
            name="ck_sft_reconciliation_operation_lifecycle",
        ),
        sa.CheckConstraint(
            "limit_value BETWEEN 1 AND 1000", name="ck_sft_reconciliation_operation_limit"
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_sft_reconciliation_operation_scope"),
    )
    op.create_index(
        "ix_sft_reconciliation_operation_department",
        "sft_artifact_reconciliation_operations",
        ["department_id", "created_at"],
    )
    op.create_table(
        "sft_artifact_reconciliation_operation_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("ownership_manifest", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("blocked_reason_code", sa.String(48)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('registered','completed','blocked')",
            name="ck_sft_reconciliation_item_status",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'blocked' AND completed_at IS NULL AND blocked_at IS NOT NULL "
            "AND blocked_reason_code IS NOT NULL)",
            name="ck_sft_reconciliation_item_lifecycle",
        ),
        sa.CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN "
            "('staging_path_unsafe','artifact_ownership_mismatch','artifact_manifest_invalid',"
            "'artifact_permissions_invalid','artifact_state_changed')",
            name="ck_sft_reconciliation_item_reason",
        ),
        sa.CheckConstraint(
            "resource_type IN ('source_stage','source_final','dataset_stage','dataset_final')",
            name="ck_sft_reconciliation_item_resource_type",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "department_id"],
            [
                "sft_artifact_reconciliation_operations.id",
                "sft_artifact_reconciliation_operations.department_id",
            ],
            name="fk_sft_reconciliation_item_operation_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "resource_type", "resource_id", name="uq_sft_reconciliation_item"
        ),
    )


def downgrade() -> None:
    op.drop_table("sft_artifact_reconciliation_operation_items")
    op.drop_index(
        "ix_sft_reconciliation_operation_department",
        table_name="sft_artifact_reconciliation_operations",
    )
    op.drop_table("sft_artifact_reconciliation_operations")
    op.drop_index("ix_sft_build_claim", table_name="sft_dataset_builds")
    op.drop_index("ix_sft_build_department_status_created", table_name="sft_dataset_builds")
    op.drop_table("sft_dataset_builds")
    op.drop_index("ix_sft_source_import_department_status", table_name="sft_source_import_attempts")
    op.drop_table("sft_source_import_attempts")
    op.drop_index("ix_sft_source_bundle_department_status_created", table_name="sft_source_bundles")
    op.drop_table("sft_source_bundles")

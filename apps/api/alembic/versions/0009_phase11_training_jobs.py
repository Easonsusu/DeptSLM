"""Add metadata-only Phase 11 LlamaFactory job-generation state.

Revision ID: 0009_phase11_training_jobs
Revises: 0008_phase10_sft_dataset_builder
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_phase11_training_jobs"
down_revision = "0008_phase10_sft_dataset_builder"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_build_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("profile_id", sa.String(80), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("base_model_license", sa.String(40), nullable=False),
        sa.Column("llamafactory_version", sa.String(32), nullable=False),
        sa.Column("artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("manifest_contract_version", sa.String(100), nullable=False),
        sa.Column("configuration_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_info_contract_version", sa.String(100), nullable=False),
        sa.Column("execution_profile_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_example_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_normalization_version", sa.String(100), nullable=False),
        sa.Column("dataset_split_version", sa.String(100), nullable=False),
        sa.Column("dataset_build_version", sa.Integer(), nullable=False),
        sa.Column("dataset_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_status", sa.String(16), nullable=False),
        sa.Column("dataset_review_status", sa.String(16), nullable=False),
        sa.Column("dataset_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_publication_attempt_number", sa.Integer(), nullable=False),
        sa.Column("dataset_code_revision", sa.String(40), nullable=False),
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
        sa.Column("execution_scope_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("publication_attempt_id", sa.Uuid()),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("train_example_count", sa.Integer()),
        sa.Column("validation_example_count", sa.Integer()),
        sa.Column("maximum_record_content_bytes", sa.Integer(), nullable=False),
        sa.Column("result_manifest_sha256", sa.String(64)),
        sa.Column("training_config_sha256", sa.String(64)),
        sa.Column("training_config_byte_size", sa.BigInteger()),
        sa.Column("dataset_info_sha256", sa.String(64)),
        sa.Column("dataset_info_byte_size", sa.BigInteger()),
        sa.Column("train_sha256", sa.String(64)),
        sa.Column("train_byte_size", sa.BigInteger()),
        sa.Column("validation_sha256", sa.String(64)),
        sa.Column("validation_byte_size", sa.BigInteger()),
        sa.Column("publication_manifest", sa.JSON()),
        sa.Column("artifact_cleanup_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
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
            name="ck_training_job_status",
        ),
        sa.CheckConstraint(
            "review_status IN ('not_ready','pending','approved','rejected','archived','purged')",
            name="ck_training_job_review_status",
        ),
        sa.CheckConstraint(
            "profile_id IN ('phase11-qwen3-0.6b-lora-v1','phase11-qwen3-0.6b-qlora-nf4-v1')",
            name="ck_training_job_profile",
        ),
        sa.CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca' AND "
            "base_model_license = 'Apache-2.0' AND llamafactory_version = '0.9.5'",
            name="ck_training_job_model_contract",
        ),
        sa.CheckConstraint(
            "artifact_contract_version = 'phase11-training-job-v1' AND "
            "manifest_contract_version = 'phase11-training-job-manifest-v1' AND "
            "configuration_contract_version = 'phase11-training-config-v1' AND "
            "dataset_info_contract_version = 'phase11-dataset-info-v1' AND "
            "execution_profile_contract_version = 'phase11-execution-profile-v1'",
            name="ck_training_job_artifact_contracts",
        ),
        sa.CheckConstraint(
            "dataset_artifact_contract_version = 'phase10-sft-dataset-v1' AND "
            "dataset_example_contract_version = 'phase10-sft-example-v1' AND "
            "dataset_normalization_version = 'phase10-sft-normalization-v1' AND "
            "dataset_split_version = 'phase10-sft-group-split-v1'",
            name="ck_training_job_dataset_contracts",
        ),
        sa.CheckConstraint(
            "dataset_status = 'succeeded' AND dataset_review_status = 'approved'",
            name="ck_training_job_dataset_snapshot_lifecycle",
        ),
        sa.CheckConstraint(
            "dataset_publication_attempt_number > 0 AND dataset_train_example_count > 0 "
            "AND dataset_validation_example_count > 0 AND dataset_source_example_count >= 2 "
            "AND dataset_source_group_count >= 2 AND "
            "dataset_source_reference_count >= dataset_source_example_count",
            name="ck_training_job_dataset_snapshot_counts",
        ),
        sa.CheckConstraint(
            "maximum_record_content_bytes = 7680", name="ck_training_job_record_limit"
        ),
        sa.CheckConstraint("attempt_number > 0 AND version > 0", name="ck_training_job_versions"),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ('dataset_unavailable',"
            "'dataset_artifact_mismatch','dataset_contract_invalid','dataset_record_invalid',"
            "'dataset_authority_changed','department_unavailable','requester_unauthorized',"
            "'training_job_publication_failed','claim_lost','cancelled','worker_shutdown',"
            "'worker_timeout','database_unavailable')",
            name="ck_training_job_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND review_status = 'not_ready' AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL AND publication_attempt_id IS NULL AND error_code IS NULL) "
            "OR status <> 'queued'",
            name="ck_training_job_queued_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND review_status = 'not_ready' AND worker_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND publication_attempt_id IS NOT NULL "
            "AND error_code IS NULL) OR status <> 'running'",
            name="ck_training_job_running_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND review_status IN "
            "('pending','approved','rejected','archived','purged') "
            "AND finished_at IS NOT NULL AND train_example_count > 0 "
            "AND validation_example_count > 0 AND result_manifest_sha256 IS NOT NULL "
            "AND training_config_sha256 IS NOT NULL AND dataset_info_sha256 IS NOT NULL "
            "AND train_sha256 IS NOT NULL AND validation_sha256 IS NOT NULL "
            "AND error_code IS NULL) OR status <> 'succeeded'",
            name="ck_training_job_succeeded_lifecycle",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["dataset_build_id", "department_id"],
            ["sft_dataset_builds.id", "sft_dataset_builds.department_id"],
            name="fk_training_job_dataset_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_training_job_department"),
    )
    op.create_index(
        "ix_training_job_department_status_created",
        "training_jobs",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "ix_training_job_claim", "training_jobs", ["status", "lease_expires_at", "created_at"]
    )

    op.create_table(
        "training_job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ownership_manifest", sa.JSON()),
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
            "status IN ('registered','running','staged','published','succeeded',"
            "'failed','cancelled','reclaimed')",
            name="ck_training_job_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 AND version > 0", name="ck_training_job_attempt_versions"
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND claimed_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND claimed_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status = 'staged' AND claimed_at IS NOT NULL AND staged_at IS NOT NULL "
            "AND finished_at IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND published_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status IN ('failed','cancelled','reclaimed') AND finished_at IS NOT NULL)",
            name="ck_training_job_attempt_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_training_job_attempt_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "training_job_id", "attempt_number", name="uq_training_job_attempt_number"
        ),
        sa.UniqueConstraint("publication_attempt_id", name="uq_training_job_attempt_publication"),
        sa.UniqueConstraint(
            "training_job_id",
            "department_id",
            "publication_attempt_id",
            name="uq_training_job_attempt_scope_publication",
        ),
    )
    op.create_index(
        "ix_training_job_attempt_department_status",
        "training_job_attempts",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "uq_training_job_attempt_active",
        "training_job_attempts",
        ["training_job_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('registered','running','staged','published')"),
    )

    op.create_table(
        "training_job_artifact_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("retention_days", sa.Integer()),
        sa.Column("operation_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "operation_type IN ('reconcile','purge')", name="ck_training_job_operation_type"
        ),
        sa.CheckConstraint(
            "status IN ('registered','completed','completed_with_blocks')",
            name="ck_training_job_operation_status",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL) OR "
            "(status IN ('completed','completed_with_blocks') AND completed_at IS NOT NULL)",
            name="ck_training_job_operation_lifecycle",
        ),
        sa.CheckConstraint(
            "limit_value BETWEEN 1 AND 1000", name="ck_training_job_operation_limit"
        ),
        sa.CheckConstraint(
            "(operation_type = 'reconcile' AND retention_days IS NULL) OR "
            "(operation_type = 'purge' AND retention_days BETWEEN 30 AND 730)",
            name="ck_training_job_operation_retention",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_training_job_operation_scope"),
    )
    op.create_index(
        "ix_training_job_operation_department",
        "training_job_artifact_operations",
        ["department_id", "created_at"],
    )

    op.create_table(
        "training_job_purge_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_id", sa.Uuid(), nullable=False),
        sa.Column("expected_job_version", sa.Integer(), nullable=False),
        sa.Column("expected_review_status", sa.String(16), nullable=False),
        sa.Column("retention_anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "registered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deletion_authorized_at", sa.DateTime(timezone=True)),
        sa.Column("terminalized_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('registered','deletion_authorized','terminalized')",
            name="ck_training_job_purge_reservation_status",
        ),
        sa.CheckConstraint(
            "expected_review_status IN ('rejected','archived')",
            name="ck_training_job_purge_reservation_review",
        ),
        sa.CheckConstraint(
            "retention_days BETWEEN 30 AND 730 AND expected_job_version > 0 AND version > 0",
            name="ck_training_job_purge_reservation_values",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND deletion_authorized_at IS NULL AND terminalized_at IS NULL) "
            "OR (status = 'deletion_authorized' AND deletion_authorized_at IS NOT NULL "
            "AND terminalized_at IS NULL) OR (status = 'terminalized' "
            "AND deletion_authorized_at IS NOT NULL AND terminalized_at IS NOT NULL)",
            name="ck_training_job_purge_reservation_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "department_id"],
            [
                "training_job_artifact_operations.id",
                "training_job_artifact_operations.department_id",
            ],
            name="fk_training_job_purge_reservation_operation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_training_job_purge_reservation_job_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "training_job_id", name="uq_training_job_purge_reservation_operation"
        ),
    )
    op.create_index(
        "uq_training_job_purge_reservation_active",
        "training_job_purge_reservations",
        ["training_job_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('registered','deletion_authorized')"),
    )
    op.create_index(
        "ix_training_job_purge_reservation_operation",
        "training_job_purge_reservations",
        ["operation_id", "status", "created_at"],
    )

    op.create_table(
        "training_job_artifact_operation_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_id", sa.Uuid(), nullable=False),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("resource_surface", sa.String(16), nullable=False),
        sa.Column("ownership_manifest", sa.JSON()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("blocked_reason_code", sa.String(48)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "resource_surface IN ('stage','final')", name="ck_training_job_item_surface"
        ),
        sa.CheckConstraint(
            "status IN ('registered','completed','blocked')", name="ck_training_job_item_status"
        ),
        sa.CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN "
            "('staging_path_unsafe','artifact_ownership_mismatch','artifact_manifest_invalid',"
            "'artifact_permissions_invalid')",
            name="ck_training_job_item_reason",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'blocked' AND completed_at IS NULL AND blocked_at IS NOT NULL "
            "AND blocked_reason_code IS NOT NULL)",
            name="ck_training_job_item_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "department_id"],
            [
                "training_job_artifact_operations.id",
                "training_job_artifact_operations.department_id",
            ],
            name="fk_training_job_operation_item_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_training_job_operation_item_job_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_job_id", "department_id", "publication_attempt_id"],
            [
                "training_job_attempts.training_job_id",
                "training_job_attempts.department_id",
                "training_job_attempts.publication_attempt_id",
            ],
            name="fk_training_job_operation_item_attempt_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            "training_job_id",
            "publication_attempt_id",
            "resource_surface",
            name="uq_training_job_operation_item",
        ),
    )
    op.create_index(
        "ix_training_job_operation_item_status",
        "training_job_artifact_operation_items",
        ["operation_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_training_job_operation_item_status", table_name="training_job_artifact_operation_items"
    )
    op.drop_table("training_job_artifact_operation_items")
    op.drop_index(
        "ix_training_job_purge_reservation_operation", table_name="training_job_purge_reservations"
    )
    op.drop_index(
        "uq_training_job_purge_reservation_active", table_name="training_job_purge_reservations"
    )
    op.drop_table("training_job_purge_reservations")
    op.drop_index(
        "ix_training_job_operation_department", table_name="training_job_artifact_operations"
    )
    op.drop_table("training_job_artifact_operations")
    op.drop_index("uq_training_job_attempt_active", table_name="training_job_attempts")
    op.drop_index("ix_training_job_attempt_department_status", table_name="training_job_attempts")
    op.drop_table("training_job_attempts")
    op.drop_index("ix_training_job_claim", table_name="training_jobs")
    op.drop_index("ix_training_job_department_status_created", table_name="training_jobs")
    op.drop_table("training_jobs")

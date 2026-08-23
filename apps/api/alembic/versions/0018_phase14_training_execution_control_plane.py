# ruff: noqa: E501
"""Add the Phase 14.1 controlled training-execution authority."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_phase14_training_execution_control_plane"
down_revision = "0017_phase12_adapter_runtime_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The parent/current-attempt relationship is added after both tables exist
    # so an empty database and a populated downgrade/upgrade cycle see the same
    # restrictive foreign-key contract.
    op.create_table(
        "training_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_version", sa.Integer(), nullable=False),
        sa.Column("training_job_status", sa.String(16), nullable=False),
        sa.Column("training_job_review_status", sa.String(16), nullable=False),
        sa.Column("training_job_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_attempt_number", sa.Integer(), nullable=False),
        sa.Column("training_job_code_revision", sa.String(40), nullable=False),
        sa.Column("training_job_execution_scope_id", sa.Uuid(), nullable=False),
        sa.Column("training_job_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_manifest_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_publication_manifest", sa.JSON(), nullable=False),
        sa.Column("training_job_config_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_config_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_dataset_info_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_dataset_info_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_train_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_train_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_validation_sha256", sa.String(64), nullable=False),
        sa.Column("training_job_validation_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("training_job_artifact_cleanup_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("training_job_purged_at", sa.DateTime(timezone=True)),
        sa.Column("training_job_profile_id", sa.String(80), nullable=False),
        sa.Column("training_job_base_model_id", sa.String(200), nullable=False),
        sa.Column("training_job_base_model_revision", sa.String(64), nullable=False),
        sa.Column("training_job_base_model_license", sa.String(40), nullable=False),
        sa.Column("training_job_llamafactory_version", sa.String(32), nullable=False),
        sa.Column("training_job_artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("training_job_manifest_contract_version", sa.String(100), nullable=False),
        sa.Column("training_job_configuration_contract_version", sa.String(100), nullable=False),
        sa.Column("training_job_dataset_info_contract_version", sa.String(100), nullable=False),
        sa.Column(
            "training_job_execution_profile_contract_version", sa.String(100), nullable=False
        ),
        sa.Column("dataset_build_id", sa.Uuid(), nullable=False),
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
        sa.Column("dataset_artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_example_contract_version", sa.String(100), nullable=False),
        sa.Column("dataset_normalization_version", sa.String(100), nullable=False),
        sa.Column("dataset_split_version", sa.String(100), nullable=False),
        sa.Column("profile_id", sa.String(80), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("base_model_license", sa.String(40), nullable=False),
        sa.Column("llamafactory_version", sa.String(32), nullable=False),
        sa.Column("execution_contract_version", sa.String(100), nullable=False),
        sa.Column("execution_code_revision", sa.String(40), nullable=False),
        sa.Column("authority_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("current_attempt_number", sa.Integer(), nullable=False),
        sa.Column("current_attempt_id", sa.Uuid()),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_training_execution_training_job_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_training_execution_requester",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "department_id", name="uq_training_execution_scope"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancel_requested','cancelled')",
            name="ck_training_execution_status",
        ),
        sa.CheckConstraint(
            "training_job_status = 'succeeded' AND training_job_review_status = 'approved'",
            name="ck_training_execution_source_lifecycle",
        ),
        sa.CheckConstraint(
            "json_typeof(training_job_publication_manifest) = 'object'",
            name="ck_training_execution_publication_manifest",
        ),
        sa.CheckConstraint(
            "profile_id IN ('phase11-qwen3-0.6b-lora-v1','phase11-qwen3-0.6b-qlora-nf4-v1')",
            name="ck_training_execution_profile",
        ),
        sa.CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca' AND "
            "base_model_license = 'Apache-2.0' AND llamafactory_version = '0.9.5'",
            name="ck_training_execution_model_contract",
        ),
        sa.CheckConstraint(
            "execution_contract_version = 'phase14-training-execution-v1'",
            name="ck_training_execution_contract",
        ),
        sa.CheckConstraint(
            "execution_code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_training_execution_code_revision",
        ),
        sa.CheckConstraint(
            "authority_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_training_execution_authority_fingerprint",
        ),
        sa.CheckConstraint(
            "training_job_version > 0 AND training_job_attempt_number > 0 AND "
            "current_attempt_number > 0 AND version > 0",
            name="ck_training_execution_versions",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'training_job_unavailable','training_job_authority_changed',"
            "'training_job_artifact_missing','training_job_artifact_mismatch',"
            "'input_snapshot_failed','runtime_unavailable','runtime_protocol_invalid',"
            "'department_unavailable','requester_unauthorized','claim_lost','cancelled',"
            "'worker_shutdown','worker_timeout','database_unavailable')",
            name="ck_training_execution_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND current_attempt_id IS NULL AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL AND cancellation_requested_at IS NULL AND error_code IS NULL) "
            "OR (status IN ('running','cancel_requested') AND current_attempt_id IS NOT NULL "
            "AND worker_id IS NOT NULL AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND ((status = 'running' AND cancellation_requested_at IS NULL AND error_code IS NULL) "
            "OR (status = 'cancel_requested' AND cancellation_requested_at IS NOT NULL "
            "AND error_code IS NULL))) "
            "OR (status = 'succeeded' AND current_attempt_id IS NULL AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND error_code IS NULL) "
            "OR (status = 'failed' AND current_attempt_id IS NULL AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL) "
            "OR (status = 'cancelled' AND current_attempt_id IS NULL AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND error_code = 'cancelled')",
            name="ck_training_execution_lifecycle",
        ),
    )
    op.create_table(
        "training_execution_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("input_snapshot_fingerprint", sa.String(64)),
        sa.Column("runtime_fingerprint", sa.String(64)),
        sa.Column("result_classification", sa.String(32)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "department_id"],
            ["training_executions.id", "training_executions.department_id"],
            name="fk_training_execution_attempt_execution_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "department_id", name="uq_training_execution_attempt_scope"),
        sa.UniqueConstraint(
            "execution_id", "attempt_number", name="uq_training_execution_attempt_number"
        ),
        sa.CheckConstraint(
            "status IN ('registered','running','succeeded','failed','cancelled','reclaimed')",
            name="ck_training_execution_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 AND version > 0", name="ck_training_execution_attempt_versions"
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND worker_id IS NULL AND claim_token IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL) OR "
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('succeeded','failed','cancelled','reclaimed') "
            "AND worker_id IS NOT NULL AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND finished_at IS NOT NULL)",
            name="ck_training_execution_attempt_lifecycle",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'training_job_unavailable','training_job_authority_changed',"
            "'training_job_artifact_missing','training_job_artifact_mismatch',"
            "'input_snapshot_failed','runtime_unavailable','runtime_protocol_invalid',"
            "'department_unavailable','requester_unauthorized','claim_lost','cancelled',"
            "'worker_shutdown','worker_timeout','database_unavailable')",
            name="ck_training_execution_attempt_error_code",
        ),
        sa.CheckConstraint(
            "result_classification IS NULL OR result_classification IN "
            "('process_ready','execution_started','execution_succeeded','execution_failed','execution_cancelled')",
            name="ck_training_execution_attempt_result_classification",
        ),
        sa.CheckConstraint(
            "status <> 'succeeded' OR ("
            "input_snapshot_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "runtime_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "result_classification = 'execution_succeeded' AND error_code IS NULL)",
            name="ck_training_execution_attempt_success_contract",
        ),
    )
    op.create_foreign_key(
        "fk_training_execution_current_attempt_scope",
        "training_executions",
        "training_execution_attempts",
        ["current_attempt_id", "department_id"],
        ["id", "department_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_training_execution_active_job_profile",
        "training_executions",
        ["training_job_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running','cancel_requested')"),
    )
    op.create_index(
        "ix_training_execution_department_status_created",
        "training_executions",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "ix_training_execution_claim",
        "training_executions",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "uq_training_execution_attempt_active",
        "training_execution_attempts",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('registered','running')"),
    )
    op.create_index(
        "ix_training_execution_attempt_claim",
        "training_execution_attempts",
        ["department_id", "status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_training_execution_attempt_claim", table_name="training_execution_attempts")
    op.drop_index("uq_training_execution_attempt_active", table_name="training_execution_attempts")
    op.drop_constraint(
        "fk_training_execution_current_attempt_scope", "training_executions", type_="foreignkey"
    )
    op.drop_table("training_execution_attempts")
    op.drop_index("ix_training_execution_claim", table_name="training_executions")
    op.drop_index(
        "ix_training_execution_department_status_created", table_name="training_executions"
    )
    op.drop_index("uq_training_execution_active_job_profile", table_name="training_executions")
    op.drop_table("training_executions")

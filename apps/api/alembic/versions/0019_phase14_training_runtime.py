# ruff: noqa: E501
"""Add the Phase 14.2 real-runtime evidence contract."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_phase14_training_runtime"
down_revision = "0018_phase14_training_execution_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The real runtime returns only the reviewed fixed error vocabulary.  The
    # Phase 14.1 check is replaced in this same revision so failures remain
    # observable without widening the column to arbitrary exception text.
    for table, name in (
        ("training_executions", "ck_training_execution_error_code"),
        ("training_execution_attempts", "ck_training_execution_attempt_error_code"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(
            name,
            table,
            "error_code IS NULL OR error_code IN ("
            "'training_job_unavailable','training_job_authority_changed',"
            "'training_job_artifact_missing','training_job_artifact_mismatch',"
            "'input_snapshot_failed','runtime_unavailable','runtime_protocol_invalid',"
            "'runtime_environment_invalid','runtime_hardware_unsupported',"
            "'runtime_model_unavailable','runtime_dependency_mismatch','runtime_auth_failed',"
            "'runtime_busy','training_config_invalid','child_start_failed','child_failed',"
            "'child_timeout','runtime_disconnected','output_limit_exceeded','output_invalid',"
            "'runtime_cleanup_failed','department_unavailable','requester_unauthorized',"
            "'claim_lost','cancelled','worker_shutdown','worker_timeout','database_unavailable')",
        )
    table = "training_execution_attempts"
    op.add_column(
        table,
        sa.Column("runtime_kind", sa.String(16), nullable=False, server_default="fake"),
    )
    op.alter_column(table, "runtime_kind", server_default=None)
    op.add_column(table, sa.Column("runtime_contract_version", sa.String(100)))
    op.add_column(table, sa.Column("runtime_dependency_lock_sha256", sa.String(64)))
    op.add_column(table, sa.Column("runtime_environment_profile_id", sa.String(100)))
    op.add_column(table, sa.Column("runtime_environment_fingerprint", sa.String(64)))
    op.add_column(table, sa.Column("runtime_hardware_profile_id", sa.String(100)))
    op.add_column(table, sa.Column("runtime_hardware_fingerprint", sa.String(64)))
    op.add_column(table, sa.Column("output_stage_fingerprint", sa.String(64)))
    op.add_column(table, sa.Column("output_file_count", sa.Integer()))
    op.add_column(table, sa.Column("output_total_bytes", sa.BigInteger()))
    op.add_column(table, sa.Column("output_retained_at", sa.DateTime(timezone=True)))
    op.add_column(table, sa.Column("output_purged_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_training_execution_attempt_runtime_kind",
        table,
        "runtime_kind IN ('fake','real')",
    )
    op.create_check_constraint(
        "ck_training_execution_attempt_runtime_contract",
        table,
        "runtime_contract_version IS NULL OR runtime_contract_version = 'phase14-training-runtime-v1'",
    )
    op.create_check_constraint(
        "ck_training_execution_attempt_runtime_hashes",
        table,
        "(runtime_dependency_lock_sha256 IS NULL OR runtime_dependency_lock_sha256 ~ '^[0-9a-f]{64}$') AND "
        "(runtime_environment_fingerprint IS NULL OR runtime_environment_fingerprint ~ '^[0-9a-f]{64}$') AND "
        "(runtime_hardware_fingerprint IS NULL OR runtime_hardware_fingerprint ~ '^[0-9a-f]{64}$') AND "
        "(output_stage_fingerprint IS NULL OR output_stage_fingerprint ~ '^[0-9a-f]{64}$')",
    )
    op.create_check_constraint(
        "ck_training_execution_attempt_output_bounds",
        table,
        "(output_file_count IS NULL AND output_total_bytes IS NULL) OR "
        "(output_file_count >= 0 AND output_file_count <= 4096 AND output_total_bytes >= 0 AND output_total_bytes <= 8589934592)",
    )
    op.create_check_constraint(
        "ck_training_execution_attempt_real_success_contract",
        table,
        "status <> 'succeeded' OR runtime_kind <> 'real' OR ("
        "runtime_contract_version = 'phase14-training-runtime-v1' AND "
        "runtime_dependency_lock_sha256 ~ '^[0-9a-f]{64}$' AND "
        "runtime_environment_profile_id IS NOT NULL AND runtime_environment_profile_id <> '' AND "
        "runtime_environment_fingerprint ~ '^[0-9a-f]{64}$' AND "
        "runtime_hardware_profile_id IS NOT NULL AND runtime_hardware_profile_id <> '' AND "
        "runtime_hardware_fingerprint ~ '^[0-9a-f]{64}$' AND "
        "output_stage_fingerprint ~ '^[0-9a-f]{64}$' AND "
        "output_file_count >= 1 AND output_total_bytes >= 1 AND "
        "output_retained_at IS NOT NULL AND output_purged_at IS NULL AND "
        "input_snapshot_fingerprint ~ '^[0-9a-f]{64}$' AND "
        "runtime_fingerprint ~ '^[0-9a-f]{64}$' AND "
        "result_classification = 'execution_succeeded' AND error_code IS NULL)",
    )
    op.create_check_constraint(
        "ck_training_execution_attempt_output_retention",
        table,
        "output_purged_at IS NULL OR (runtime_kind = 'real' AND output_retained_at IS NOT NULL)",
    )


def downgrade() -> None:
    table = "training_execution_attempts"
    for name in (
        "ck_training_execution_attempt_output_retention",
        "ck_training_execution_attempt_real_success_contract",
        "ck_training_execution_attempt_output_bounds",
        "ck_training_execution_attempt_runtime_hashes",
        "ck_training_execution_attempt_runtime_contract",
        "ck_training_execution_attempt_runtime_kind",
    ):
        op.drop_constraint(name, table, type_="check")
    for table_name, constraint_name in (
        ("training_executions", "ck_training_execution_error_code"),
        ("training_execution_attempts", "ck_training_execution_attempt_error_code"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="check")
    # The legacy Phase 14.1 vocabulary does not contain the real-runtime
    # detail codes. Preserve downgrade safety by collapsing those private
    # codes to the legacy protocol bucket before recreating its CHECKs.
    runtime_only_codes = (
        "runtime_environment_invalid",
        "runtime_hardware_unsupported",
        "runtime_model_unavailable",
        "runtime_dependency_mismatch",
        "runtime_auth_failed",
        "runtime_busy",
        "training_config_invalid",
        "child_start_failed",
        "child_failed",
        "child_timeout",
        "runtime_disconnected",
        "output_limit_exceeded",
        "output_invalid",
        "runtime_cleanup_failed",
    )
    placeholders = ",".join(f"'{code}'" for code in runtime_only_codes)
    op.execute(
        sa.text(
            "UPDATE training_executions SET error_code='runtime_protocol_invalid' "
            f"WHERE error_code IN ({placeholders})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE training_execution_attempts SET error_code='runtime_protocol_invalid' "
            f"WHERE error_code IN ({placeholders})"
        )
    )
    for name in (
        "output_purged_at",
        "output_retained_at",
        "output_total_bytes",
        "output_file_count",
        "output_stage_fingerprint",
        "runtime_hardware_fingerprint",
        "runtime_hardware_profile_id",
        "runtime_environment_fingerprint",
        "runtime_environment_profile_id",
        "runtime_dependency_lock_sha256",
        "runtime_contract_version",
        "runtime_kind",
    ):
        op.drop_column(table, name)
    for table_name, constraint_name in (
        ("training_executions", "ck_training_execution_error_code"),
        ("training_execution_attempts", "ck_training_execution_attempt_error_code"),
    ):
        op.create_check_constraint(
            constraint_name,
            table_name,
            "error_code IS NULL OR error_code IN ("
            "'training_job_unavailable','training_job_authority_changed',"
            "'training_job_artifact_missing','training_job_artifact_mismatch',"
            "'input_snapshot_failed','runtime_unavailable','runtime_protocol_invalid',"
            "'department_unavailable','requester_unauthorized','claim_lost','cancelled',"
            "'worker_shutdown','worker_timeout','database_unavailable')",
        )

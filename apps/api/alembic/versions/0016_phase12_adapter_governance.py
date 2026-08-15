# ruff: noqa: E501
"""Add Phase 12.3 adapter review and deployment governance authorities."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_phase12_adapter_governance"
down_revision = "0015_phase12_adapter_evaluation"
branch_labels = None
depends_on = None

_ERROR_CODES = (
    "adapter_unavailable",
    "adapter_authority_changed",
    "review_unavailable",
    "review_authority_changed",
    "evaluation_unavailable",
    "evaluation_authority_changed",
    "evaluation_gate_failed",
    "suite_authority_changed",
    "registry_artifact_missing",
    "registry_artifact_mismatch",
    "registry_artifact_unsafe",
    "rollback_target_unavailable",
    "deployment_version_conflict",
    "deployment_operation_conflict",
    "purge_conflict",
    "claim_lost",
    "cancelled",
    "worker_shutdown",
    "worker_timeout",
    "requester_unauthorized",
    "department_unavailable",
    "database_unavailable",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join("'" + value + "'" for value in values)


def upgrade() -> None:
    op.create_table(
        "adapter_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_version", sa.Integer(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_version", sa.Integer(), nullable=False),
        sa.Column("baseline_evidence_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_evidence_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("registry_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("registry_attempt_version", sa.Integer(), nullable=False),
        sa.Column("registry_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("registry_attempt_number", sa.Integer(), nullable=False),
        sa.Column("registry_execution_scope_id", sa.Uuid(), nullable=False),
        sa.Column("registry_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("registry_adapter_config_sha256", sa.String(64), nullable=False),
        sa.Column("registry_adapter_config_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("registry_adapter_model_sha256", sa.String(64), nullable=False),
        sa.Column("registry_adapter_model_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_version", sa.Integer(), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("runner_contract_version", sa.String(100), nullable=False),
        sa.Column("artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("metric_contract_version", sa.String(100), nullable=False),
        sa.Column("gate_policy_version", sa.String(100), nullable=False),
        sa.Column("seed_policy_version", sa.String(100), nullable=False),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("suite_artifact_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("suite_canonical_cases_sha256", sa.String(64), nullable=False),
        sa.Column("suite_canonical_cases_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("result_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("result_summary_sha256", sa.String(64), nullable=False),
        sa.Column("case_results_sha256", sa.String(64), nullable=False),
        sa.Column("case_results_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_review_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_review_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_review_evaluation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "registry_attempt_id",
                "department_id",
                "adapter_id",
                "registry_publication_attempt_id",
                "registry_attempt_number",
            ],
            [
                "adapter_registry_attempts.id",
                "adapter_registry_attempts.department_id",
                "adapter_registry_attempts.adapter_id",
                "adapter_registry_attempts.publication_attempt_id",
                "adapter_registry_attempts.attempt_number",
            ],
            name="fk_adapter_review_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dependency_id", "department_id", "adapter_id"],
            [
                "adapter_upstream_dependencies.id",
                "adapter_upstream_dependencies.department_id",
                "adapter_upstream_dependencies.adapter_id",
            ],
            name="fk_adapter_review_dependency_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id", "department_id"],
            ["evaluation_suites.id", "evaluation_suites.department_id"],
            name="fk_adapter_review_suite_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_review_requester",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_review_reviewer",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_review_scope"),
        sa.UniqueConstraint(
            "evaluation_id", "department_id", name="uq_adapter_review_evaluation_once"
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','archived')",
            name="ck_adapter_review_status",
        ),
        sa.CheckConstraint(
            "adapter_version > 0 AND evaluation_version > 0 AND suite_version > 0 AND version > 0",
            name="ck_adapter_review_versions",
        ),
        sa.CheckConstraint(
            "registry_attempt_version > 0 AND registry_attempt_number > 0 AND registry_manifest_sha256 ~ '^[0-9a-f]{64}$' AND registry_adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND registry_adapter_config_byte_size > 0 AND registry_adapter_model_sha256 ~ '^[0-9a-f]{64}$' AND registry_adapter_model_byte_size > 0 AND suite_artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' AND suite_canonical_cases_sha256 ~ '^[0-9a-f]{64}$' AND suite_canonical_cases_byte_size > 0 AND result_manifest_sha256 ~ '^[0-9a-f]{64}$' AND result_summary_sha256 ~ '^[0-9a-f]{64}$' AND case_results_sha256 ~ '^[0-9a-f]{64}$' AND case_results_byte_size > 0 AND code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_adapter_review_authority",
        ),
        sa.CheckConstraint(
            "reviewed_by_user_id IS NULL OR decided_at IS NOT NULL",
            name="ck_adapter_review_decision_actor",
        ),
        sa.CheckConstraint(
            "status = 'pending' OR decided_at IS NOT NULL",
            name="ck_adapter_review_decision_lifecycle",
        ),
    )
    op.create_index(
        "uq_adapter_review_pending_adapter",
        "adapter_reviews",
        ["department_id", "adapter_id", "adapter_version"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_adapter_review_approved_adapter",
        "adapter_reviews",
        ["department_id", "adapter_id", "adapter_version"],
        unique=True,
        postgresql_where=sa.text("status = 'approved' AND archived_at IS NULL"),
    )
    op.create_index(
        "ix_adapter_review_department_created",
        "adapter_reviews",
        ["department_id", "created_at", "id"],
    )

    op.create_table(
        "department_adapter_deployments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("adapter_id", sa.Uuid()),
        sa.Column("adapter_version", sa.Integer()),
        sa.Column("review_id", sa.Uuid()),
        sa.Column("review_version", sa.Integer()),
        sa.Column("evaluation_id", sa.Uuid()),
        sa.Column("evaluation_version", sa.Integer()),
        sa.Column("suite_id", sa.Uuid()),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("deployment_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_deployment_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_deployment_review_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_deployment_evaluation_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("department_id", name="uq_adapter_deployment_department"),
        sa.CheckConstraint(
            "target_kind IN ('base','adapter')", name="ck_adapter_deployment_target"
        ),
        sa.CheckConstraint(
            "deployment_version > 0 AND version > 0", name="ck_adapter_deployment_versions"
        ),
        sa.CheckConstraint(
            "(target_kind = 'base' AND adapter_id IS NULL AND adapter_version IS NULL AND review_id IS NULL AND review_version IS NULL AND evaluation_id IS NULL AND evaluation_version IS NULL AND suite_id IS NULL) OR (target_kind = 'adapter' AND adapter_id IS NOT NULL AND adapter_version > 0 AND review_id IS NOT NULL AND review_version > 0 AND evaluation_id IS NOT NULL AND evaluation_version > 0 AND suite_id IS NOT NULL)",
            name="ck_adapter_deployment_target_shape",
        ),
    )

    op.create_table(
        "adapter_deployment_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("operation_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expected_deployment_version", sa.Integer(), nullable=False),
        sa.Column("target_adapter_id", sa.Uuid()),
        sa.Column("target_adapter_version", sa.Integer()),
        sa.Column("target_review_id", sa.Uuid()),
        sa.Column("target_review_version", sa.Integer()),
        sa.Column("target_evaluation_id", sa.Uuid()),
        sa.Column("target_evaluation_version", sa.Integer()),
        sa.Column("target_retention_id", sa.Uuid()),
        sa.Column("target_retention_version", sa.Integer()),
        sa.Column("current_target_kind", sa.String(16), nullable=False),
        sa.Column("current_adapter_id", sa.Uuid()),
        sa.Column("current_adapter_version", sa.Integer()),
        sa.Column("current_deployment_version", sa.Integer(), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("registry_attempt_id", sa.Uuid()),
        sa.Column("registry_attempt_version", sa.Integer()),
        sa.Column("registry_publication_attempt_id", sa.Uuid()),
        sa.Column("registry_attempt_number", sa.Integer()),
        sa.Column("registry_execution_scope_id", sa.Uuid()),
        sa.Column("registry_manifest_sha256", sa.String(64)),
        sa.Column("registry_adapter_config_sha256", sa.String(64)),
        sa.Column("registry_adapter_config_byte_size", sa.BigInteger()),
        sa.Column("registry_adapter_model_sha256", sa.String(64)),
        sa.Column("registry_adapter_model_byte_size", sa.BigInteger()),
        sa.Column("dependency_id", sa.Uuid()),
        sa.Column("dependency_version", sa.Integer()),
        sa.Column("suite_id", sa.Uuid()),
        sa.Column("suite_version", sa.Integer()),
        sa.Column("suite_artifact_manifest_sha256", sa.String(64)),
        sa.Column("suite_canonical_cases_sha256", sa.String(64)),
        sa.Column("suite_canonical_cases_byte_size", sa.BigInteger()),
        sa.Column("result_manifest_sha256", sa.String(64)),
        sa.Column("result_summary_sha256", sa.String(64)),
        sa.Column("case_results_sha256", sa.String(64)),
        sa.Column("case_results_byte_size", sa.BigInteger()),
        sa.Column("runner_contract_version", sa.String(100)),
        sa.Column("artifact_contract_version", sa.String(100)),
        sa.Column("metric_contract_version", sa.String(100)),
        sa.Column("gate_policy_version", sa.String(100)),
        sa.Column("seed_policy_version", sa.String(100)),
        sa.Column("code_revision", sa.String(40)),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_deployment_operation_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_deployment_operation_requester",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_operation_target_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["current_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_operation_current_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_deployment_operation_target_review_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_evaluation_id", "department_id", "target_adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_deployment_operation_target_evaluation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "registry_attempt_id",
                "department_id",
                "target_adapter_id",
                "registry_publication_attempt_id",
                "registry_attempt_number",
            ],
            [
                "adapter_registry_attempts.id",
                "adapter_registry_attempts.department_id",
                "adapter_registry_attempts.adapter_id",
                "adapter_registry_attempts.publication_attempt_id",
                "adapter_registry_attempts.attempt_number",
            ],
            name="fk_adapter_deployment_operation_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dependency_id", "department_id", "target_adapter_id"],
            [
                "adapter_upstream_dependencies.id",
                "adapter_upstream_dependencies.department_id",
                "adapter_upstream_dependencies.adapter_id",
            ],
            name="fk_adapter_deployment_operation_dependency_scope",
            ondelete="RESTRICT",
        ),
        # The target retention table is created below; the exact composite FK
        # is added after that table exists.
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_deployment_operation_scope"),
        sa.CheckConstraint(
            "operation_type IN ('promote','rollback_adapter','rollback_base')",
            name="ck_adapter_deployment_operation_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_adapter_deployment_operation_status",
        ),
        sa.CheckConstraint(
            "expected_deployment_version >= 0 AND attempt_number > 0 AND version > 0",
            name="ck_adapter_deployment_operation_versions",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN (" + _quoted(_ERROR_CODES) + ")",
            name="ck_adapter_deployment_operation_error",
        ),
        sa.CheckConstraint(
            "((operation_type = 'rollback_base' AND target_adapter_id IS NULL AND target_review_id IS NULL AND target_evaluation_id IS NULL AND target_retention_id IS NULL AND registry_attempt_id IS NULL AND dependency_id IS NULL AND suite_id IS NULL) OR (operation_type IN ('promote','rollback_adapter') AND target_adapter_id IS NOT NULL AND target_review_id IS NOT NULL AND target_evaluation_id IS NOT NULL AND registry_attempt_id IS NOT NULL AND dependency_id IS NOT NULL AND suite_id IS NOT NULL AND (operation_type = 'promote' OR (target_retention_id IS NOT NULL AND target_retention_version > 0))))",
            name="ck_adapter_deployment_operation_target_shape",
        ),
        sa.CheckConstraint(
            "(status IN ('queued','running') AND finished_at IS NULL) OR (status IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL)",
            name="ck_adapter_deployment_operation_lifecycle",
        ),
    )
    op.create_index(
        "uq_adapter_deployment_operation_active",
        "adapter_deployment_operations",
        ["department_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "ix_adapter_deployment_operation_department_created",
        "adapter_deployment_operations",
        ["department_id", "created_at", "id"],
    )

    op.create_table(
        "adapter_deployment_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid()),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("deployment_version_before", sa.Integer(), nullable=False),
        sa.Column("deployment_version_after", sa.Integer(), nullable=False),
        sa.Column("from_target_kind", sa.String(16), nullable=False),
        sa.Column("from_adapter_id", sa.Uuid()),
        sa.Column("from_adapter_version", sa.Integer()),
        sa.Column("to_target_kind", sa.String(16), nullable=False),
        sa.Column("to_adapter_id", sa.Uuid()),
        sa.Column("to_adapter_version", sa.Integer()),
        sa.Column("approved_review_id", sa.Uuid()),
        sa.Column("approved_review_version", sa.Integer()),
        sa.Column("evaluation_id", sa.Uuid()),
        sa.Column("evaluation_version", sa.Integer()),
        sa.Column("suite_id", sa.Uuid()),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("rollback_retention_id", sa.Uuid()),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_deployment_event_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user_identities.id"],
            name="fk_adapter_deployment_event_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "department_id"],
            ["adapter_deployment_operations.id", "adapter_deployment_operations.department_id"],
            name="fk_adapter_deployment_event_operation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["from_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_event_from_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_event_to_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_deployment_event_review_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "department_id", "to_adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_deployment_event_evaluation_scope",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "event_type IN ('promote','rollback_adapter','rollback_base','rollback_retention_release')",
            name="ck_adapter_deployment_event_type",
        ),
        sa.CheckConstraint(
            "from_target_kind IN ('base','adapter') AND to_target_kind IN ('base','adapter')",
            name="ck_adapter_deployment_event_targets",
        ),
        sa.CheckConstraint(
            "((from_target_kind = 'base' AND from_adapter_id IS NULL AND from_adapter_version IS NULL) OR (from_target_kind = 'adapter' AND from_adapter_id IS NOT NULL AND from_adapter_version > 0)) AND ((to_target_kind = 'base' AND to_adapter_id IS NULL AND to_adapter_version IS NULL) OR (to_target_kind = 'adapter' AND to_adapter_id IS NOT NULL AND to_adapter_version > 0))",
            name="ck_adapter_deployment_event_target_shape",
        ),
        sa.CheckConstraint(
            "((to_target_kind = 'base' AND approved_review_id IS NULL AND approved_review_version IS NULL AND evaluation_id IS NULL AND evaluation_version IS NULL AND suite_id IS NULL) OR (to_target_kind = 'adapter' AND approved_review_id IS NOT NULL AND approved_review_version > 0 AND evaluation_id IS NOT NULL AND evaluation_version > 0 AND suite_id IS NOT NULL))",
            name="ck_adapter_deployment_event_authority_shape",
        ),
        sa.CheckConstraint(
            "deployment_version_before >= 0 AND ((event_type = 'rollback_retention_release' AND deployment_version_after = deployment_version_before) OR (event_type <> 'rollback_retention_release' AND deployment_version_after > deployment_version_before))",
            name="ck_adapter_deployment_event_versions",
        ),
    )
    op.create_index(
        "ix_adapter_deployment_event_department_created",
        "adapter_deployment_events",
        ["department_id", "created_at", "id"],
    )
    op.create_unique_constraint(
        "uq_adapter_deployment_event_scope", "adapter_deployment_events", ["id", "department_id"]
    )

    op.create_table(
        "adapter_rollback_retentions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_version", sa.Integer(), nullable=False),
        sa.Column("approved_review_id", sa.Uuid(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_version", sa.Integer(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("creation_event_id", sa.Uuid(), nullable=False),
        sa.Column("release_event_id", sa.Uuid()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("release_reason", sa.String(32)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_rollback_retention_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_rollback_retention_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_rollback_retention_review_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_rollback_retention_evaluation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["creation_event_id", "department_id"],
            ["adapter_deployment_events.id", "adapter_deployment_events.department_id"],
            name="fk_adapter_rollback_retention_creation_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_event_id", "department_id"],
            ["adapter_deployment_events.id", "adapter_deployment_events.department_id"],
            name="fk_adapter_rollback_retention_release_event",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_rollback_retention_scope"),
        sa.UniqueConstraint(
            "id",
            "department_id",
            "adapter_id",
            name="uq_adapter_rollback_retention_adapter_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active','released')", name="ck_adapter_rollback_retention_status"
        ),
        sa.CheckConstraint(
            "adapter_version > 0 AND review_version > 0 AND evaluation_version > 0 AND version > 0",
            name="ck_adapter_rollback_retention_versions",
        ),
        sa.CheckConstraint(
            "release_reason IS NULL OR release_reason IN ('reactivated','manual_release')",
            name="ck_adapter_rollback_retention_reason",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND released_at IS NULL AND release_event_id IS NULL) OR (status = 'released' AND released_at IS NOT NULL AND release_event_id IS NOT NULL)",
            name="ck_adapter_rollback_retention_lifecycle",
        ),
    )
    op.create_index(
        "uq_adapter_rollback_retention_active",
        "adapter_rollback_retentions",
        ["department_id", "adapter_id", "adapter_version"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_adapter_rollback_retention_department_created",
        "adapter_rollback_retentions",
        ["department_id", "created_at", "id"],
    )
    op.create_foreign_key(
        "fk_adapter_deployment_operation_target_retention_exact",
        "adapter_deployment_operations",
        "adapter_rollback_retentions",
        ["target_retention_id", "department_id", "target_adapter_id"],
        ["id", "department_id", "adapter_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_adapter_deployment_event_rollback_retention",
        "adapter_deployment_events",
        "adapter_rollback_retentions",
        ["rollback_retention_id", "department_id"],
        ["id", "department_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_adapter_deployment_event_rollback_retention",
        "adapter_deployment_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_adapter_deployment_operation_target_retention_exact",
        "adapter_deployment_operations",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_adapter_rollback_retention_department_created", table_name="adapter_rollback_retentions"
    )
    op.drop_index("uq_adapter_rollback_retention_active", table_name="adapter_rollback_retentions")
    op.drop_table("adapter_rollback_retentions")
    op.drop_index(
        "ix_adapter_deployment_event_department_created", table_name="adapter_deployment_events"
    )
    op.drop_constraint(
        "uq_adapter_deployment_event_scope", "adapter_deployment_events", type_="unique"
    )
    op.drop_table("adapter_deployment_events")
    op.drop_index(
        "ix_adapter_deployment_operation_department_created",
        table_name="adapter_deployment_operations",
    )
    op.drop_index(
        "uq_adapter_deployment_operation_active", table_name="adapter_deployment_operations"
    )
    op.drop_table("adapter_deployment_operations")
    op.drop_table("department_adapter_deployments")
    op.drop_index("ix_adapter_review_department_created", table_name="adapter_reviews")
    op.drop_index("uq_adapter_review_approved_adapter", table_name="adapter_reviews")
    op.drop_index("uq_adapter_review_pending_adapter", table_name="adapter_reviews")
    op.drop_table("adapter_reviews")

# ruff: noqa: E501
"""Add Phase 12.2 adapter-target evaluation metadata.

The migration stores only tenant-scoped authority, lifecycle state, numeric
evidence and artifact digests.  Questions, answers, prompts, evidence text,
vectors and adapter bytes remain outside PostgreSQL.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015_phase12_adapter_evaluation"
down_revision = "0014_phase12_adapter_purge"
branch_labels = None
depends_on = None

_ERROR_CODES = (
    "adapter_unavailable",
    "adapter_authority_changed",
    "adapter_artifact_missing",
    "adapter_artifact_mismatch",
    "suite_unavailable",
    "suite_authority_changed",
    "department_unavailable",
    "requester_unauthorized",
    "qdrant_unavailable",
    "retrieval_authority_failed",
    "source_artifact_missing",
    "source_artifact_mismatch",
    "base_runtime_unavailable",
    "base_runtime_timeout",
    "candidate_runtime_unavailable",
    "candidate_runtime_timeout",
    "candidate_adapter_load_failed",
    "invalid_generation_response",
    "invalid_citation",
    "result_publication_failed",
    "claim_lost",
    "worker_shutdown",
    "cancelled",
    "database_unavailable",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join("'" + value + "'" for value in values)


def _metric_columns(nullable: bool = False) -> list[sa.Column]:
    return [
        sa.Column(name, sa.Numeric(20, 18), nullable=nullable)
        for name in (
            "retrieval_recall_at_5",
            "retrieval_recall_at_10",
            "retrieval_recall_at_20",
            "retrieval_mrr_at_20",
            "answer_status_accuracy",
            "citation_precision",
            "citation_recall",
            "normalized_exact_match",
            "character_f1",
            "invalid_contract_rate",
        )
    ]


def upgrade() -> None:
    # The Phase 12.1 dependency table already has a globally unique primary
    # key, but this composite key lets the new run foreign key enforce the
    # department/adapter scope at the database boundary as well.
    op.create_unique_constraint(
        "uq_adapter_dependency_scope",
        "adapter_upstream_dependencies",
        ["id", "department_id", "adapter_id"],
    )
    op.create_table(
        "adapter_evaluation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("gate_status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("expected_adapter_version", sa.Integer(), nullable=False),
        sa.Column("adapter_version", sa.Integer(), nullable=False),
        sa.Column("registry_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("registry_attempt_version", sa.Integer(), nullable=False),
        sa.Column("registry_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("registry_attempt_number", sa.Integer(), nullable=False),
        sa.Column("registry_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("registry_adapter_config_sha256", sa.String(64), nullable=False),
        sa.Column("registry_adapter_config_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("registry_adapter_model_sha256", sa.String(64), nullable=False),
        sa.Column("registry_adapter_model_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_version", sa.Integer(), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("suite_artifact_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("suite_canonical_cases_sha256", sa.String(64), nullable=False),
        sa.Column("suite_canonical_cases_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("retrieval_recall_at_5_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("retrieval_mrr_at_20_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("answer_status_accuracy_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("citation_precision_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("citation_recall_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("normalized_exact_match_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("character_f1_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("invalid_contract_rate_max", sa.Numeric(8, 6), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("runner_contract_version", sa.String(100), nullable=False),
        sa.Column("artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("metric_contract_version", sa.String(100), nullable=False),
        sa.Column("gate_policy_version", sa.String(100), nullable=False),
        sa.Column("seed_policy_version", sa.String(100), nullable=False),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("base_seed", sa.BigInteger(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("completed_case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_publication_attempt_id", sa.Uuid()),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result_manifest_sha256", sa.String(64)),
        sa.Column("result_summary_sha256", sa.String(64)),
        sa.Column("case_results_sha256", sa.String(64)),
        sa.Column("case_results_byte_size", sa.BigInteger()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_evaluation_run_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_evaluation_run_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id", "department_id"],
            ["evaluation_suites.id", "evaluation_suites.department_id"],
            name="fk_adapter_evaluation_run_suite_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_evaluation_run_requester",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dependency_id", "department_id", "adapter_id"],
            [
                "adapter_upstream_dependencies.id",
                "adapter_upstream_dependencies.department_id",
                "adapter_upstream_dependencies.adapter_id",
            ],
            name="fk_adapter_evaluation_run_dependency",
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
            name="fk_adapter_evaluation_run_registry_attempt",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "id", "department_id", "adapter_id", "suite_id", name="uq_adapter_evaluation_run_scope"
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_adapter_evaluation_run_status",
        ),
        sa.CheckConstraint(
            "gate_status IN ('pending','passed','failed')",
            name="ck_adapter_evaluation_run_gate_status",
        ),
        sa.CheckConstraint(
            "runner_contract_version = 'phase12-adapter-evaluation-v1' AND artifact_contract_version = 'phase12-adapter-evaluation-artifact-v1' AND metric_contract_version = 'phase9-deterministic-metrics-v1' AND gate_policy_version = 'phase9-quality-gates-v1' AND seed_policy_version = 'phase12-adapter-evaluation-seed-v1'",
            name="ck_adapter_evaluation_run_contracts",
        ),
        sa.CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca'",
            name="ck_adapter_evaluation_run_model",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN (" + _quoted(_ERROR_CODES) + ")",
            name="ck_adapter_evaluation_run_error_code",
        ),
        sa.CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$' AND base_seed BETWEEN 0 AND 9223372036854775807 AND expected_adapter_version > 0 AND adapter_version > 0 AND registry_attempt_version > 0 AND registry_attempt_number > 0 AND dependency_version > 0 AND suite_version > 0 AND case_count BETWEEN 1 AND 500 AND completed_case_count BETWEEN 0 AND case_count AND attempt_number > 0 AND version > 0",
            name="ck_adapter_evaluation_run_versions_counts",
        ),
        sa.CheckConstraint(
            "registry_manifest_sha256 ~ '^[0-9a-f]{64}$' AND registry_adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND registry_adapter_model_sha256 ~ '^[0-9a-f]{64}$' AND registry_adapter_config_byte_size > 0 AND registry_adapter_model_byte_size > 0 AND suite_artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' AND suite_canonical_cases_sha256 ~ '^[0-9a-f]{64}$' AND suite_canonical_cases_byte_size > 0",
            name="ck_adapter_evaluation_run_authority_digests",
        ),
        sa.CheckConstraint(
            "retrieval_recall_at_5_min BETWEEN 0 AND 1 AND retrieval_mrr_at_20_min BETWEEN 0 AND 1 AND answer_status_accuracy_min BETWEEN 0 AND 1 AND citation_precision_min BETWEEN 0 AND 1 AND citation_recall_min BETWEEN 0 AND 1 AND normalized_exact_match_min BETWEEN 0 AND 1 AND character_f1_min BETWEEN 0 AND 1 AND invalid_contract_rate_max BETWEEN 0 AND 1",
            name="ck_adapter_evaluation_run_gate_ranges",
        ),
        sa.CheckConstraint(
            "(result_manifest_sha256 IS NULL OR result_manifest_sha256 ~ '^[0-9a-f]{64}$') AND (result_summary_sha256 IS NULL OR result_summary_sha256 ~ '^[0-9a-f]{64}$') AND (case_results_sha256 IS NULL OR case_results_sha256 ~ '^[0-9a-f]{64}$') AND (case_results_byte_size IS NULL OR case_results_byte_size > 0)",
            name="ck_adapter_evaluation_run_artifacts",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND gate_status = 'pending' AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND claimed_at IS NULL AND started_at IS NULL AND finished_at IS NULL AND cancellation_requested_at IS NULL AND cancelled_at IS NULL AND result_publication_attempt_id IS NULL AND completed_case_count = 0) OR status <> 'queued'",
            name="ck_adapter_evaluation_run_queued_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND gate_status = 'pending' AND worker_id IS NOT NULL AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL AND claimed_at IS NOT NULL AND started_at IS NOT NULL AND finished_at IS NULL AND cancelled_at IS NULL AND result_publication_attempt_id IS NOT NULL) OR status <> 'running'",
            name="ck_adapter_evaluation_run_running_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND gate_status IN ('passed','failed') AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL AND result_manifest_sha256 IS NOT NULL AND result_summary_sha256 IS NOT NULL AND case_results_sha256 IS NOT NULL AND completed_case_count = case_count AND cancellation_requested_at IS NULL AND cancelled_at IS NULL AND error_code IS NULL) OR status <> 'succeeded'",
            name="ck_adapter_evaluation_run_succeeded_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND gate_status = 'pending' AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL AND cancelled_at IS NULL AND error_code IS NOT NULL) OR status <> 'failed'",
            name="ck_adapter_evaluation_run_failed_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND gate_status = 'pending' AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL AND cancellation_requested_at IS NOT NULL AND cancelled_at IS NOT NULL AND error_code = 'cancelled') OR status <> 'cancelled'",
            name="ck_adapter_evaluation_run_cancelled_lifecycle",
        ),
    )
    op.create_index(
        "uq_adapter_evaluation_run_active",
        "adapter_evaluation_runs",
        ["department_id", "adapter_id", "suite_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "ix_adapter_evaluation_run_department_status_created",
        "adapter_evaluation_runs",
        ["department_id", "status", "created_at"],
    )

    op.create_table(
        "adapter_evaluation_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Uuid()),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("result_manifest_sha256", sa.String(64)),
        sa.Column("result_summary_sha256", sa.String(64)),
        sa.Column("case_results_sha256", sa.String(64)),
        sa.Column("case_results_byte_size", sa.BigInteger()),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_evaluation_attempt_run_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "id", "department_id", "run_id", name="uq_adapter_evaluation_attempt_scope"
        ),
        sa.UniqueConstraint(
            "run_id", "attempt_number", name="uq_adapter_evaluation_attempt_number"
        ),
        sa.UniqueConstraint(
            "publication_attempt_id", name="uq_adapter_evaluation_attempt_publication"
        ),
        sa.CheckConstraint(
            "status IN ('running','reclaimed','succeeded','failed','cancelled')",
            name="ck_adapter_evaluation_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 AND version > 0", name="ck_adapter_evaluation_attempt_versions"
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN (" + _quoted(_ERROR_CODES) + ")",
            name="ck_adapter_evaluation_attempt_error_code",
        ),
        sa.CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$' AND ((status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL AND finished_at IS NULL) OR (status IN ('reclaimed','failed','cancelled') AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL AND error_code IS NOT NULL) OR (status = 'succeeded' AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL AND error_code IS NULL AND result_manifest_sha256 IS NOT NULL AND result_summary_sha256 IS NOT NULL AND case_results_sha256 IS NOT NULL AND case_results_byte_size > 0))",
            name="ck_adapter_evaluation_attempt_lifecycle",
        ),
        sa.CheckConstraint(
            "(result_manifest_sha256 IS NULL OR result_manifest_sha256 ~ '^[0-9a-f]{64}$') AND (result_summary_sha256 IS NULL OR result_summary_sha256 ~ '^[0-9a-f]{64}$') AND (case_results_sha256 IS NULL OR case_results_sha256 ~ '^[0-9a-f]{64}$') AND (case_results_byte_size IS NULL OR case_results_byte_size > 0)",
            name="ck_adapter_evaluation_attempt_artifacts",
        ),
    )
    op.create_index(
        "uq_adapter_evaluation_attempt_active",
        "adapter_evaluation_attempts",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "adapter_evaluation_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("adapter_version", sa.Integer(), nullable=False),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("metric_contract_version", sa.String(100), nullable=False),
        sa.Column("gate_policy_version", sa.String(100), nullable=False),
        sa.Column("seed_policy_version", sa.String(100), nullable=False),
        sa.Column("gate_status", sa.String(16), nullable=False),
        sa.Column("failed_gate_count", sa.Integer(), nullable=False),
        *_metric_columns(),
        *[
            sa.Column("delta_" + name, sa.Numeric(20, 18))
            for name in (
                "retrieval_recall_at_5",
                "retrieval_recall_at_10",
                "retrieval_recall_at_20",
                "retrieval_mrr_at_20",
                "answer_status_accuracy",
                "citation_precision",
                "citation_recall",
                "normalized_exact_match",
                "character_f1",
                "invalid_contract_rate",
            )
        ],
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_evaluation_evidence_run_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "target", name="uq_adapter_evaluation_evidence_target"),
        sa.CheckConstraint(
            "target IN ('baseline','candidate')", name="ck_adapter_evaluation_evidence_target"
        ),
        sa.CheckConstraint(
            "gate_status IN ('passed','failed') AND failed_gate_count BETWEEN 0 AND 8",
            name="ck_adapter_evaluation_evidence_gate",
        ),
        sa.CheckConstraint(
            "adapter_version > 0 AND base_model_id = 'Qwen/Qwen3-0.6B' AND base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca' AND metric_contract_version = 'phase9-deterministic-metrics-v1' AND gate_policy_version = 'phase9-quality-gates-v1' AND seed_policy_version = 'phase12-adapter-evaluation-seed-v1'",
            name="ck_adapter_evaluation_evidence_contract",
        ),
        sa.CheckConstraint(
            "retrieval_recall_at_5 BETWEEN 0 AND 1 AND retrieval_recall_at_10 BETWEEN 0 AND 1 AND retrieval_recall_at_20 BETWEEN 0 AND 1 AND retrieval_mrr_at_20 BETWEEN 0 AND 1 AND answer_status_accuracy BETWEEN 0 AND 1 AND citation_precision BETWEEN 0 AND 1 AND citation_recall BETWEEN 0 AND 1 AND normalized_exact_match BETWEEN 0 AND 1 AND character_f1 BETWEEN 0 AND 1 AND invalid_contract_rate BETWEEN 0 AND 1",
            name="ck_adapter_evaluation_evidence_metric_ranges",
        ),
    )
    op.create_index(
        "ix_adapter_evaluation_evidence_department_run",
        "adapter_evaluation_evidence",
        ["department_id", "run_id"],
    )

    op.create_table(
        "adapter_evaluation_case_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("expected_status", sa.String(32), nullable=False),
        sa.Column("actual_status", sa.String(32), nullable=False),
        sa.Column("relevant_chunk_count", sa.Integer(), nullable=False),
        sa.Column("retrieval_candidate_count", sa.Integer(), nullable=False),
        sa.Column("retrieved_relevant_at_5", sa.Integer(), nullable=False),
        sa.Column("retrieved_relevant_at_10", sa.Integer(), nullable=False),
        sa.Column("retrieved_relevant_at_20", sa.Integer(), nullable=False),
        sa.Column("reciprocal_rank_at_20", sa.Numeric(20, 18), nullable=False),
        sa.Column("answer_status_correct", sa.Boolean(), nullable=False),
        sa.Column("cited_count", sa.Integer(), nullable=False),
        sa.Column("cited_relevant_count", sa.Integer(), nullable=False),
        sa.Column("citation_precision", sa.Numeric(20, 18), nullable=False),
        sa.Column("citation_recall", sa.Numeric(20, 18), nullable=False),
        sa.Column("normalized_exact_match", sa.Numeric(1, 0), nullable=False),
        sa.Column("character_f1", sa.Numeric(20, 18), nullable=False),
        sa.Column("answer_contract_valid", sa.Boolean(), nullable=False),
        sa.Column("case_gate_passed", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_evaluation_case_result_run_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "run_id", "target", "case_id", name="uq_adapter_evaluation_case_target"
        ),
        sa.CheckConstraint(
            "target IN ('baseline','candidate')", name="ck_adapter_evaluation_case_target"
        ),
        sa.CheckConstraint(
            "actual_status IN ('answered','insufficient_information','failed')",
            name="ck_adapter_evaluation_case_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN (" + _quoted(_ERROR_CODES) + ")",
            name="ck_adapter_evaluation_case_error_code",
        ),
        sa.CheckConstraint(
            "expected_status IN ('answered','insufficient_information') AND ((expected_status = 'answered' AND relevant_chunk_count BETWEEN 1 AND 8) OR (expected_status = 'insufficient_information' AND relevant_chunk_count = 0)) AND ((actual_status = 'failed' AND error_code IS NOT NULL) OR (actual_status <> 'failed' AND error_code IS NULL))",
            name="ck_adapter_evaluation_case_expected_lifecycle",
        ),
        sa.CheckConstraint(
            "retrieval_candidate_count >= 0 AND retrieved_relevant_at_5 >= 0 AND retrieved_relevant_at_10 >= retrieved_relevant_at_5 AND retrieved_relevant_at_20 >= retrieved_relevant_at_10 AND retrieved_relevant_at_20 <= relevant_chunk_count AND cited_count >= 0 AND cited_relevant_count BETWEEN 0 AND cited_count AND cited_relevant_count <= relevant_chunk_count AND reciprocal_rank_at_20 BETWEEN 0 AND 1 AND citation_precision BETWEEN 0 AND 1 AND citation_recall BETWEEN 0 AND 1 AND normalized_exact_match BETWEEN 0 AND 1 AND character_f1 BETWEEN 0 AND 1",
            name="ck_adapter_evaluation_case_numeric_ranges",
        ),
    )
    op.create_index(
        "ix_adapter_evaluation_case_department_run",
        "adapter_evaluation_case_results",
        ["department_id", "run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_adapter_evaluation_case_department_run", table_name="adapter_evaluation_case_results"
    )
    op.drop_table("adapter_evaluation_case_results")
    op.drop_index(
        "ix_adapter_evaluation_evidence_department_run", table_name="adapter_evaluation_evidence"
    )
    op.drop_table("adapter_evaluation_evidence")
    op.drop_index("uq_adapter_evaluation_attempt_active", table_name="adapter_evaluation_attempts")
    op.drop_table("adapter_evaluation_attempts")
    op.drop_index(
        "ix_adapter_evaluation_run_department_status_created", table_name="adapter_evaluation_runs"
    )
    op.drop_index("uq_adapter_evaluation_run_active", table_name="adapter_evaluation_runs")
    op.drop_table("adapter_evaluation_runs")
    op.drop_constraint(
        "uq_adapter_dependency_scope", "adapter_upstream_dependencies", type_="unique"
    )

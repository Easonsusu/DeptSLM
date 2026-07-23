"""Add department-scoped evaluation metadata and worker queue.

Revision ID: 0007_phase9_evaluation_runner
Revises: 0006_phase8_rag_feedback
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_phase9_evaluation_runner"
down_revision = "0006_phase8_rag_feedback"
branch_labels = None
depends_on = None

ERROR_CODES = (
    "suite_artifact_missing",
    "suite_artifact_mismatch",
    "suite_contract_invalid",
    "suite_source_stale",
    "department_unavailable",
    "requester_unauthorized",
    "database_unavailable",
    "qdrant_unavailable",
    "retrieval_authority_failed",
    "source_artifact_missing",
    "source_artifact_mismatch",
    "runtime_unavailable",
    "runtime_timeout",
    "invalid_query_embedding",
    "generation_failed",
    "invalid_generation_response",
    "invalid_citation",
    "result_publication_failed",
    "claim_lost",
    "cancelled",
)


def _error_values() -> str:
    return ",".join(f"'{value}'" for value in ERROR_CODES)


def upgrade() -> None:
    op.create_table(
        "evaluation_suites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("imported_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("suite_contract_version", sa.String(100), nullable=False),
        sa.Column("artifact_contract_version", sa.String(100), nullable=False),
        sa.Column("metric_contract_version", sa.String(100), nullable=False),
        sa.Column("answer_normalization_version", sa.String(100), nullable=False),
        sa.Column("gate_policy_version", sa.String(100), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("answered_case_count", sa.Integer(), nullable=False),
        sa.Column("insufficient_case_count", sa.Integer(), nullable=False),
        sa.Column("artifact_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_cases_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_cases_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("retrieval_recall_at_5_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("retrieval_mrr_at_20_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("answer_status_accuracy_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("citation_precision_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("citation_recall_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("normalized_exact_match_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("character_f1_min", sa.Numeric(8, 6), nullable=False),
        sa.Column("invalid_contract_rate_max", sa.Numeric(8, 6), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_evaluation_suite_status"),
        sa.CheckConstraint(
            "suite_contract_version = 'phase9-evaluation-suite-v1'",
            name="ck_evaluation_suite_contract",
        ),
        sa.CheckConstraint(
            "artifact_contract_version = 'phase9-evaluation-artifact-v1'",
            name="ck_evaluation_suite_artifact_contract",
        ),
        sa.CheckConstraint(
            "metric_contract_version = 'phase9-deterministic-metrics-v1'",
            name="ck_evaluation_suite_metric_contract",
        ),
        sa.CheckConstraint(
            "answer_normalization_version = 'phase9-answer-normalization-v1'",
            name="ck_evaluation_suite_normalization_contract",
        ),
        sa.CheckConstraint(
            "gate_policy_version = 'phase9-quality-gates-v1'",
            name="ck_evaluation_suite_gate_contract",
        ),
        sa.CheckConstraint("case_count BETWEEN 1 AND 500", name="ck_evaluation_suite_case_count"),
        sa.CheckConstraint(
            "answered_case_count >= 0 AND insufficient_case_count >= 0 "
            "AND answered_case_count + insufficient_case_count = case_count",
            name="ck_evaluation_suite_case_totals",
        ),
        sa.CheckConstraint(
            "answered_case_count > 0", name="ck_evaluation_suite_applicable_metrics"
        ),
        sa.CheckConstraint(
            "artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND canonical_cases_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_suite_hashes",
        ),
        sa.CheckConstraint(
            "canonical_cases_byte_size BETWEEN 1 AND 16777216",
            name="ck_evaluation_suite_artifact_size",
        ),
        sa.CheckConstraint(
            "retrieval_recall_at_5_min BETWEEN 0 AND 1 "
            "AND retrieval_mrr_at_20_min BETWEEN 0 AND 1 "
            "AND answer_status_accuracy_min BETWEEN 0 AND 1 "
            "AND citation_precision_min BETWEEN 0 AND 1 "
            "AND citation_recall_min BETWEEN 0 AND 1 "
            "AND normalized_exact_match_min BETWEEN 0 AND 1 "
            "AND character_f1_min BETWEEN 0 AND 1 "
            "AND invalid_contract_rate_max BETWEEN 0 AND 1",
            name="ck_evaluation_suite_gate_ranges",
        ),
        sa.CheckConstraint("version > 0", name="ck_evaluation_suite_version"),
        sa.CheckConstraint(
            "(status = 'active' AND archived_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL)",
            name="ck_evaluation_suite_lifecycle",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_evaluation_suite_department"),
    )
    op.create_index(
        "ix_evaluation_suite_department_status_created",
        "evaluation_suites",
        ["department_id", "status", "created_at"],
    )

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("gate_status", sa.String(16), nullable=False),
        sa.Column("runner_contract_version", sa.String(100), nullable=False),
        sa.Column("code_revision", sa.String(40), nullable=False),
        sa.Column("query_embedding_pipeline_version", sa.String(100), nullable=False),
        sa.Column("query_embedding_model_id", sa.String(200), nullable=False),
        sa.Column("query_embedding_model_revision", sa.String(64), nullable=False),
        sa.Column("query_embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("query_embedding_distance", sa.String(16), nullable=False),
        sa.Column("generation_model_id", sa.String(200), nullable=False),
        sa.Column("generation_model_revision", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("answer_contract_version", sa.String(100), nullable=False),
        sa.Column("qdrant_collection", sa.String(128), nullable=False),
        sa.Column("vector_schema_version", sa.String(100), nullable=False),
        sa.Column("base_seed", sa.BigInteger(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("completed_case_count", sa.Integer(), nullable=False),
        sa.Column("answered_case_count", sa.Integer(), nullable=False),
        sa.Column("insufficient_case_count", sa.Integer(), nullable=False),
        sa.Column("retrieval_recall_at_5", sa.Numeric(20, 18), nullable=True),
        sa.Column("retrieval_recall_at_10", sa.Numeric(20, 18), nullable=True),
        sa.Column("retrieval_recall_at_20", sa.Numeric(20, 18), nullable=True),
        sa.Column("retrieval_mrr_at_20", sa.Numeric(20, 18), nullable=True),
        sa.Column("answer_status_accuracy", sa.Numeric(20, 18), nullable=True),
        sa.Column("citation_precision", sa.Numeric(20, 18), nullable=True),
        sa.Column("citation_recall", sa.Numeric(20, 18), nullable=True),
        sa.Column("normalized_exact_match", sa.Numeric(20, 18), nullable=True),
        sa.Column("character_f1", sa.Numeric(20, 18), nullable=True),
        sa.Column("invalid_contract_rate", sa.Numeric(20, 18), nullable=True),
        sa.Column("failed_gate_count", sa.Integer(), nullable=True),
        sa.Column("result_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("result_summary_sha256", sa.String(64), nullable=True),
        sa.Column("case_results_sha256", sa.String(64), nullable=True),
        sa.Column("case_results_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_evaluation_run_status",
        ),
        sa.CheckConstraint(
            "gate_status IN ('pending','passed','failed')",
            name="ck_evaluation_run_gate_status",
        ),
        sa.CheckConstraint(
            "runner_contract_version = 'phase9-evaluation-runner-v1'",
            name="ck_evaluation_run_runner_contract",
        ),
        sa.CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$'", name="ck_evaluation_run_code_revision"
        ),
        sa.CheckConstraint(
            "query_embedding_pipeline_version = 'phase7-qwen3-query-embedding-v1' "
            "AND query_embedding_model_id = 'Qwen/Qwen3-Embedding-0.6B' "
            "AND query_embedding_model_revision = "
            "'d23109d65ca9fdf61eef614209744716f337f50f' "
            "AND query_embedding_dimension = 1024 "
            "AND query_embedding_distance = 'cosine'",
            name="ck_evaluation_run_embedding_contract",
        ),
        sa.CheckConstraint(
            "generation_model_id = 'Qwen/Qwen3-0.6B' "
            "AND generation_model_revision = "
            "'c1899de289a04d12100db370d81485cdf75e47ca' "
            "AND prompt_version = 'phase7-grounded-answer-prompt-v1' "
            "AND answer_contract_version = 'phase7-grounded-answer-v1'",
            name="ck_evaluation_run_generation_contract",
        ),
        sa.CheckConstraint(
            "qdrant_collection = 'deptslm_chunks_qwen3_0_6b_1024_v1' "
            "AND vector_schema_version = 'phase6-qdrant-chunks-v1'",
            name="ck_evaluation_run_vector_contract",
        ),
        sa.CheckConstraint(
            "base_seed BETWEEN 0 AND 9223372036854775807",
            name="ck_evaluation_run_seed",
        ),
        sa.CheckConstraint(
            "case_count BETWEEN 1 AND 500 AND completed_case_count BETWEEN 0 AND case_count "
            "AND answered_case_count >= 0 AND insufficient_case_count >= 0 "
            "AND answered_case_count + insufficient_case_count <= completed_case_count",
            name="ck_evaluation_run_counts",
        ),
        sa.CheckConstraint(
            "(retrieval_recall_at_5 IS NULL OR retrieval_recall_at_5 BETWEEN 0 AND 1) "
            "AND (retrieval_recall_at_10 IS NULL OR retrieval_recall_at_10 BETWEEN 0 AND 1) "
            "AND (retrieval_recall_at_20 IS NULL OR retrieval_recall_at_20 BETWEEN 0 AND 1) "
            "AND (retrieval_mrr_at_20 IS NULL OR retrieval_mrr_at_20 BETWEEN 0 AND 1) "
            "AND (answer_status_accuracy IS NULL OR answer_status_accuracy BETWEEN 0 AND 1) "
            "AND (citation_precision IS NULL OR citation_precision BETWEEN 0 AND 1) "
            "AND (citation_recall IS NULL OR citation_recall BETWEEN 0 AND 1) "
            "AND (normalized_exact_match IS NULL OR normalized_exact_match BETWEEN 0 AND 1) "
            "AND (character_f1 IS NULL OR character_f1 BETWEEN 0 AND 1) "
            "AND (invalid_contract_rate IS NULL OR invalid_contract_rate BETWEEN 0 AND 1)",
            name="ck_evaluation_run_metric_ranges",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_error_values()})",
            name="ck_evaluation_run_error_code",
        ),
        sa.CheckConstraint(
            "(result_manifest_sha256 IS NULL OR "
            "result_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(result_summary_sha256 IS NULL OR result_summary_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (case_results_sha256 IS NULL OR case_results_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (case_results_byte_size IS NULL OR case_results_byte_size > 0)",
            name="ck_evaluation_run_artifacts",
        ),
        sa.CheckConstraint("attempt_number > 0 AND version > 0", name="ck_evaluation_run_versions"),
        sa.CheckConstraint(
            "(status = 'queued' AND gate_status = 'pending' "
            "AND worker_id IS NULL AND claim_token IS NULL AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND cancellation_requested_at IS NULL "
            "AND completed_case_count = 0 AND answered_case_count = 0 "
            "AND insufficient_case_count = 0 AND failed_gate_count IS NULL "
            "AND result_manifest_sha256 IS NULL AND result_summary_sha256 IS NULL "
            "AND case_results_sha256 IS NULL AND case_results_byte_size IS NULL "
            "AND error_code IS NULL) OR status <> 'queued'",
            name="ck_evaluation_run_queued_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND gate_status = 'pending' "
            "AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND failed_gate_count IS NULL AND result_manifest_sha256 IS NULL "
            "AND result_summary_sha256 IS NULL AND case_results_sha256 IS NULL "
            "AND case_results_byte_size IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_evaluation_run_running_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND gate_status IN ('passed','failed') "
            "AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND completed_case_count = case_count "
            "AND cancellation_requested_at IS NULL "
            "AND answered_case_count + insufficient_case_count = case_count "
            "AND retrieval_recall_at_5 IS NOT NULL AND retrieval_recall_at_10 IS NOT NULL "
            "AND retrieval_recall_at_20 IS NOT NULL AND retrieval_mrr_at_20 IS NOT NULL "
            "AND answer_status_accuracy IS NOT NULL AND citation_precision IS NOT NULL "
            "AND citation_recall IS NOT NULL AND normalized_exact_match IS NOT NULL "
            "AND character_f1 IS NOT NULL AND invalid_contract_rate IS NOT NULL "
            "AND failed_gate_count IS NOT NULL AND failed_gate_count BETWEEN 0 AND 8 "
            "AND result_manifest_sha256 IS NOT NULL AND result_summary_sha256 IS NOT NULL "
            "AND case_results_sha256 IS NOT NULL AND case_results_byte_size IS NOT NULL "
            "AND error_code IS NULL) OR status <> 'succeeded'",
            name="ck_evaluation_run_succeeded_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND gate_status = 'pending' "
            "AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND error_code IS NOT NULL "
            "AND cancellation_requested_at IS NULL "
            "AND failed_gate_count IS NULL AND result_manifest_sha256 IS NULL "
            "AND result_summary_sha256 IS NULL AND case_results_sha256 IS NULL "
            "AND case_results_byte_size IS NULL) OR status <> 'failed'",
            name="ck_evaluation_run_failed_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND gate_status = 'pending' "
            "AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND error_code = 'cancelled' "
            "AND cancellation_requested_at IS NOT NULL "
            "AND failed_gate_count IS NULL AND result_manifest_sha256 IS NULL "
            "AND result_summary_sha256 IS NULL AND case_results_sha256 IS NULL "
            "AND case_results_byte_size IS NULL) OR status <> 'cancelled'",
            name="ck_evaluation_run_cancelled_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id", "department_id"],
            ["evaluation_suites.id", "evaluation_suites.department_id"],
            name="fk_evaluation_run_suite_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "department_id", "suite_id", name="uq_evaluation_run_department_suite"
        ),
    )
    op.create_index(
        "ix_evaluation_run_department_status_created",
        "evaluation_runs",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "ix_evaluation_run_suite_created",
        "evaluation_runs",
        ["department_id", "suite_id", "created_at"],
    )

    op.create_table(
        "evaluation_case_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("expected_status", sa.String(32), nullable=False),
        sa.Column("actual_status", sa.String(32), nullable=False),
        sa.Column("relevant_chunk_count", sa.Integer(), nullable=False),
        sa.Column("retrieved_relevant_at_5", sa.Integer(), nullable=False),
        sa.Column("retrieved_relevant_at_10", sa.Integer(), nullable=False),
        sa.Column("retrieved_relevant_at_20", sa.Integer(), nullable=False),
        sa.Column("reciprocal_rank_at_20", sa.Numeric(20, 18), nullable=False),
        sa.Column("status_correct", sa.Boolean(), nullable=False),
        sa.Column("cited_count", sa.Integer(), nullable=False),
        sa.Column("cited_relevant_count", sa.Integer(), nullable=False),
        sa.Column("citation_precision", sa.Numeric(20, 18), nullable=False),
        sa.Column("citation_recall", sa.Numeric(20, 18), nullable=False),
        sa.Column("normalized_exact_match", sa.Numeric(1, 0), nullable=False),
        sa.Column("character_f1", sa.Numeric(20, 18), nullable=False),
        sa.Column("answer_contract_valid", sa.Boolean(), nullable=False),
        sa.Column("case_gate_passed", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "expected_status IN ('answered','insufficient_information')",
            name="ck_evaluation_case_expected_status",
        ),
        sa.CheckConstraint(
            "actual_status IN ('answered','insufficient_information','failed')",
            name="ck_evaluation_case_actual_status",
        ),
        sa.CheckConstraint(
            "relevant_chunk_count >= 0 AND retrieved_relevant_at_5 >= 0 "
            "AND retrieved_relevant_at_10 >= retrieved_relevant_at_5 "
            "AND retrieved_relevant_at_20 >= retrieved_relevant_at_10 "
            "AND retrieved_relevant_at_20 <= relevant_chunk_count "
            "AND cited_count >= 0 AND cited_relevant_count BETWEEN 0 AND cited_count "
            "AND cited_relevant_count <= relevant_chunk_count",
            name="ck_evaluation_case_counts",
        ),
        sa.CheckConstraint(
            "reciprocal_rank_at_20 BETWEEN 0 AND 1 "
            "AND citation_precision BETWEEN 0 AND 1 "
            "AND citation_recall BETWEEN 0 AND 1 "
            "AND normalized_exact_match IN (0,1) "
            "AND character_f1 BETWEEN 0 AND 1",
            name="ck_evaluation_case_metrics",
        ),
        sa.CheckConstraint(
            "(expected_status = 'answered' AND relevant_chunk_count BETWEEN 1 AND 8) OR "
            "(expected_status = 'insufficient_information' AND relevant_chunk_count = 0)",
            name="ck_evaluation_case_expected_contract",
        ),
        sa.CheckConstraint(
            "(actual_status = 'failed' AND error_code IS NOT NULL) OR "
            "(actual_status <> 'failed' AND error_code IS NULL)",
            name="ck_evaluation_case_error_lifecycle",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_error_values()})",
            name="ck_evaluation_case_error_code",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "department_id", "suite_id"],
            [
                "evaluation_runs.id",
                "evaluation_runs.department_id",
                "evaluation_runs.suite_id",
            ],
            name="fk_evaluation_case_result_run_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "case_id"),
    )
    op.create_index(
        "ix_evaluation_case_result_department_run",
        "evaluation_case_results",
        ["department_id", "run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_case_result_department_run", table_name="evaluation_case_results")
    op.drop_table("evaluation_case_results")
    op.drop_index("ix_evaluation_run_suite_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_run_department_status_created", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("ix_evaluation_suite_department_status_created", table_name="evaluation_suites")
    op.drop_table("evaluation_suites")

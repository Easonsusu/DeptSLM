"""Add content-free grounded-answer run and citation metadata.

Revision ID: 0005_phase7_rag_answers
Revises: 0004_phase6_vector_indexing
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_phase7_rag_answers"
down_revision = "0004_phase6_vector_indexing"
branch_labels = None
depends_on = None

ERROR_CODES = (
    "runtime_unavailable",
    "runtime_timeout",
    "query_embedding_failed",
    "invalid_query_embedding",
    "qdrant_unavailable",
    "retrieval_authority_failed",
    "source_artifact_missing",
    "source_artifact_mismatch",
    "source_changed",
    "generation_failed",
    "generation_timeout",
    "invalid_generation_response",
    "invalid_citation",
    "department_unavailable",
    "database_unavailable",
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_chunk_scope",
        "document_chunks",
        ["id", "department_id", "document_id", "extraction_id"],
    )
    op.create_table(
        "rag_answer_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("question_char_count", sa.Integer(), nullable=False),
        sa.Column("retrieval_candidate_count", sa.Integer(), nullable=True),
        sa.Column("retrieval_authorized_count", sa.Integer(), nullable=True),
        sa.Column("selected_source_count", sa.Integer(), nullable=True),
        sa.Column("query_embedding_pipeline_version", sa.String(100), nullable=False),
        sa.Column("query_embedding_model_id", sa.String(200), nullable=False),
        sa.Column("query_embedding_model_revision", sa.String(64), nullable=False),
        sa.Column("generation_model_id", sa.String(200), nullable=False),
        sa.Column("generation_model_revision", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("answer_contract_version", sa.String(100), nullable=False),
        sa.Column("minimum_score", sa.Numeric(4, 3), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('running','answered','insufficient_information','failed')",
            name="ck_rag_run_status",
        ),
        sa.CheckConstraint(
            "question_char_count BETWEEN 1 AND 2000", name="ck_rag_run_question_chars"
        ),
        sa.CheckConstraint(
            "retrieval_candidate_count IS NULL OR retrieval_candidate_count >= 0",
            name="ck_rag_run_candidate_count",
        ),
        sa.CheckConstraint(
            "retrieval_authorized_count IS NULL OR retrieval_authorized_count >= 0",
            name="ck_rag_run_authorized_count",
        ),
        sa.CheckConstraint(
            "selected_source_count IS NULL OR selected_source_count BETWEEN 0 AND 8",
            name="ck_rag_run_selected_count",
        ),
        sa.CheckConstraint(
            "query_embedding_pipeline_version = 'phase7-qwen3-query-embedding-v1'",
            name="ck_rag_run_query_pipeline",
        ),
        sa.CheckConstraint(
            "query_embedding_model_id = 'Qwen/Qwen3-Embedding-0.6B'",
            name="ck_rag_run_embedding_model",
        ),
        sa.CheckConstraint(
            "query_embedding_model_revision = 'd23109d65ca9fdf61eef614209744716f337f50f'",
            name="ck_rag_run_embedding_revision",
        ),
        sa.CheckConstraint(
            "generation_model_id = 'Qwen/Qwen3-0.6B'",
            name="ck_rag_run_generation_model",
        ),
        sa.CheckConstraint(
            "generation_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca'",
            name="ck_rag_run_generation_revision",
        ),
        sa.CheckConstraint(
            "prompt_version = 'phase7-grounded-answer-prompt-v1'",
            name="ck_rag_run_prompt_version",
        ),
        sa.CheckConstraint(
            "answer_contract_version = 'phase7-grounded-answer-v1'",
            name="ck_rag_run_answer_contract",
        ),
        sa.CheckConstraint("minimum_score BETWEEN -1.0 AND 1.0", name="ck_rag_run_minimum_score"),
        sa.CheckConstraint("version > 0", name="ck_rag_run_version"),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join(f"'{code}'" for code in ERROR_CODES)
            + ")",
            name="ck_rag_run_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL "
            "AND retrieval_candidate_count IS NULL "
            "AND retrieval_authorized_count IS NULL "
            "AND selected_source_count IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_rag_run_running_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'answered' AND finished_at IS NOT NULL "
            "AND retrieval_candidate_count IS NOT NULL "
            "AND retrieval_authorized_count IS NOT NULL "
            "AND selected_source_count BETWEEN 1 AND 8 "
            "AND retrieval_candidate_count >= retrieval_authorized_count "
            "AND retrieval_authorized_count >= selected_source_count "
            "AND error_code IS NULL) OR status <> 'answered'",
            name="ck_rag_run_answered_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'insufficient_information' AND finished_at IS NOT NULL "
            "AND retrieval_candidate_count IS NOT NULL "
            "AND retrieval_authorized_count IS NOT NULL "
            "AND selected_source_count = 0 "
            "AND retrieval_candidate_count >= retrieval_authorized_count "
            "AND error_code IS NULL) OR status <> 'insufficient_information'",
            name="ck_rag_run_insufficient_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND finished_at IS NOT NULL "
            "AND selected_source_count IS NULL AND error_code IS NOT NULL) "
            "OR status <> 'failed'",
            name="ck_rag_run_failed_lifecycle",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "department_id", name="uq_rag_run_department"),
    )
    op.create_index(
        "ix_rag_run_department_created",
        "rag_answer_runs",
        ["department_id", "created_at"],
    )
    op.create_table(
        "rag_answer_citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("indexing_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("source_label", sa.String(3), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("retrieval_score", sa.Numeric(8, 6), nullable=False),
        sa.Column("provenance_kind", sa.String(16), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("source_label ~ '^S[1-8]$'", name="ck_rag_citation_source_label"),
        sa.CheckConstraint("rank BETWEEN 1 AND 8", name="ck_rag_citation_rank"),
        sa.CheckConstraint("ordinal >= 0", name="ck_rag_citation_ordinal"),
        sa.CheckConstraint("retrieval_score BETWEEN -1.0 AND 1.0", name="ck_rag_citation_score"),
        sa.CheckConstraint(
            "provenance_kind IN ('page','line')",
            name="ck_rag_citation_provenance_kind",
        ),
        sa.CheckConstraint(
            "(provenance_kind = 'page' AND page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND page_start > 0 AND page_end >= page_start "
            "AND line_start IS NULL AND line_end IS NULL) OR "
            "(provenance_kind = 'line' AND line_start IS NOT NULL AND line_end IS NOT NULL "
            "AND line_start > 0 AND line_end >= line_start "
            "AND page_start IS NULL AND page_end IS NULL)",
            name="ck_rag_citation_provenance_range",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "department_id"],
            ["rag_answer_runs.id", "rag_answer_runs.department_id"],
            name="fk_rag_citation_run_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_rag_citation_document_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_rag_citation_extraction_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["indexing_id", "department_id", "document_id", "extraction_id"],
            [
                "document_vector_indexings.id",
                "document_vector_indexings.department_id",
                "document_vector_indexings.document_id",
                "document_vector_indexings.extraction_id",
            ],
            name="fk_rag_citation_indexing_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "department_id", "document_id", "extraction_id"],
            [
                "document_chunks.id",
                "document_chunks.department_id",
                "document_chunks.document_id",
                "document_chunks.extraction_id",
            ],
            name="fk_rag_citation_chunk_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "source_label", name="uq_rag_citation_run_label"),
        sa.UniqueConstraint("run_id", "rank", name="uq_rag_citation_run_rank"),
        sa.UniqueConstraint("run_id", "chunk_id", name="uq_rag_citation_run_chunk"),
    )
    op.create_index(
        "ix_rag_citation_department_run",
        "rag_answer_citations",
        ["department_id", "run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_rag_citation_department_run", table_name="rag_answer_citations")
    op.drop_table("rag_answer_citations")
    op.drop_index("ix_rag_run_department_created", table_name="rag_answer_runs")
    op.drop_table("rag_answer_runs")
    op.drop_constraint("uq_chunk_scope", "document_chunks", type_="unique")

"""Add department-scoped structured RAG feedback metadata.

Revision ID: 0006_phase8_rag_feedback
Revises: 0005_phase7_rag_answers
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_phase8_rag_feedback"
down_revision = "0005_phase7_rag_answers"
branch_labels = None
depends_on = None

HELPFUL_REASONS = (
    "clear",
    "complete",
    "well_supported",
    "useful_citations",
)
NEGATIVE_REASONS = (
    "incorrect",
    "unsupported_claim",
    "missing_information",
    "wrong_citation",
    "irrelevant_source",
    "unsafe_content",
    "formatting_problem",
    "insufficient_when_expected",
    "other_unspecified",
)
RESOLVED_CODES = (
    "confirmed_quality_issue",
    "confirmed_safety_issue",
    "addressed_externally",
    "no_action_required",
)
DISMISSED_CODES = (
    "duplicate",
    "not_reproducible",
    "out_of_scope",
    "no_issue_found",
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_rag_citation_id_department_run",
        "rag_answer_citations",
        ["id", "department_id", "run_id"],
    )
    op.create_table(
        "rag_answer_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("sentiment", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolution_code", sa.String(64), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "sentiment IN ('helpful','unhelpful','report')",
            name="ck_rag_feedback_sentiment",
        ),
        sa.CheckConstraint(
            "status IN ('open','triaged','resolved','dismissed')",
            name="ck_rag_feedback_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_rag_feedback_version"),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_rag_feedback_expiry",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL "
            "AND resolution_code IS NULL) OR "
            "(status = 'triaged' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND resolution_code IS NULL) OR "
            "(status = 'resolved' AND reviewed_by_user_id IS NOT NULL "
            f"AND reviewed_at IS NOT NULL AND resolution_code IN ({_sql_values(RESOLVED_CODES)})) "
            "OR (status = 'dismissed' AND reviewed_by_user_id IS NOT NULL "
            f"AND reviewed_at IS NOT NULL AND resolution_code IN ({_sql_values(DISMISSED_CODES)}))",
            name="ck_rag_feedback_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "department_id"],
            ["rag_answer_runs.id", "rag_answer_runs.department_id"],
            name="fk_rag_feedback_run_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "department_id", "run_id", name="uq_rag_feedback_id_department_run"
        ),
        sa.UniqueConstraint(
            "department_id",
            "run_id",
            "submitted_by_user_id",
            name="uq_rag_feedback_owner",
        ),
    )
    op.create_index(
        "ix_rag_feedback_owner_lookup",
        "rag_answer_feedback",
        ["department_id", "run_id", "submitted_by_user_id"],
    )
    op.create_index(
        "ix_rag_feedback_review_queue",
        "rag_answer_feedback",
        ["department_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_rag_feedback_expiry_purge",
        "rag_answer_feedback",
        ["department_id", "expires_at", "id"],
    )

    op.create_table(
        "rag_answer_feedback_reasons",
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("rank BETWEEN 1 AND 5", name="ck_rag_feedback_reason_rank"),
        sa.CheckConstraint(
            f"reason_code IN ({_sql_values(HELPFUL_REASONS + NEGATIVE_REASONS)})",
            name="ck_rag_feedback_reason_code",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_id", "department_id", "run_id"],
            [
                "rag_answer_feedback.id",
                "rag_answer_feedback.department_id",
                "rag_answer_feedback.run_id",
            ],
            name="fk_rag_feedback_reason_parent_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("feedback_id", "rank"),
        sa.UniqueConstraint("feedback_id", "reason_code", name="uq_rag_feedback_reason_code"),
    )

    op.create_table(
        "rag_answer_feedback_source_targets",
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("citation_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("rank BETWEEN 1 AND 8", name="ck_rag_feedback_target_rank"),
        sa.ForeignKeyConstraint(
            ["feedback_id", "department_id", "run_id"],
            [
                "rag_answer_feedback.id",
                "rag_answer_feedback.department_id",
                "rag_answer_feedback.run_id",
            ],
            name="fk_rag_feedback_target_parent_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["citation_id", "department_id", "run_id"],
            [
                "rag_answer_citations.id",
                "rag_answer_citations.department_id",
                "rag_answer_citations.run_id",
            ],
            name="fk_rag_feedback_target_citation_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("feedback_id", "rank"),
        sa.UniqueConstraint("feedback_id", "citation_id", name="uq_rag_feedback_target_citation"),
    )


def downgrade() -> None:
    op.drop_table("rag_answer_feedback_source_targets")
    op.drop_table("rag_answer_feedback_reasons")
    op.drop_index("ix_rag_feedback_expiry_purge", table_name="rag_answer_feedback")
    op.drop_index("ix_rag_feedback_review_queue", table_name="rag_answer_feedback")
    op.drop_index("ix_rag_feedback_owner_lookup", table_name="rag_answer_feedback")
    op.drop_table("rag_answer_feedback")
    op.drop_constraint("uq_rag_citation_id_department_run", "rag_answer_citations", type_="unique")

"""Add department-scoped vector-indexing jobs.

Revision ID: 0004_phase6_vector_indexing
Revises: 0003_phase5_extraction
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_phase6_vector_indexing"
down_revision = "0003_phase5_extraction"
branch_labels = None
depends_on = None

ERROR_CODES = (
    "document_unavailable",
    "extraction_unavailable",
    "chunk_artifact_missing",
    "chunk_artifact_mismatch",
    "embedding_model_unavailable",
    "embedding_failed",
    "embedding_timeout",
    "invalid_embedding",
    "qdrant_unavailable",
    "qdrant_schema_mismatch",
    "qdrant_write_failed",
    "qdrant_verification_failed",
    "qdrant_cleanup_failed",
    "claim_lost",
    "worker_shutdown",
    "database_unavailable",
)


def upgrade() -> None:
    op.create_table(
        "document_vector_indexings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retry_of_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("embedding_pipeline_version", sa.String(100), nullable=False),
        sa.Column("embedding_model_id", sa.String(200), nullable=False),
        sa.Column("embedding_model_revision", sa.String(64), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("distance", sa.String(16), nullable=False),
        sa.Column("vector_schema_version", sa.String(100), nullable=False),
        sa.Column("qdrant_collection", sa.String(128), nullable=False),
        sa.Column("expected_chunk_count", sa.Integer(), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("vector_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
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
            name="ck_vector_indexing_status",
        ),
        sa.CheckConstraint(
            "embedding_pipeline_version = 'phase6-qwen3-embedding-v1'",
            name="ck_vector_indexing_pipeline",
        ),
        sa.CheckConstraint(
            "embedding_model_id = 'Qwen/Qwen3-Embedding-0.6B'",
            name="ck_vector_indexing_model_id",
        ),
        sa.CheckConstraint(
            "embedding_model_revision = 'd23109d65ca9fdf61eef614209744716f337f50f'",
            name="ck_vector_indexing_model_revision",
        ),
        sa.CheckConstraint("embedding_dimension = 1024", name="ck_vector_indexing_dimension"),
        sa.CheckConstraint("distance = 'cosine'", name="ck_vector_indexing_distance"),
        sa.CheckConstraint(
            "vector_schema_version = 'phase6-qdrant-chunks-v1'",
            name="ck_vector_indexing_schema",
        ),
        sa.CheckConstraint(
            "qdrant_collection = 'deptslm_chunks_qwen3_0_6b_1024_v1'",
            name="ck_vector_indexing_collection",
        ),
        sa.CheckConstraint("expected_chunk_count > 0", name="ck_vector_indexing_expected_count"),
        sa.CheckConstraint(
            "point_count IS NULL OR point_count >= 0", name="ck_vector_indexing_point_count"
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_vector_indexing_attempt"),
        sa.CheckConstraint("version > 0", name="ck_vector_indexing_version"),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join(f"'{code}'" for code in ERROR_CODES)
            + ")",
            name="ck_vector_indexing_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND worker_id IS NULL AND claim_token IS NULL "
            "AND vector_attempt_id IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL "
            "AND started_at IS NULL AND finished_at IS NULL AND point_count IS NULL "
            "AND error_code IS NULL) OR status <> 'queued'",
            name="ck_vector_indexing_queued_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND vector_attempt_id IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND point_count IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_vector_indexing_running_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND vector_attempt_id IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NULL AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND point_count = expected_chunk_count "
            "AND error_code IS NULL) OR status <> 'succeeded'",
            name="ck_vector_indexing_succeeded_lifecycle",
        ),
        sa.CheckConstraint(
            "(status IN ('failed','cancelled') AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND point_count IS NULL AND error_code IS NOT NULL) "
            "OR status NOT IN ('failed','cancelled')",
            name="ck_vector_indexing_failure_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_vector_indexing_document_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_vector_indexing_extraction_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_id", "department_id", "document_id", "extraction_id"],
            [
                "document_vector_indexings.id",
                "document_vector_indexings.department_id",
                "document_vector_indexings.document_id",
                "document_vector_indexings.extraction_id",
            ],
            name="fk_vector_indexing_retry_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "department_id",
            "document_id",
            "extraction_id",
            name="uq_vector_indexing_scope",
        ),
    )
    op.create_index(
        "ix_vector_indexing_department_status_created",
        "document_vector_indexings",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "ix_vector_indexing_document_extraction_status",
        "document_vector_indexings",
        ["document_id", "extraction_id", "status"],
    )
    op.create_index(
        "ix_vector_indexing_claim",
        "document_vector_indexings",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_vector_indexing_lease",
        "document_vector_indexings",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_vector_indexing_active_pipeline",
        "document_vector_indexings",
        ["extraction_id", "embedding_pipeline_version"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "uq_vector_indexing_succeeded_contract",
        "document_vector_indexings",
        [
            "extraction_id",
            "embedding_model_revision",
            "embedding_dimension",
            "vector_schema_version",
        ],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )


def downgrade() -> None:
    op.drop_index("uq_vector_indexing_succeeded_contract", table_name="document_vector_indexings")
    op.drop_index("uq_vector_indexing_active_pipeline", table_name="document_vector_indexings")
    op.drop_index("ix_vector_indexing_lease", table_name="document_vector_indexings")
    op.drop_index("ix_vector_indexing_claim", table_name="document_vector_indexings")
    op.drop_index(
        "ix_vector_indexing_document_extraction_status", table_name="document_vector_indexings"
    )
    op.drop_index(
        "ix_vector_indexing_department_status_created", table_name="document_vector_indexings"
    )
    op.drop_table("document_vector_indexings")

"""Add department-scoped extraction jobs and chunk metadata.

Revision ID: 0003_phase5_extraction
Revises: 0002_phase4_documents
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_phase5_extraction"
down_revision = "0002_phase4_documents"
branch_labels = None
depends_on = None

ERROR_CODES = (
    "source_missing",
    "source_integrity_mismatch",
    "unsupported_media_type",
    "invalid_utf8",
    "invalid_pdf",
    "encrypted_pdf",
    "page_limit_exceeded",
    "extraction_timeout",
    "extraction_output_limit",
    "no_extractable_text",
    "chunk_limit_exceeded",
    "extraction_quota_exceeded",
    "parser_failed",
    "storage_unavailable",
    "database_unavailable",
    "document_unavailable",
    "claim_lost",
    "worker_shutdown",
)


def upgrade() -> None:
    op.create_unique_constraint("uq_document_id_department", "documents", ["id", "department_id"])
    op.create_table(
        "document_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retry_of_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("pipeline_version", sa.String(100), nullable=False),
        sa.Column("parser_name", sa.String(100), nullable=True),
        sa.Column("parser_version", sa.String(100), nullable=True),
        sa.Column("normalization_version", sa.String(100), nullable=False),
        sa.Column("chunking_version", sa.String(100), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("normalized_sha256", sa.String(64), nullable=True),
        sa.Column("normalized_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("output_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
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
            name="ck_extraction_status",
        ),
        sa.CheckConstraint(
            "pipeline_version ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_pipeline_version",
        ),
        sa.CheckConstraint(
            "normalization_version ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_normalization_version",
        ),
        sa.CheckConstraint(
            "chunking_version ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_chunking_version",
        ),
        sa.CheckConstraint(
            "parser_name IS NULL OR parser_name ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_parser_name",
        ),
        sa.CheckConstraint(
            "parser_version IS NULL OR parser_version ~ '^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,99}$'",
            name="ck_extraction_parser_version",
        ),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_extraction_source_sha256"),
        sa.CheckConstraint("source_byte_size > 0", name="ck_extraction_source_size"),
        sa.CheckConstraint(
            "normalized_sha256 IS NULL OR normalized_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_extraction_normalized_sha256",
        ),
        sa.CheckConstraint(
            "normalized_byte_size IS NULL OR normalized_byte_size > 0",
            name="ck_extraction_normalized_size",
        ),
        sa.CheckConstraint(
            "output_byte_size IS NULL OR output_byte_size > 0",
            name="ck_extraction_output_size",
        ),
        sa.CheckConstraint(
            "chunk_count IS NULL OR chunk_count >= 0", name="ck_extraction_chunk_count"
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_extraction_attempt"),
        sa.CheckConstraint("version > 0", name="ck_extraction_version"),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join(f"'{code}'" for code in ERROR_CODES)
            + ")",
            name="ck_extraction_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND worker_id IS NULL AND claim_token IS NULL "
            "AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND parser_name IS NULL AND parser_version IS NULL AND normalized_sha256 IS NULL "
            "AND normalized_byte_size IS NULL AND output_byte_size IS NULL AND chunk_count IS NULL "
            "AND error_code IS NULL) OR status <> 'queued'",
            name="ck_extraction_queued_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND normalized_sha256 IS NULL AND normalized_byte_size IS NULL "
            "AND output_byte_size IS NULL AND chunk_count IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_extraction_running_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND parser_name IS NOT NULL AND parser_version IS NOT NULL "
            "AND normalized_sha256 IS NOT NULL AND normalized_byte_size IS NOT NULL "
            "AND output_byte_size IS NOT NULL AND chunk_count IS NOT NULL AND error_code IS NULL) "
            "OR status <> 'succeeded'",
            name="ck_extraction_succeeded_lifecycle",
        ),
        sa.CheckConstraint(
            "(status IN ('failed','cancelled') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL "
            "AND normalized_sha256 IS NULL AND normalized_byte_size IS NULL "
            "AND output_byte_size IS NULL AND chunk_count IS NULL) "
            "OR status NOT IN ('failed','cancelled')",
            name="ck_extraction_failure_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_extraction_document_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_extraction_retry_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "department_id", "document_id", name="uq_extraction_id_department_document"
        ),
    )
    op.create_index(
        "ix_extraction_department_status_created",
        "document_extractions",
        ["department_id", "status", "created_at"],
    )
    op.create_index(
        "ix_extraction_document_status_created",
        "document_extractions",
        ["document_id", "status", "created_at"],
    )
    op.create_index(
        "ix_extraction_claim",
        "document_extractions",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_extraction_lease",
        "document_extractions",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_extraction_active_document",
        "document_extractions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "uq_extraction_succeeded_pipeline",
        "document_extractions",
        ["document_id", "source_sha256", "pipeline_version"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.BigInteger(), nullable=False),
        sa.Column("char_end", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("provenance_kind", sa.String(16), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunk_ordinal"),
        sa.CheckConstraint("char_start >= 0 AND char_end > char_start", name="ck_chunk_char_range"),
        sa.CheckConstraint("byte_size > 0", name="ck_chunk_byte_size"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_chunk_content_sha256"),
        sa.CheckConstraint("provenance_kind IN ('page','line')", name="ck_chunk_provenance_kind"),
        sa.CheckConstraint(
            "(provenance_kind = 'page' AND page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND page_start > 0 AND page_end >= page_start "
            "AND line_start IS NULL AND line_end IS NULL) "
            "OR (provenance_kind = 'line' AND line_start IS NOT NULL AND line_end IS NOT NULL "
            "AND line_start > 0 AND line_end >= line_start "
            "AND page_start IS NULL AND page_end IS NULL)",
            name="ck_chunk_provenance_range",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_chunk_extraction_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_chunk_document_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extraction_id", "ordinal", name="uq_chunk_extraction_ordinal"),
    )
    op.create_index(
        "ix_chunk_department_document",
        "document_chunks",
        ["department_id", "document_id", "ordinal"],
    )


def downgrade() -> None:
    op.drop_index("ix_chunk_department_document", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("uq_extraction_succeeded_pipeline", table_name="document_extractions")
    op.drop_index("uq_extraction_active_document", table_name="document_extractions")
    op.drop_index("ix_extraction_lease", table_name="document_extractions")
    op.drop_index("ix_extraction_claim", table_name="document_extractions")
    op.drop_index("ix_extraction_document_status_created", table_name="document_extractions")
    op.drop_index("ix_extraction_department_status_created", table_name="document_extractions")
    op.drop_table("document_extractions")
    op.drop_constraint("uq_document_id_department", "documents", type_="unique")

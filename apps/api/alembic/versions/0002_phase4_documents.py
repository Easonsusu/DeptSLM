"""Add department-scoped document metadata.

Revision ID: 0002_phase4_documents
Revises: 0001_phase3
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_phase4_documents"
down_revision = "0001_phase3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "media_type IN ('application/pdf','text/plain','text/markdown')",
            name="ck_document_media_type",
        ),
        sa.CheckConstraint("byte_size > 0", name="ck_document_byte_size_positive"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_document_sha256"),
        sa.CheckConstraint("status IN ('stored','deleted')", name="ck_document_status"),
        sa.CheckConstraint("version > 0", name="ck_document_version_positive"),
        sa.CheckConstraint(
            "(status = 'stored' AND deleted_at IS NULL AND deleted_by_user_id IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL AND deleted_by_user_id IS NOT NULL)",
            name="ck_document_deletion_lifecycle",
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_department_status_created",
        "documents",
        ["department_id", "status", "created_at"],
    )
    op.create_index("ix_document_department_sha256", "documents", ["department_id", "sha256"])


def downgrade() -> None:
    op.drop_index("ix_document_department_sha256", table_name="documents")
    op.drop_index("ix_document_department_status_created", table_name="documents")
    op.drop_table("documents")

"""Add bounded Phase 12.1E-A per-status scan cursors and keyset indexes.

This revision is intentionally numbered 0013 on the Phase 12.1E-A branch.
PR #19 currently owns a different 0013 revision; after this branch is merged,
that PR must be rebased and its migration renumbered to 0014.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_phase12_adapter_reconciliation_cursor"
down_revision = "0012_phase12_adapter_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_adapter_import_attempt_department_status_created",
        table_name="adapter_import_attempts",
    )
    op.create_index(
        "ix_adapter_import_attempt_department_status_created_id",
        "adapter_import_attempts",
        ["department_id", "status", "created_at", "id"],
    )
    op.drop_index(
        "ix_adapter_registry_attempt_department_status",
        table_name="adapter_registry_attempts",
    )
    op.create_index(
        "ix_adapter_registry_attempt_department_status_created_id",
        "adapter_registry_attempts",
        ["department_id", "status", "created_at", "id"],
    )
    op.create_table(
        "adapter_artifact_reconciliation_cursors",
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("family", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cursor_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_artifact_reconciliation_cursor_department",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("department_id", "family", "status"),
        sa.UniqueConstraint(
            "department_id",
            "family",
            "status",
            name="uq_adapter_artifact_reconciliation_cursor_scope",
        ),
        sa.CheckConstraint(
            "family IN ('source','registry')",
            name="ck_adapter_artifact_reconciliation_cursor_family",
        ),
        sa.CheckConstraint(
            "(family = 'source' AND status IN "
            "('failed','abandoned','registered','validated','staged','published')) OR "
            "(family = 'registry' AND status IN "
            "('validation_failed','failed','reclaimed'))",
            name="ck_adapter_artifact_reconciliation_cursor_status",
        ),
        sa.CheckConstraint(
            "((cursor_created_at IS NULL AND cursor_attempt_id IS NULL) OR "
            "(cursor_created_at IS NOT NULL AND cursor_attempt_id IS NOT NULL))",
            name="ck_adapter_artifact_reconciliation_cursor_pair",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_adapter_artifact_reconciliation_cursor_version",
        ),
    )


def downgrade() -> None:
    op.drop_table("adapter_artifact_reconciliation_cursors")
    op.drop_index(
        "ix_adapter_registry_attempt_department_status_created_id",
        table_name="adapter_registry_attempts",
    )
    op.create_index(
        "ix_adapter_registry_attempt_department_status",
        "adapter_registry_attempts",
        ["department_id", "status", "created_at"],
    )
    op.drop_index(
        "ix_adapter_import_attempt_department_status_created_id",
        table_name="adapter_import_attempts",
    )
    op.create_index(
        "ix_adapter_import_attempt_department_status_created",
        "adapter_import_attempts",
        ["department_id", "status", "created_at"],
    )

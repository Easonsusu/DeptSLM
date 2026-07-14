"""Add departments, identities, memberships, and append-only audit events.

Revision ID: 0001_phase3
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_phase3"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(issuer) > 0", name="ck_user_identity_issuer_nonempty"),
        sa.CheckConstraint("length(subject) > 0", name="ck_user_identity_subject_nonempty"),
        sa.CheckConstraint("status IN ('active','suspended','revoked')", name="ck_user_identity_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identity_issuer_subject"),
    )
    op.create_table(
        "departments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_department_slug_format"),
        sa.CheckConstraint("length(slug) BETWEEN 2 AND 63", name="ck_department_slug_length"),
        sa.CheckConstraint("length(btrim(display_name)) BETWEEN 1 AND 200", name="ck_department_display_name_length"),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_department_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_department_slug"),
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('system_admin','department_admin','instructor','student','viewer')", name="ck_membership_role"),
        sa.CheckConstraint("status IN ('active','suspended','revoked')", name="ck_membership_status"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user_identities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["user_identities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "department_id", name="uq_membership_user_department"),
    )
    op.create_index("ix_membership_department_status", "memberships", ["department_id", "status"])
    op.create_index("ix_membership_user_status", "memberships", ["user_id", "status"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_subject", sa.String(512), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.String(100), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(action) > 0", name="ck_audit_action_nonempty"),
        sa.CheckConstraint("length(resource_type) > 0", name="ck_audit_resource_type_nonempty"),
        sa.CheckConstraint("result IN ('allowed','denied')", name="ck_audit_result"),
        sa.CheckConstraint("length(reason_code) > 0", name="ck_audit_reason_nonempty"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user_identities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_department_created", "audit_events", ["department_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_department_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_membership_user_status", table_name="memberships")
    op.drop_index("ix_membership_department_status", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("departments")
    op.drop_table("user_identities")

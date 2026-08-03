# ruff: noqa: E501
"""Add the Phase 12.1E-B adapter-artifact purge authority.

This migration is deliberately independent from the Phase 12.1E-A
reconciliation tables.  It stores only closed, content-free authority and
crash-resumable deletion progress; external bytes remain beneath
``DEPTSLM_DATA_DIR``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_phase12_adapter_purge"
down_revision = "0013_phase12_adapter_reconciliation_cursor"
branch_labels = None
depends_on = None

_BLOCKED_REASONS = (
    "purge_authority_changed",
    "purge_manifest_invalid",
    "purge_permissions_invalid",
    "purge_path_unsafe",
    "purge_tombstone_conflict",
    "purge_dependency_active",
    "purge_operation_conflict",
    "purge_database_unavailable",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join("'" + value + "'" for value in values)


def upgrade() -> None:
    op.create_table(
        "adapter_purge_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("item_limit_value", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expected_adapter_version", sa.Integer(), nullable=False),
        sa.Column("expected_source_version", sa.Integer(), nullable=False),
        sa.Column("expected_source_attempt_version", sa.Integer(), nullable=False),
        sa.Column("expected_registry_attempt_version", sa.Integer(), nullable=False),
        sa.Column("source_authoritative_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("source_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("source_attempt_number", sa.Integer(), nullable=False),
        sa.Column("registry_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("registry_publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("registry_attempt_number", sa.Integer(), nullable=False),
        sa.Column("authority_snapshot", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("eligible_item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_audited_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
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
            name="fk_adapter_purge_operation_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_purge_operation_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_purge_operation_source_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id", "department_id"],
            ["memberships.user_id", "memberships.department_id"],
            name="fk_adapter_purge_operation_requester_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_purge_operation_requester_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_authoritative_attempt_id",
                "department_id",
                "source_bundle_id",
                "source_publication_attempt_id",
                "source_attempt_number",
            ],
            [
                "adapter_import_attempts.id",
                "adapter_import_attempts.department_id",
                "adapter_import_attempts.source_bundle_id",
                "adapter_import_attempts.publication_attempt_id",
                "adapter_import_attempts.attempt_number",
            ],
            name="fk_adapter_purge_operation_source_attempt_exact",
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
            name="fk_adapter_purge_operation_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_purge_operation_scope"),
        sa.CheckConstraint(
            "status IN ('registered','deleting','completed','completed_with_blocks','blocked')",
            name="ck_adapter_purge_operation_status",
        ),
        sa.CheckConstraint(
            "limit_value BETWEEN 1 AND 1000 AND item_limit_value BETWEEN 2 AND 2000",
            name="ck_adapter_purge_operation_limits",
        ),
        sa.CheckConstraint(
            "expected_adapter_version > 0 AND expected_source_version > 0 AND expected_source_attempt_version > 0 AND expected_registry_attempt_version > 0 AND source_attempt_number > 0 AND registry_attempt_number > 0 AND version > 0",
            name="ck_adapter_purge_operation_versions",
        ),
        sa.CheckConstraint(
            "json_typeof(authority_snapshot) = 'object'", name="ck_adapter_purge_operation_snapshot"
        ),
        sa.CheckConstraint(
            "eligible_item_count >= 0 AND completed_item_count >= 0 AND blocked_item_count >= 0 AND completed_item_count + blocked_item_count <= eligible_item_count",
            name="ck_adapter_purge_operation_counts",
        ),
        sa.CheckConstraint(
            "(status IN ('registered','deleting') AND completed_at IS NULL) OR (status IN ('completed','completed_with_blocks','blocked') AND completed_at IS NOT NULL)",
            name="ck_adapter_purge_operation_lifecycle",
        ),
    )
    op.create_index(
        "uq_adapter_purge_operation_active",
        "adapter_purge_operations",
        ["department_id", "adapter_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('registered','deleting')"),
    )
    op.create_index(
        "ix_adapter_purge_operation_department_created",
        "adapter_purge_operations",
        ["department_id", "created_at"],
    )

    op.create_table(
        "adapter_purge_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("surface_type", sa.String(16), nullable=False),
        sa.Column("import_attempt_id", sa.Uuid()),
        sa.Column("registry_attempt_id", sa.Uuid()),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("expected_resource_version", sa.Integer(), nullable=False),
        sa.Column("expected_attempt_version", sa.Integer(), nullable=False),
        sa.Column("expected_resource_status", sa.String(24), nullable=False),
        sa.Column("expected_attempt_status", sa.String(24), nullable=False),
        sa.Column("authority_manifest", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("authority_snapshot", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("expected_tombstone_namespace", sa.JSON(none_as_null=True)),
        sa.Column("observed_identity", sa.JSON(none_as_null=True)),
        sa.Column("tombstone_identity", sa.JSON(none_as_null=True)),
        sa.Column("deletion_plan", sa.JSON(none_as_null=True)),
        sa.Column("next_entry_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("in_flight_entry", sa.JSON(none_as_null=True)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("blocked_reason_code", sa.String(64)),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deletion_authorized_at", sa.DateTime(timezone=True)),
        sa.Column("tombstone_bound_at", sa.DateTime(timezone=True)),
        sa.Column("deletion_started_at", sa.DateTime(timezone=True)),
        sa.Column("directory_unlink_started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "department_id"],
            ["adapter_purge_operations.id", "adapter_purge_operations.department_id"],
            name="fk_adapter_purge_reservation_operation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_purge_reservation_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_purge_reservation_source_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "import_attempt_id",
                "department_id",
                "source_bundle_id",
                "publication_attempt_id",
                "attempt_number",
            ],
            [
                "adapter_import_attempts.id",
                "adapter_import_attempts.department_id",
                "adapter_import_attempts.source_bundle_id",
                "adapter_import_attempts.publication_attempt_id",
                "adapter_import_attempts.attempt_number",
            ],
            name="fk_adapter_purge_reservation_import_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "registry_attempt_id",
                "department_id",
                "adapter_id",
                "publication_attempt_id",
                "attempt_number",
            ],
            [
                "adapter_registry_attempts.id",
                "adapter_registry_attempts.department_id",
                "adapter_registry_attempts.adapter_id",
                "adapter_registry_attempts.publication_attempt_id",
                "adapter_registry_attempts.attempt_number",
            ],
            name="fk_adapter_purge_reservation_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_purge_reservation_scope"),
        sa.UniqueConstraint(
            "operation_id", "surface_type", name="uq_adapter_purge_reservation_surface"
        ),
        sa.CheckConstraint(
            "surface_type IN ('source_final','registry_final')",
            name="ck_adapter_purge_reservation_surface",
        ),
        sa.CheckConstraint(
            "((surface_type = 'source_final' AND import_attempt_id IS NOT NULL AND registry_attempt_id IS NULL AND expected_resource_status = 'consumed' AND expected_attempt_status = 'committed') OR (surface_type = 'registry_final' AND import_attempt_id IS NULL AND registry_attempt_id IS NOT NULL AND expected_resource_status = 'validated' AND expected_attempt_status = 'succeeded'))",
            name="ck_adapter_purge_reservation_authority",
        ),
        sa.CheckConstraint(
            "publication_attempt_id IS NOT NULL AND attempt_number > 0 AND expected_resource_version > 0 AND expected_attempt_version > 0 AND version > 0",
            name="ck_adapter_purge_reservation_versions",
        ),
        sa.CheckConstraint(
            "json_typeof(authority_manifest) = 'object' AND json_typeof(authority_snapshot) = 'object'",
            name="ck_adapter_purge_reservation_snapshot",
        ),
        sa.CheckConstraint(
            "status IN ('registered','deletion_authorized','tombstone_bound','deleting','completed','blocked')",
            name="ck_adapter_purge_reservation_status",
        ),
        sa.CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN ("
            + _quoted(_BLOCKED_REASONS)
            + ")",
            name="ck_adapter_purge_reservation_reason",
        ),
        sa.CheckConstraint("next_entry_index >= 0", name="ck_adapter_purge_reservation_progress"),
        sa.CheckConstraint(
            "(status IN ('registered','deletion_authorized') AND tombstone_identity IS NULL AND completed_at IS NULL AND blocked_at IS NULL) OR (status IN ('tombstone_bound','deleting') AND json_typeof(tombstone_identity) = 'object' AND tombstone_bound_at IS NOT NULL AND deletion_authorized_at IS NOT NULL AND completed_at IS NULL AND blocked_at IS NULL) OR (status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL) OR (status = 'blocked' AND blocked_at IS NOT NULL AND completed_at IS NULL AND blocked_reason_code IS NOT NULL)",
            name="ck_adapter_purge_reservation_lifecycle",
        ),
    )
    op.create_index(
        "uq_adapter_purge_reservation_active_source",
        "adapter_purge_reservations",
        ["department_id", "source_bundle_id"],
        unique=True,
        postgresql_where=sa.text(
            "surface_type = 'source_final' AND status IN ('registered','deletion_authorized','tombstone_bound','deleting')"
        ),
    )
    op.create_index(
        "uq_adapter_purge_reservation_active_registry",
        "adapter_purge_reservations",
        ["department_id", "adapter_id"],
        unique=True,
        postgresql_where=sa.text(
            "surface_type = 'registry_final' AND status IN ('registered','deletion_authorized','tombstone_bound','deleting')"
        ),
    )
    op.create_index(
        "ix_adapter_purge_reservation_operation_status",
        "adapter_purge_reservations",
        ["operation_id", "status", "created_at"],
    )

    op.create_table(
        "adapter_purge_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("surface_type", sa.String(16), nullable=False),
        sa.Column("adapter_id", sa.Uuid(), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=False),
        sa.Column("import_attempt_id", sa.Uuid()),
        sa.Column("registry_attempt_id", sa.Uuid()),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("expected_item_version", sa.Integer(), nullable=False),
        sa.Column("ownership_manifest", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("observed_identity", sa.JSON(none_as_null=True)),
        sa.Column("tombstone_identity", sa.JSON(none_as_null=True)),
        sa.Column("deletion_plan", sa.JSON(none_as_null=True)),
        sa.Column("expected_tombstone_namespace", sa.JSON(none_as_null=True)),
        sa.Column("next_entry_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("in_flight_entry", sa.JSON(none_as_null=True)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("blocked_reason_code", sa.String(64)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("move_authorized_at", sa.DateTime(timezone=True)),
        sa.Column("tombstone_bound_at", sa.DateTime(timezone=True)),
        sa.Column("deletion_started_at", sa.DateTime(timezone=True)),
        sa.Column("directory_unlink_started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "department_id"],
            ["adapter_purge_operations.id", "adapter_purge_operations.department_id"],
            name="fk_adapter_purge_item_operation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id", "department_id"],
            ["adapter_purge_reservations.id", "adapter_purge_reservations.department_id"],
            name="fk_adapter_purge_item_reservation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_purge_item_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_purge_item_source_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "import_attempt_id",
                "department_id",
                "source_bundle_id",
                "publication_attempt_id",
                "attempt_number",
            ],
            [
                "adapter_import_attempts.id",
                "adapter_import_attempts.department_id",
                "adapter_import_attempts.source_bundle_id",
                "adapter_import_attempts.publication_attempt_id",
                "adapter_import_attempts.attempt_number",
            ],
            name="fk_adapter_purge_item_import_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "registry_attempt_id",
                "department_id",
                "adapter_id",
                "publication_attempt_id",
                "attempt_number",
            ],
            [
                "adapter_registry_attempts.id",
                "adapter_registry_attempts.department_id",
                "adapter_registry_attempts.adapter_id",
                "adapter_registry_attempts.publication_attempt_id",
                "adapter_registry_attempts.attempt_number",
            ],
            name="fk_adapter_purge_item_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "operation_id", "reservation_id", name="uq_adapter_purge_item_reservation"
        ),
        sa.CheckConstraint(
            "surface_type IN ('source_final','registry_final')",
            name="ck_adapter_purge_item_surface",
        ),
        sa.CheckConstraint(
            "expected_item_version > 0 AND attempt_number > 0 AND version > 0",
            name="ck_adapter_purge_item_versions",
        ),
        sa.CheckConstraint(
            "json_typeof(ownership_manifest) = 'object'", name="ck_adapter_purge_item_manifest"
        ),
        sa.CheckConstraint(
            "status IN ('registered','verified','tombstone_bound','deleting','completed','blocked')",
            name="ck_adapter_purge_item_status",
        ),
        sa.CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN ("
            + _quoted(_BLOCKED_REASONS)
            + ")",
            name="ck_adapter_purge_item_reason",
        ),
        sa.CheckConstraint("next_entry_index >= 0", name="ck_adapter_purge_item_progress"),
    )
    op.create_index(
        "ix_adapter_purge_item_operation_status",
        "adapter_purge_items",
        ["operation_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_adapter_purge_item_operation_status", table_name="adapter_purge_items")
    op.drop_table("adapter_purge_items")
    op.drop_index(
        "ix_adapter_purge_reservation_operation_status", table_name="adapter_purge_reservations"
    )
    op.drop_index(
        "uq_adapter_purge_reservation_active_registry", table_name="adapter_purge_reservations"
    )
    op.drop_index(
        "uq_adapter_purge_reservation_active_source", table_name="adapter_purge_reservations"
    )
    op.drop_table("adapter_purge_reservations")
    op.drop_index(
        "ix_adapter_purge_operation_department_created", table_name="adapter_purge_operations"
    )
    op.drop_index("uq_adapter_purge_operation_active", table_name="adapter_purge_operations")
    op.drop_table("adapter_purge_operations")

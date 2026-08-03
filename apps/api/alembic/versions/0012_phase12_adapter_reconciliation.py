"""Add the Phase 12.1E-A adapter-artifact reconciliation authority.

The revision is intentionally self-contained.  It contains metadata-only
operation and item state and never imports application models or filesystem
helpers so historical migrations remain reproducible.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_phase12_adapter_reconciliation"
down_revision = "0011_phase12_adapter_registry"
branch_labels = None
depends_on = None

_BLOCKED_REASONS = (
    "staging_path_unsafe",
    "artifact_ownership_mismatch",
    "artifact_manifest_invalid",
    "artifact_permissions_invalid",
    "artifact_authority_changed",
    "artifact_tombstone_conflict",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join("'" + value + "'" for value in values)


def upgrade() -> None:
    # The exact registry-attempt tuple is referenced by reconciliation items.
    op.create_unique_constraint(
        "uq_adapter_registry_attempt_exact",
        "adapter_registry_attempts",
        ["id", "department_id", "adapter_id", "publication_attempt_id", "attempt_number"],
    )

    # Terminal attempts may be cleanup-confirmed.  All live and authoritative
    # states remain fenced until every applicable surface is absent.
    op.drop_constraint(
        "ck_adapter_import_attempt_lifecycle",
        "adapter_import_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_adapter_import_attempt_lifecycle",
        "adapter_import_attempts",
        "(status = 'registered' AND validated_at IS NULL AND staged_at IS NULL AND "
        "published_at IS NULL AND committed_at IS NULL AND finished_at IS NULL AND "
        "cleanup_confirmed_at IS NULL AND ownership_manifest IS NULL AND error_code IS NULL) OR "
        "(status = 'validated' AND validated_at IS NOT NULL AND staged_at IS NULL AND "
        "published_at IS NULL AND committed_at IS NULL AND finished_at IS NULL AND "
        "cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status = 'staged' AND validated_at IS NOT NULL AND staged_at IS NOT NULL AND "
        "ownership_manifest IS NOT NULL AND published_at IS NULL AND committed_at IS NULL "
        "AND finished_at IS NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status = 'published' AND validated_at IS NOT NULL AND staged_at IS NOT NULL AND "
        "published_at IS NOT NULL AND ownership_manifest IS NOT NULL AND committed_at IS NULL "
        "AND finished_at IS NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status = 'committed' AND validated_at IS NOT NULL AND staged_at IS NOT NULL AND "
        "published_at IS NOT NULL AND committed_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND ownership_manifest IS NOT NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status IN ('failed','abandoned') AND finished_at IS NOT NULL AND committed_at IS NULL "
        "AND error_code IS NOT NULL AND (cleanup_confirmed_at IS NULL OR "
        "cleanup_confirmed_at >= finished_at))",
    )
    op.drop_constraint(
        "ck_adapter_registry_attempt_exact_lifecycle",
        "adapter_registry_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_adapter_registry_attempt_exact_lifecycle",
        "adapter_registry_attempts",
        "((status IN ('registered','running','staged','published','succeeded') "
        "AND cleanup_confirmed_at IS NULL) OR status IN ('validation_failed','failed','reclaimed')) "
        "AND ((status = 'registered' AND worker_id IS NULL AND claimed_at IS NULL AND "
        "staged_at IS NULL AND published_at IS NULL AND finished_at IS NULL AND "
        "ownership_manifest IS NULL AND error_code IS NULL) OR "
        "(status = 'running' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL AND "
        "staged_at IS NULL AND published_at IS NULL AND finished_at IS NULL AND "
        "ownership_manifest IS NULL AND error_code IS NULL) OR "
        "(status = 'staged' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL AND "
        "staged_at IS NOT NULL AND published_at IS NULL AND finished_at IS NULL AND "
        "ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
        "(status = 'published' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL AND "
        "staged_at IS NOT NULL AND published_at IS NOT NULL AND finished_at IS NULL AND "
        "ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
        "(status = 'succeeded' AND staged_at IS NOT NULL AND published_at IS NOT NULL AND "
        "finished_at IS NOT NULL AND ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
        "(status IN ('validation_failed','failed','reclaimed') AND finished_at IS NOT NULL AND "
        "error_code IS NOT NULL AND (cleanup_confirmed_at IS NULL OR "
        "cleanup_confirmed_at >= finished_at)))",
    )

    op.create_table(
        "adapter_artifact_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("operation_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("minimum_age_seconds", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
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
            name="fk_adapter_artifact_operation_department",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id", "department_id"],
            ["memberships.user_id", "memberships.department_id"],
            name="fk_adapter_artifact_operation_requester_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_artifact_operation_requester_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "department_id", name="uq_adapter_artifact_operation_scope"),
        sa.CheckConstraint(
            "operation_type = 'reconcile'", name="ck_adapter_artifact_operation_type"
        ),
        sa.CheckConstraint(
            "status IN ('registered','completed','completed_with_blocks')",
            name="ck_adapter_artifact_operation_status",
        ),
        sa.CheckConstraint(
            "limit_value BETWEEN 1 AND 1000", name="ck_adapter_artifact_operation_limit"
        ),
        sa.CheckConstraint(
            "minimum_age_seconds BETWEEN 300 AND 86400",
            name="ck_adapter_artifact_operation_minimum_age",
        ),
        sa.CheckConstraint(
            "eligible_count >= 0 AND completed_count >= 0 AND blocked_count >= 0 AND completed_count + blocked_count <= eligible_count",
            name="ck_adapter_artifact_operation_counts",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL) OR (status IN ('completed','completed_with_blocks') AND completed_at IS NOT NULL)",
            name="ck_adapter_artifact_operation_lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND blocked_count = 0) OR (status = 'completed_with_blocks' AND blocked_count > 0) OR status = 'registered'",
            name="ck_adapter_artifact_operation_blocked_lifecycle",
        ),
        sa.CheckConstraint("version > 0", name="ck_adapter_artifact_operation_version"),
    )
    op.create_index(
        "uq_adapter_artifact_operation_active",
        "adapter_artifact_operations",
        ["department_id"],
        unique=True,
        postgresql_where=sa.text("operation_type = 'reconcile' AND status = 'registered'"),
    )
    op.create_index(
        "ix_adapter_artifact_operation_department_created",
        "adapter_artifact_operations",
        ["department_id", "created_at"],
    )

    op.create_table(
        "adapter_artifact_operation_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("surface_type", sa.String(24), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid()),
        sa.Column("adapter_id", sa.Uuid()),
        sa.Column("import_attempt_id", sa.Uuid()),
        sa.Column("registry_attempt_id", sa.Uuid()),
        sa.Column("publication_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("expected_resource_version", sa.Integer(), nullable=False),
        sa.Column("expected_attempt_version", sa.Integer(), nullable=False),
        sa.Column("ownership_manifest", sa.JSON()),
        sa.Column("observed_identity", sa.JSON()),
        sa.Column("tombstone_identity", sa.JSON()),
        sa.Column("deletion_plan", sa.JSON()),
        sa.Column("next_entry_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("in_flight_entry", sa.JSON()),
        sa.Column("directory_unlink_started_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("blocked_reason_code", sa.String(64)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("tombstone_bound_at", sa.DateTime(timezone=True)),
        sa.Column("deletion_started_at", sa.DateTime(timezone=True)),
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
            ["adapter_artifact_operations.id", "adapter_artifact_operations.department_id"],
            name="fk_adapter_artifact_item_operation_scope",
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
            name="fk_adapter_artifact_item_import_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_artifact_item_source_scope",
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
            name="fk_adapter_artifact_item_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_artifact_item_adapter_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "operation_id",
            "surface_type",
            "source_bundle_id",
            "adapter_id",
            "import_attempt_id",
            "registry_attempt_id",
            "publication_attempt_id",
            "attempt_number",
            name="uq_adapter_artifact_item_operation_surface",
        ),
        sa.CheckConstraint(
            "surface_type IN ('source_stage','source_final','registry_stage','registry_final')",
            name="ck_adapter_artifact_item_surface",
        ),
        sa.CheckConstraint(
            "((surface_type IN ('source_stage','source_final') AND source_bundle_id IS NOT NULL AND import_attempt_id IS NOT NULL AND adapter_id IS NULL AND registry_attempt_id IS NULL) OR (surface_type IN ('registry_stage','registry_final') AND adapter_id IS NOT NULL AND registry_attempt_id IS NOT NULL AND source_bundle_id IS NULL AND import_attempt_id IS NULL))",
            name="ck_adapter_artifact_item_surface_authority",
        ),
        sa.CheckConstraint(
            "publication_attempt_id IS NOT NULL AND attempt_number > 0 AND expected_resource_version > 0 AND expected_attempt_version > 0",
            name="ck_adapter_artifact_item_attempt_authority",
        ),
        sa.CheckConstraint(
            "status IN ('registered','verified','tombstone_bound','deleting','completed','blocked')",
            name="ck_adapter_artifact_item_status",
        ),
        sa.CheckConstraint(
            "ownership_manifest IS NULL OR json_typeof(ownership_manifest) = 'object'",
            name="ck_adapter_artifact_item_manifest_object",
        ),
        sa.CheckConstraint(
            "observed_identity IS NULL OR json_typeof(observed_identity) IN ('object','array')",
            name="ck_adapter_artifact_item_observed_json",
        ),
        sa.CheckConstraint(
            "tombstone_identity IS NULL OR json_typeof(tombstone_identity) IN ('object','array')",
            name="ck_adapter_artifact_item_tombstone_json",
        ),
        sa.CheckConstraint(
            "deletion_plan IS NULL OR json_typeof(deletion_plan) IN ('object','array')",
            name="ck_adapter_artifact_item_plan_json",
        ),
        sa.CheckConstraint(
            "in_flight_entry IS NULL OR json_typeof(in_flight_entry) IN ('object','array')",
            name="ck_adapter_artifact_item_in_flight_json",
        ),
        sa.CheckConstraint("next_entry_index >= 0", name="ck_adapter_artifact_item_progress"),
        sa.CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN ("
            + _quoted(_BLOCKED_REASONS)
            + ")",
            name="ck_adapter_artifact_item_reason",
        ),
        sa.CheckConstraint(
            "((status = 'registered' AND observed_identity IS NULL AND tombstone_identity IS NULL AND deletion_plan IS NULL AND next_entry_index = 0 AND in_flight_entry IS NULL AND verified_at IS NULL AND tombstone_bound_at IS NULL AND deletion_started_at IS NULL AND directory_unlink_started_at IS NULL AND completed_at IS NULL AND blocked_at IS NULL AND blocked_reason_code IS NULL) OR (status = 'verified' AND observed_identity IS NOT NULL AND deletion_plan IS NOT NULL AND tombstone_identity IS NULL AND verified_at IS NOT NULL AND completed_at IS NULL AND blocked_at IS NULL AND blocked_reason_code IS NULL) OR (status = 'tombstone_bound' AND observed_identity IS NOT NULL AND deletion_plan IS NOT NULL AND tombstone_identity IS NOT NULL AND tombstone_bound_at IS NOT NULL AND completed_at IS NULL AND blocked_at IS NULL AND blocked_reason_code IS NULL) OR (status = 'deleting' AND tombstone_identity IS NOT NULL AND deletion_started_at IS NOT NULL AND completed_at IS NULL AND blocked_at IS NULL AND blocked_reason_code IS NULL) OR (status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL AND blocked_reason_code IS NULL AND in_flight_entry IS NULL) OR (status = 'blocked' AND blocked_at IS NOT NULL AND completed_at IS NULL AND blocked_reason_code IS NOT NULL))",
            name="ck_adapter_artifact_item_lifecycle",
        ),
        sa.CheckConstraint("version > 0", name="ck_adapter_artifact_item_version"),
    )
    op.create_index(
        "uq_adapter_artifact_item_active_surface",
        "adapter_artifact_operation_items",
        ["department_id", "surface_type", "publication_attempt_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('registered','verified','tombstone_bound','deleting')"
        ),
    )
    op.create_index(
        "ix_adapter_artifact_item_operation_status",
        "adapter_artifact_operation_items",
        ["operation_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_adapter_artifact_item_operation_status", table_name="adapter_artifact_operation_items"
    )
    op.drop_index(
        "uq_adapter_artifact_item_active_surface", table_name="adapter_artifact_operation_items"
    )
    op.drop_table("adapter_artifact_operation_items")
    op.drop_index(
        "ix_adapter_artifact_operation_department_created", table_name="adapter_artifact_operations"
    )
    op.drop_index("uq_adapter_artifact_operation_active", table_name="adapter_artifact_operations")
    op.drop_table("adapter_artifact_operations")
    op.drop_constraint(
        "ck_adapter_registry_attempt_exact_lifecycle", "adapter_registry_attempts", type_="check"
    )
    op.drop_constraint(
        "ck_adapter_import_attempt_lifecycle", "adapter_import_attempts", type_="check"
    )
    op.drop_constraint(
        "uq_adapter_registry_attempt_exact", "adapter_registry_attempts", type_="unique"
    )
    op.create_check_constraint(
        "ck_adapter_import_attempt_lifecycle",
        "adapter_import_attempts",
        "(status = 'registered' AND validated_at IS NULL AND staged_at IS NULL AND "
        "published_at IS NULL AND committed_at IS NULL AND finished_at IS NULL AND "
        "cleanup_confirmed_at IS NULL AND ownership_manifest IS NULL AND error_code IS NULL) OR "
        "(status = 'validated' AND validated_at IS NOT NULL AND staged_at IS NULL AND "
        "published_at IS NULL AND committed_at IS NULL AND finished_at IS NULL AND "
        "cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status = 'staged' AND validated_at IS NOT NULL AND staged_at IS NOT NULL AND "
        "ownership_manifest IS NOT NULL AND published_at IS NULL AND committed_at IS NULL "
        "AND finished_at IS NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status = 'published' AND validated_at IS NOT NULL AND staged_at IS NOT NULL AND "
        "published_at IS NOT NULL AND ownership_manifest IS NOT NULL AND committed_at IS NULL "
        "AND finished_at IS NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status = 'committed' AND validated_at IS NOT NULL AND staged_at IS NOT NULL AND "
        "published_at IS NOT NULL AND committed_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND ownership_manifest IS NOT NULL AND cleanup_confirmed_at IS NULL AND error_code IS NULL) OR "
        "(status IN ('failed','abandoned') AND finished_at IS NOT NULL AND committed_at IS NULL "
        "AND cleanup_confirmed_at IS NULL AND error_code IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_adapter_registry_attempt_exact_lifecycle",
        "adapter_registry_attempts",
        "cleanup_confirmed_at IS NULL AND ((status = 'registered' AND worker_id IS NULL "
        "AND claimed_at IS NULL AND staged_at IS NULL AND published_at IS NULL "
        "AND finished_at IS NULL AND ownership_manifest IS NULL AND error_code IS NULL) OR "
        "(status = 'running' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL "
        "AND staged_at IS NULL AND published_at IS NULL AND finished_at IS NULL "
        "AND ownership_manifest IS NULL AND error_code IS NULL) OR "
        "(status = 'staged' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL "
        "AND staged_at IS NOT NULL AND published_at IS NULL AND finished_at IS NULL "
        "AND ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
        "(status = 'published' AND worker_id IS NOT NULL AND claimed_at IS NOT NULL "
        "AND staged_at IS NOT NULL AND published_at IS NOT NULL AND finished_at IS NULL "
        "AND ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
        "(status = 'succeeded' AND staged_at IS NOT NULL AND published_at IS NOT NULL "
        "AND finished_at IS NOT NULL AND ownership_manifest IS NOT NULL AND error_code IS NULL) OR "
        "(status IN ('validation_failed','failed','reclaimed') AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL))",
    )

# ruff: noqa: E501
"""Add immutable Phase 12.4 RAG runtime routing snapshots."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_phase12_adapter_runtime_routing"
down_revision = "0016_phase12_adapter_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_rag_run_error_code", "rag_answer_runs", type_="check")
    op.create_check_constraint(
        "ck_rag_run_error_code",
        "rag_answer_runs",
        "error_code IS NULL OR error_code IN ("
        "'runtime_unavailable','runtime_timeout','query_embedding_failed',"
        "'invalid_query_embedding','qdrant_unavailable','retrieval_authority_failed',"
        "'source_artifact_missing','source_artifact_mismatch','source_changed',"
        "'generation_failed','generation_timeout','invalid_generation_response',"
        "'invalid_citation','adapter_runtime_unavailable','adapter_runtime_timeout',"
        "'adapter_load_failed','adapter_runtime_target_mismatch','deployment_authority_changed',"
        "'department_unavailable','database_unavailable')",
    )
    op.create_unique_constraint(
        "uq_adapter_deployment_id_department",
        "department_adapter_deployments",
        ["id", "department_id"],
    )
    op.create_table(
        "rag_answer_runtime_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("deployment_id", sa.Uuid()),
        sa.Column("deployment_version", sa.Integer(), nullable=False),
        sa.Column("deployment_row_version", sa.Integer()),
        sa.Column("base_model_id", sa.String(200), nullable=False),
        sa.Column("base_model_revision", sa.String(64), nullable=False),
        sa.Column("adapter_id", sa.Uuid()),
        sa.Column("adapter_version", sa.Integer()),
        sa.Column("review_id", sa.Uuid()),
        sa.Column("review_version", sa.Integer()),
        sa.Column("evaluation_id", sa.Uuid()),
        sa.Column("evaluation_version", sa.Integer()),
        sa.Column("suite_id", sa.Uuid()),
        sa.Column("suite_version", sa.Integer()),
        sa.Column("registry_attempt_id", sa.Uuid()),
        sa.Column("registry_attempt_version", sa.Integer()),
        sa.Column("registry_publication_attempt_id", sa.Uuid()),
        sa.Column("registry_attempt_number", sa.Integer()),
        sa.Column("registry_execution_scope_id", sa.Uuid()),
        sa.Column("registry_manifest_sha256", sa.String(64)),
        sa.Column("adapter_config_sha256", sa.String(64)),
        sa.Column("adapter_config_byte_size", sa.BigInteger()),
        sa.Column("adapter_model_sha256", sa.String(64)),
        sa.Column("adapter_model_byte_size", sa.BigInteger()),
        sa.Column("dependency_id", sa.Uuid()),
        sa.Column("dependency_version", sa.Integer()),
        sa.Column("runtime_contract_version", sa.String(100), nullable=False),
        sa.Column("target_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "department_id"],
            ["rag_answer_runs.id", "rag_answer_runs.department_id"],
            name="fk_rag_runtime_snapshot_run_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id", "department_id"],
            ["department_adapter_deployments.id", "department_adapter_deployments.department_id"],
            name="fk_rag_runtime_snapshot_deployment_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "department_id", name="uq_rag_runtime_snapshot_run_scope"),
        sa.CheckConstraint(
            "target_kind IN ('base','adapter')",
            name="ck_rag_runtime_snapshot_target_kind",
        ),
        sa.CheckConstraint(
            "runtime_contract_version = 'phase12-adapter-runtime-routing-v1'",
            name="ck_rag_runtime_snapshot_contract",
        ),
        sa.CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca'",
            name="ck_rag_runtime_snapshot_base_model",
        ),
        sa.CheckConstraint(
            "(deployment_id IS NULL AND deployment_version = 0 AND deployment_row_version IS NULL) OR "
            "(deployment_id IS NOT NULL AND deployment_version > 0 AND deployment_row_version > 0)",
            name="ck_rag_runtime_snapshot_deployment_versions",
        ),
        sa.CheckConstraint(
            "(target_kind = 'base' AND adapter_id IS NULL AND adapter_version IS NULL "
            "AND review_id IS NULL AND review_version IS NULL AND evaluation_id IS NULL "
            "AND evaluation_version IS NULL AND suite_id IS NULL AND suite_version IS NULL "
            "AND registry_attempt_id IS NULL AND registry_attempt_version IS NULL "
            "AND registry_publication_attempt_id IS NULL AND registry_attempt_number IS NULL "
            "AND registry_execution_scope_id IS NULL AND registry_manifest_sha256 IS NULL "
            "AND adapter_config_sha256 IS NULL AND adapter_config_byte_size IS NULL "
            "AND adapter_model_sha256 IS NULL AND adapter_model_byte_size IS NULL "
            "AND dependency_id IS NULL AND dependency_version IS NULL) OR "
            "(target_kind = 'adapter' AND deployment_id IS NOT NULL AND adapter_id IS NOT NULL "
            "AND adapter_version > 0 AND review_id IS NOT NULL AND review_version > 0 "
            "AND evaluation_id IS NOT NULL AND evaluation_version > 0 AND suite_id IS NOT NULL "
            "AND suite_version > 0 AND registry_attempt_id IS NOT NULL "
            "AND registry_attempt_version > 0 AND registry_publication_attempt_id IS NOT NULL "
            "AND registry_attempt_number > 0 AND registry_execution_scope_id IS NOT NULL "
            "AND registry_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND adapter_config_byte_size > 0 "
            "AND adapter_model_sha256 ~ '^[0-9a-f]{64}$' AND adapter_model_byte_size > 0 "
            "AND dependency_id IS NOT NULL AND dependency_version > 0)",
            name="ck_rag_runtime_snapshot_target_shape",
        ),
        sa.CheckConstraint(
            "target_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_rag_runtime_snapshot_fingerprint",
        ),
    )
    op.create_index(
        "ix_rag_runtime_snapshot_running_adapter",
        "rag_answer_runtime_snapshots",
        ["department_id", "adapter_id", "adapter_version"],
        postgresql_where=sa.text("target_kind = 'adapter'"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_rag_run_error_code", "rag_answer_runs", type_="check")
    op.create_check_constraint(
        "ck_rag_run_error_code",
        "rag_answer_runs",
        "error_code IS NULL OR error_code IN ("
        "'runtime_unavailable','runtime_timeout','query_embedding_failed',"
        "'invalid_query_embedding','qdrant_unavailable','retrieval_authority_failed',"
        "'source_artifact_missing','source_artifact_mismatch','source_changed',"
        "'generation_failed','generation_timeout','invalid_generation_response',"
        "'invalid_citation','department_unavailable','database_unavailable')",
    )
    op.drop_index(
        "ix_rag_runtime_snapshot_running_adapter", table_name="rag_answer_runtime_snapshots"
    )
    op.drop_table("rag_answer_runtime_snapshots")
    op.drop_constraint(
        "uq_adapter_deployment_id_department",
        "department_adapter_deployments",
        type_="unique",
    )

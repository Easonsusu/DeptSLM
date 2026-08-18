"""PostgreSQL persistence models through Phase 12.1E-A."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.adapter_contract import (
    ADAPTER_CONFIG_CONTRACT_VERSION,
    ADAPTER_INTAKE_CONTRACT_VERSION,
    ADAPTER_SOURCE_CONTRACT_VERSION,
    ADAPTER_TENSOR_CONTRACT_VERSION,
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    EXPECTED_TENSOR_BYTES,
    EXPECTED_TENSOR_COUNT,
    EXPECTED_TENSOR_ELEMENTS,
    PEFT_FORMAT_REFERENCE_VERSION,
    SAFETENSORS_FORMAT_REFERENCE_VERSION,
)
from app.auth import DepartmentRole, MembershipStatus

USER_STATUSES = ("active", "suspended", "revoked")
DEPARTMENT_STATUSES = ("active", "archived")
MEMBERSHIP_STATUSES = tuple(item.value for item in MembershipStatus)
DEPARTMENT_ROLES = tuple(item.value for item in DepartmentRole)
AUDIT_RESULTS = ("allowed", "denied")
DOCUMENT_STATUSES = ("stored", "deleted")
DOCUMENT_MEDIA_TYPES = ("application/pdf", "text/plain", "text/markdown")
EXTRACTION_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
EXTRACTION_ERROR_CODES = (
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
VECTOR_INDEXING_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
VECTOR_INDEXING_ERROR_CODES = (
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
RAG_ANSWER_STATUSES = ("running", "answered", "insufficient_information", "failed")
RAG_ANSWER_ERROR_CODES = (
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
RAG_FEEDBACK_SENTIMENTS = ("helpful", "unhelpful", "report")
RAG_FEEDBACK_STATUSES = ("open", "triaged", "resolved", "dismissed")
RAG_FEEDBACK_REASON_CODES = (
    "clear",
    "complete",
    "well_supported",
    "useful_citations",
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
RAG_FEEDBACK_RESOLVED_CODES = (
    "confirmed_quality_issue",
    "confirmed_safety_issue",
    "addressed_externally",
    "no_action_required",
)
RAG_FEEDBACK_DISMISSED_CODES = (
    "duplicate",
    "not_reproducible",
    "out_of_scope",
    "no_issue_found",
)
EVALUATION_SUITE_STATUSES = ("active", "archived")
EVALUATION_RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
EVALUATION_GATE_STATUSES = ("pending", "passed", "failed")
EVALUATION_CASE_EXPECTED_STATUSES = ("answered", "insufficient_information")
EVALUATION_CASE_ACTUAL_STATUSES = ("answered", "insufficient_information", "failed")
EVALUATION_ERROR_CODES = (
    "suite_artifact_missing",
    "suite_artifact_mismatch",
    "suite_contract_invalid",
    "suite_source_stale",
    "department_unavailable",
    "requester_unauthorized",
    "database_unavailable",
    "qdrant_unavailable",
    "retrieval_authority_failed",
    "source_artifact_missing",
    "source_artifact_mismatch",
    "runtime_unavailable",
    "runtime_timeout",
    "invalid_query_embedding",
    "generation_failed",
    "invalid_generation_response",
    "invalid_citation",
    "result_publication_failed",
    "claim_lost",
    "cancelled",
)
ADAPTER_EVALUATION_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
ADAPTER_EVALUATION_TARGETS = ("baseline", "candidate")
ADAPTER_EVALUATION_GATE_STATUSES = ("pending", "passed", "failed")
ADAPTER_EVALUATION_ERROR_CODES = (
    "adapter_unavailable",
    "adapter_authority_changed",
    "adapter_artifact_missing",
    "adapter_artifact_mismatch",
    "suite_unavailable",
    "suite_authority_changed",
    "department_unavailable",
    "requester_unauthorized",
    "qdrant_unavailable",
    "retrieval_authority_failed",
    "source_artifact_missing",
    "source_artifact_mismatch",
    "base_runtime_unavailable",
    "base_runtime_timeout",
    "candidate_runtime_unavailable",
    "candidate_runtime_timeout",
    "candidate_adapter_load_failed",
    "invalid_generation_response",
    "invalid_citation",
    "result_publication_failed",
    "claim_lost",
    "worker_shutdown",
    "cancelled",
    "database_unavailable",
)
ADAPTER_REVIEW_STATUSES = ("pending", "approved", "rejected", "archived")
ADAPTER_REVIEW_ACTIONS = ("start", "approve", "reject", "archive")
ADAPTER_DEPLOYMENT_TARGETS = ("base", "adapter")
ADAPTER_DEPLOYMENT_OPERATION_TYPES = ("promote", "rollback_adapter", "rollback_base")
ADAPTER_DEPLOYMENT_OPERATION_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)
ADAPTER_DEPLOYMENT_EVENT_TYPES = (
    "promote",
    "rollback_adapter",
    "rollback_base",
    "rollback_retention_release",
)
ADAPTER_ROLLBACK_RETENTION_STATUSES = ("active", "released")
ADAPTER_ROLLBACK_RELEASE_REASONS = ("reactivated", "manual_release")
ADAPTER_GOVERNANCE_ERROR_CODES = (
    "adapter_unavailable",
    "adapter_authority_changed",
    "review_unavailable",
    "review_authority_changed",
    "evaluation_unavailable",
    "evaluation_authority_changed",
    "evaluation_gate_failed",
    "suite_authority_changed",
    "registry_artifact_missing",
    "registry_artifact_mismatch",
    "registry_artifact_unsafe",
    "rollback_target_unavailable",
    "deployment_version_conflict",
    "deployment_operation_conflict",
    "purge_conflict",
    "claim_lost",
    "cancelled",
    "worker_shutdown",
    "worker_timeout",
    "requester_unauthorized",
    "department_unavailable",
    "database_unavailable",
)
SFT_SOURCE_STATUSES = ("active", "archived", "purged")
SFT_IMPORT_ATTEMPT_STATUSES = (
    "registered",
    "staged",
    "published",
    "committed",
    "failed",
    "abandoned",
)
SFT_BUILD_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
SFT_BUILD_REVIEW_STATUSES = ("not_ready", "pending", "approved", "rejected", "archived", "purged")
SFT_BUILD_ERROR_CODES = (
    "source_artifact_missing",
    "source_artifact_mismatch",
    "source_contract_invalid",
    "source_authority_changed",
    "department_unavailable",
    "requester_unauthorized",
    "dataset_publication_failed",
    "claim_lost",
    "cancelled",
    "worker_shutdown",
    "database_unavailable",
)
ADAPTER_IMPORT_SOURCE_STATUSES = (
    "staging",
    "committed",
    "claimed",
    "consumed",
    "rejected",
    "abandoned",
    "purge_pending",
    "purged",
)
ADAPTER_IMPORT_ATTEMPT_STATUSES = (
    "registered",
    "validated",
    "staged",
    "published",
    "committed",
    "failed",
    "abandoned",
)
ADAPTER_IMPORT_ERROR_CODES = (
    "adapter_config_invalid",
    "adapter_config_unsupported",
    "adapter_header_invalid",
    "adapter_header_too_large",
    "adapter_file_too_large",
    "adapter_tensor_set_invalid",
    "adapter_tensor_shape_invalid",
    "adapter_tensor_dtype_invalid",
    "adapter_tensor_offsets_invalid",
    "adapter_tensor_size_invalid",
    "adapter_input_invalid",
    "adapter_input_unsafe",
    "adapter_source_changed",
    "adapter_source_publication_failed",
    "adapter_source_authority_changed",
    "department_unavailable",
    "requester_unauthorized",
    "database_unavailable",
)
ADAPTER_REGISTRY_STATUSES = (
    "queued",
    "running",
    "validated",
    "validation_failed",
    "failed",
    "purge_pending",
    "purged",
)
ADAPTER_REGISTRY_ATTEMPT_STATUSES = (
    "registered",
    "running",
    "staged",
    "published",
    "succeeded",
    "validation_failed",
    "failed",
    "reclaimed",
)
ADAPTER_REGISTRY_DEPENDENCY_STATUSES = ("active", "released")
ADAPTER_REGISTRY_ERROR_CODES = (
    "adapter_source_unavailable",
    "adapter_source_artifact_mismatch",
    "adapter_source_authority_changed",
    "training_job_unavailable",
    "training_job_artifact_mismatch",
    "training_job_authority_changed",
    "dataset_authority_changed",
    "adapter_config_invalid",
    "adapter_config_unsupported",
    "adapter_header_invalid",
    "adapter_header_too_large",
    "adapter_file_too_large",
    "adapter_tensor_set_invalid",
    "adapter_tensor_shape_invalid",
    "adapter_tensor_dtype_invalid",
    "adapter_tensor_offsets_invalid",
    "adapter_tensor_size_invalid",
    "adapter_registry_manifest_invalid",
    "adapter_registry_publication_failed",
    "adapter_registry_authority_changed",
    "department_unavailable",
    "requester_unauthorized",
    "claim_lost",
    "worker_shutdown",
    "worker_timeout",
    "database_unavailable",
)
ADAPTER_PURGE_BLOCKED_REASONS = (
    "purge_authority_changed",
    "purge_manifest_invalid",
    "purge_permissions_invalid",
    "purge_path_unsafe",
    "purge_tombstone_conflict",
    "purge_dependency_active",
    "purge_operation_conflict",
    "purge_database_unavailable",
)


class Base(DeclarativeBase):
    pass


def utc_timestamp() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identity_issuer_subject"),
        CheckConstraint("issuer ~ '[^[:space:]]'", name="ck_user_identity_issuer_nonempty"),
        CheckConstraint("subject ~ '[^[:space:]]'", name="ck_user_identity_subject_nonempty"),
        CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name="ck_user_identity_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_department_slug"),
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'",
            name="ck_department_slug_format",
        ),
        CheckConstraint("length(slug) BETWEEN 2 AND 63", name="ck_department_slug_length"),
        CheckConstraint(
            "length(btrim(display_name)) BETWEEN 1 AND 200",
            name="ck_department_display_name_length",
        ),
        CheckConstraint("status IN ('active','archived')", name="ck_department_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "department_id", name="uq_membership_user_department"),
        CheckConstraint(
            "role IN ('system_admin','department_admin','instructor','student','viewer')",
            name="ck_membership_role",
        ),
        CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name="ck_membership_status",
        ),
        Index("ix_membership_department_status", "department_id", "status"),
        Index("ix_membership_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "department_id", name="uq_document_id_department"),
        CheckConstraint(
            "original_filename ~ '[^[:space:]]'",
            name="ck_document_filename_nonempty",
        ),
        CheckConstraint(
            "char_length(original_filename) <= 255",
            name="ck_document_filename_char_length",
        ),
        CheckConstraint(
            "octet_length(original_filename) <= 255",
            name="ck_document_filename_byte_length",
        ),
        CheckConstraint(
            "media_type IN ('application/pdf','text/plain','text/markdown')",
            name="ck_document_media_type",
        ),
        CheckConstraint("byte_size > 0", name="ck_document_byte_size_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_document_sha256"),
        CheckConstraint("status IN ('stored','deleted')", name="ck_document_status"),
        CheckConstraint("version > 0", name="ck_document_version_positive"),
        CheckConstraint(
            "(status = 'stored' AND deleted_at IS NULL AND deleted_by_user_id IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL AND deleted_by_user_id IS NOT NULL)",
            name="ck_document_deletion_lifecycle",
        ),
        Index("ix_document_department_status_created", "department_id", "status", "created_at"),
        Index("ix_document_department_sha256", "department_id", "sha256"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    uploaded_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="stored")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DocumentExtraction(Base):
    __tablename__ = "document_extractions"
    __table_args__ = (
        UniqueConstraint(
            "id", "department_id", "document_id", name="uq_extraction_id_department_document"
        ),
        ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_extraction_document_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["retry_of_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_extraction_retry_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_extraction_status",
        ),
        CheckConstraint(
            "pipeline_version ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_pipeline_version",
        ),
        CheckConstraint(
            "normalization_version ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_normalization_version",
        ),
        CheckConstraint(
            "chunking_version ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_chunking_version",
        ),
        CheckConstraint(
            "parser_name IS NULL OR parser_name ~ '^[a-z0-9][a-z0-9._-]{0,99}$'",
            name="ck_extraction_parser_name",
        ),
        CheckConstraint(
            "parser_version IS NULL OR parser_version ~ '^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,99}$'",
            name="ck_extraction_parser_version",
        ),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_extraction_source_sha256"),
        CheckConstraint("source_byte_size > 0", name="ck_extraction_source_size"),
        CheckConstraint(
            "normalized_sha256 IS NULL OR normalized_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_extraction_normalized_sha256",
        ),
        CheckConstraint(
            "normalized_byte_size IS NULL OR normalized_byte_size > 0",
            name="ck_extraction_normalized_size",
        ),
        CheckConstraint(
            "output_byte_size IS NULL OR output_byte_size > 0", name="ck_extraction_output_size"
        ),
        CheckConstraint(
            "chunk_count IS NULL OR chunk_count >= 0", name="ck_extraction_chunk_count"
        ),
        CheckConstraint("attempt_number > 0", name="ck_extraction_attempt"),
        CheckConstraint("version > 0", name="ck_extraction_version"),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'source_missing','source_integrity_mismatch','unsupported_media_type','invalid_utf8',"
            "'invalid_pdf','encrypted_pdf','page_limit_exceeded','extraction_timeout',"
            "'extraction_output_limit','no_extractable_text','chunk_limit_exceeded',"
            "'extraction_quota_exceeded','parser_failed','storage_unavailable',"
            "'database_unavailable','document_unavailable','claim_lost','worker_shutdown')",
            name="ck_extraction_error_code",
        ),
        CheckConstraint(
            "(status = 'queued' AND worker_id IS NULL AND claim_token IS NULL "
            "AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND parser_name IS NULL AND parser_version IS NULL AND normalized_sha256 IS NULL "
            "AND normalized_byte_size IS NULL AND output_byte_size IS NULL AND chunk_count IS NULL "
            "AND error_code IS NULL) OR status <> 'queued'",
            name="ck_extraction_queued_lifecycle",
        ),
        CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND normalized_sha256 IS NULL AND normalized_byte_size IS NULL "
            "AND output_byte_size IS NULL AND chunk_count IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_extraction_running_lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND parser_name IS NOT NULL AND parser_version IS NOT NULL "
            "AND normalized_sha256 IS NOT NULL AND normalized_byte_size IS NOT NULL "
            "AND output_byte_size IS NOT NULL AND chunk_count IS NOT NULL AND error_code IS NULL) "
            "OR status <> 'succeeded'",
            name="ck_extraction_succeeded_lifecycle",
        ),
        CheckConstraint(
            "(status IN ('failed','cancelled') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL "
            "AND normalized_sha256 IS NULL AND normalized_byte_size IS NULL "
            "AND output_byte_size IS NULL AND chunk_count IS NULL) "
            "OR status NOT IN ('failed','cancelled')",
            name="ck_extraction_failure_lifecycle",
        ),
        Index("ix_extraction_department_status_created", "department_id", "status", "created_at"),
        Index("ix_extraction_document_status_created", "document_id", "status", "created_at"),
        Index("ix_extraction_claim", "status", "lease_expires_at", "created_at"),
        Index(
            "ix_extraction_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "uq_extraction_active_document",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        Index(
            "uq_extraction_succeeded_pipeline",
            "document_id",
            "source_sha256",
            "pipeline_version",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    retry_of_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_name: Mapped[str | None] = mapped_column(String(100))
    parser_version: Mapped[str | None] = mapped_column(String(100))
    normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    chunking_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    normalized_sha256: Mapped[str | None] = mapped_column(String(64))
    normalized_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    output_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["extraction_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_chunk_extraction_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_chunk_document_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_chunk_ordinal"),
        CheckConstraint("char_start >= 0 AND char_end > char_start", name="ck_chunk_char_range"),
        CheckConstraint("byte_size > 0", name="ck_chunk_byte_size"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_chunk_content_sha256"),
        CheckConstraint("provenance_kind IN ('page','line')", name="ck_chunk_provenance_kind"),
        CheckConstraint(
            "(provenance_kind = 'page' AND page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND page_start > 0 AND page_end >= page_start "
            "AND line_start IS NULL AND line_end IS NULL) "
            "OR (provenance_kind = 'line' AND line_start IS NOT NULL AND line_end IS NOT NULL "
            "AND line_start > 0 AND line_end >= line_start "
            "AND page_start IS NULL AND page_end IS NULL)",
            name="ck_chunk_provenance_range",
        ),
        UniqueConstraint("extraction_id", "ordinal", name="uq_chunk_extraction_ordinal"),
        UniqueConstraint(
            "id",
            "department_id",
            "document_id",
            "extraction_id",
            name="uq_chunk_scope",
        ),
        Index("ix_chunk_department_document", "department_id", "document_id", "ordinal"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    extraction_id: Mapped[UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    char_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = utc_timestamp()


class DocumentVectorIndexing(Base):
    __tablename__ = "document_vector_indexings"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "department_id",
            "document_id",
            "extraction_id",
            name="uq_vector_indexing_scope",
        ),
        ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_vector_indexing_document_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["extraction_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_vector_indexing_extraction_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_vector_indexing_status",
        ),
        CheckConstraint(
            "embedding_pipeline_version = 'phase6-qwen3-embedding-v1'",
            name="ck_vector_indexing_pipeline",
        ),
        CheckConstraint(
            "embedding_model_id = 'Qwen/Qwen3-Embedding-0.6B'",
            name="ck_vector_indexing_model_id",
        ),
        CheckConstraint(
            "embedding_model_revision = 'd23109d65ca9fdf61eef614209744716f337f50f'",
            name="ck_vector_indexing_model_revision",
        ),
        CheckConstraint("embedding_dimension = 1024", name="ck_vector_indexing_dimension"),
        CheckConstraint("distance = 'cosine'", name="ck_vector_indexing_distance"),
        CheckConstraint(
            "vector_schema_version = 'phase6-qdrant-chunks-v1'",
            name="ck_vector_indexing_schema",
        ),
        CheckConstraint(
            "qdrant_collection = 'deptslm_chunks_qwen3_0_6b_1024_v1'",
            name="ck_vector_indexing_collection",
        ),
        CheckConstraint("expected_chunk_count > 0", name="ck_vector_indexing_expected_count"),
        CheckConstraint(
            "point_count IS NULL OR point_count >= 0", name="ck_vector_indexing_point_count"
        ),
        CheckConstraint("attempt_number > 0", name="ck_vector_indexing_attempt"),
        CheckConstraint("version > 0", name="ck_vector_indexing_version"),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'document_unavailable','extraction_unavailable','chunk_artifact_missing',"
            "'chunk_artifact_mismatch','embedding_model_unavailable','embedding_failed',"
            "'embedding_timeout','invalid_embedding','qdrant_unavailable',"
            "'qdrant_schema_mismatch','qdrant_write_failed','qdrant_verification_failed',"
            "'qdrant_cleanup_failed','claim_lost','worker_shutdown','database_unavailable')",
            name="ck_vector_indexing_error_code",
        ),
        CheckConstraint(
            "(status = 'queued' AND worker_id IS NULL AND claim_token IS NULL "
            "AND vector_attempt_id IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL "
            "AND started_at IS NULL AND finished_at IS NULL AND point_count IS NULL "
            "AND error_code IS NULL) OR status <> 'queued'",
            name="ck_vector_indexing_queued_lifecycle",
        ),
        CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND vector_attempt_id IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND point_count IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_vector_indexing_running_lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND vector_attempt_id IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NULL AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND point_count = expected_chunk_count "
            "AND error_code IS NULL) OR status <> 'succeeded'",
            name="ck_vector_indexing_succeeded_lifecycle",
        ),
        CheckConstraint(
            "(status IN ('failed','cancelled') AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND point_count IS NULL AND error_code IS NOT NULL) "
            "OR status NOT IN ('failed','cancelled')",
            name="ck_vector_indexing_failure_lifecycle",
        ),
        Index(
            "ix_vector_indexing_department_status_created",
            "department_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_vector_indexing_document_extraction_status",
            "document_id",
            "extraction_id",
            "status",
        ),
        Index("ix_vector_indexing_claim", "status", "lease_expires_at", "created_at"),
        Index(
            "ix_vector_indexing_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "uq_vector_indexing_active_pipeline",
            "extraction_id",
            "embedding_pipeline_version",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        Index(
            "uq_vector_indexing_succeeded_contract",
            "extraction_id",
            "embedding_model_revision",
            "embedding_dimension",
            "vector_schema_version",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    extraction_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    retry_of_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    embedding_pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    distance: Mapped[str] = mapped_column(String(16), nullable=False)
    vector_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    qdrant_collection: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    point_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    vector_attempt_id: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RagAnswerRun(Base):
    """Content-free metadata for one non-streaming grounded-answer attempt."""

    __tablename__ = "rag_answer_runs"
    __table_args__ = (
        UniqueConstraint("id", "department_id", name="uq_rag_run_department"),
        CheckConstraint(
            "status IN ('running','answered','insufficient_information','failed')",
            name="ck_rag_run_status",
        ),
        CheckConstraint(
            "question_char_count BETWEEN 1 AND 2000",
            name="ck_rag_run_question_chars",
        ),
        CheckConstraint(
            "retrieval_candidate_count IS NULL OR retrieval_candidate_count >= 0",
            name="ck_rag_run_candidate_count",
        ),
        CheckConstraint(
            "retrieval_authorized_count IS NULL OR retrieval_authorized_count >= 0",
            name="ck_rag_run_authorized_count",
        ),
        CheckConstraint(
            "selected_source_count IS NULL OR selected_source_count BETWEEN 0 AND 8",
            name="ck_rag_run_selected_count",
        ),
        CheckConstraint(
            "query_embedding_pipeline_version = 'phase7-qwen3-query-embedding-v1'",
            name="ck_rag_run_query_pipeline",
        ),
        CheckConstraint(
            "query_embedding_model_id = 'Qwen/Qwen3-Embedding-0.6B'",
            name="ck_rag_run_embedding_model",
        ),
        CheckConstraint(
            "query_embedding_model_revision = 'd23109d65ca9fdf61eef614209744716f337f50f'",
            name="ck_rag_run_embedding_revision",
        ),
        CheckConstraint(
            "generation_model_id = 'Qwen/Qwen3-0.6B'",
            name="ck_rag_run_generation_model",
        ),
        CheckConstraint(
            "generation_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca'",
            name="ck_rag_run_generation_revision",
        ),
        CheckConstraint(
            "prompt_version = 'phase7-grounded-answer-prompt-v1'",
            name="ck_rag_run_prompt_version",
        ),
        CheckConstraint(
            "answer_contract_version = 'phase7-grounded-answer-v1'",
            name="ck_rag_run_answer_contract",
        ),
        CheckConstraint(
            "minimum_score BETWEEN -1.0 AND 1.0",
            name="ck_rag_run_minimum_score",
        ),
        CheckConstraint("version > 0", name="ck_rag_run_version"),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'runtime_unavailable','runtime_timeout','query_embedding_failed',"
            "'invalid_query_embedding','qdrant_unavailable','retrieval_authority_failed',"
            "'source_artifact_missing','source_artifact_mismatch','source_changed',"
            "'generation_failed','generation_timeout','invalid_generation_response',"
            "'invalid_citation','adapter_runtime_unavailable','adapter_runtime_timeout',"
            "'adapter_load_failed','adapter_runtime_target_mismatch','deployment_authority_changed',"
            "'department_unavailable','database_unavailable')",
            name="ck_rag_run_error_code",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL "
            "AND retrieval_candidate_count IS NULL "
            "AND retrieval_authorized_count IS NULL "
            "AND selected_source_count IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_rag_run_running_lifecycle",
        ),
        CheckConstraint(
            "(status = 'answered' AND finished_at IS NOT NULL "
            "AND retrieval_candidate_count IS NOT NULL "
            "AND retrieval_authorized_count IS NOT NULL "
            "AND selected_source_count BETWEEN 1 AND 8 "
            "AND retrieval_candidate_count >= retrieval_authorized_count "
            "AND retrieval_authorized_count >= selected_source_count "
            "AND error_code IS NULL) OR status <> 'answered'",
            name="ck_rag_run_answered_lifecycle",
        ),
        CheckConstraint(
            "(status = 'insufficient_information' AND finished_at IS NOT NULL "
            "AND retrieval_candidate_count IS NOT NULL "
            "AND retrieval_authorized_count IS NOT NULL "
            "AND selected_source_count BETWEEN 0 AND 8 "
            "AND retrieval_candidate_count >= retrieval_authorized_count "
            "AND retrieval_authorized_count >= selected_source_count "
            "AND error_code IS NULL) OR status <> 'insufficient_information'",
            name="ck_rag_run_insufficient_lifecycle",
        ),
        CheckConstraint(
            "(status = 'failed' AND finished_at IS NOT NULL "
            "AND selected_source_count IS NULL AND error_code IS NOT NULL) "
            "OR status <> 'failed'",
            name="ck_rag_run_failed_lifecycle",
        ),
        Index("ix_rag_run_department_created", "department_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    question_char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_candidate_count: Mapped[int | None] = mapped_column(Integer)
    retrieval_authorized_count: Mapped[int | None] = mapped_column(Integer)
    selected_source_count: Mapped[int | None] = mapped_column(Integer)
    query_embedding_pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    query_embedding_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    query_embedding_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    generation_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    answer_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    minimum_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = utc_timestamp()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RagAnswerRuntimeSnapshot(Base):
    """Immutable server-owned Phase 12.4 generation target for one RAG run."""

    __tablename__ = "rag_answer_runtime_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "department_id"],
            ["rag_answer_runs.id", "rag_answer_runs.department_id"],
            name="fk_rag_runtime_snapshot_run_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["deployment_id", "department_id"],
            ["department_adapter_deployments.id", "department_adapter_deployments.department_id"],
            name="fk_rag_runtime_snapshot_deployment_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "department_id", name="uq_rag_runtime_snapshot_run_scope"),
        CheckConstraint(
            "target_kind IN ('base','adapter')",
            name="ck_rag_runtime_snapshot_target_kind",
        ),
        CheckConstraint(
            "runtime_contract_version = 'phase12-adapter-runtime-routing-v1'",
            name="ck_rag_runtime_snapshot_contract",
        ),
        CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca'",
            name="ck_rag_runtime_snapshot_base_model",
        ),
        CheckConstraint(
            "(deployment_id IS NULL AND deployment_version = 0 AND "
            "deployment_row_version IS NULL) OR "
            "(deployment_id IS NOT NULL AND deployment_version > 0 AND deployment_row_version > 0)",
            name="ck_rag_runtime_snapshot_deployment_versions",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "target_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_rag_runtime_snapshot_fingerprint",
        ),
        Index(
            "ix_rag_runtime_snapshot_running_adapter",
            "department_id",
            "adapter_id",
            "adapter_version",
            postgresql_where=text("target_kind = 'adapter'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    deployment_id: Mapped[UUID | None] = mapped_column()
    deployment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    deployment_row_version: Mapped[int | None] = mapped_column(Integer)
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_id: Mapped[UUID | None] = mapped_column()
    adapter_version: Mapped[int | None] = mapped_column(Integer)
    review_id: Mapped[UUID | None] = mapped_column()
    review_version: Mapped[int | None] = mapped_column(Integer)
    evaluation_id: Mapped[UUID | None] = mapped_column()
    evaluation_version: Mapped[int | None] = mapped_column(Integer)
    suite_id: Mapped[UUID | None] = mapped_column()
    suite_version: Mapped[int | None] = mapped_column(Integer)
    registry_attempt_id: Mapped[UUID | None] = mapped_column()
    registry_attempt_version: Mapped[int | None] = mapped_column(Integer)
    registry_publication_attempt_id: Mapped[UUID | None] = mapped_column()
    registry_attempt_number: Mapped[int | None] = mapped_column(Integer)
    registry_execution_scope_id: Mapped[UUID | None] = mapped_column()
    registry_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter_config_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter_config_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    adapter_model_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter_model_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    dependency_id: Mapped[UUID | None] = mapped_column()
    dependency_version: Mapped[int | None] = mapped_column(Integer)
    runtime_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = utc_timestamp()


class RagAnswerCitation(Base):
    """Department-scoped provenance metadata for an actually referenced source."""

    __tablename__ = "rag_answer_citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "department_id"],
            ["rag_answer_runs.id", "rag_answer_runs.department_id"],
            name="fk_rag_citation_run_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            name="fk_rag_citation_document_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["extraction_id", "department_id", "document_id"],
            [
                "document_extractions.id",
                "document_extractions.department_id",
                "document_extractions.document_id",
            ],
            name="fk_rag_citation_extraction_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint("source_label ~ '^S[1-8]$'", name="ck_rag_citation_source_label"),
        CheckConstraint("rank BETWEEN 1 AND 8", name="ck_rag_citation_rank"),
        CheckConstraint("ordinal >= 0", name="ck_rag_citation_ordinal"),
        CheckConstraint(
            "retrieval_score BETWEEN -1.0 AND 1.0",
            name="ck_rag_citation_score",
        ),
        CheckConstraint(
            "provenance_kind IN ('page','line')",
            name="ck_rag_citation_provenance_kind",
        ),
        CheckConstraint(
            "(provenance_kind = 'page' AND page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND page_start > 0 AND page_end >= page_start "
            "AND line_start IS NULL AND line_end IS NULL) OR "
            "(provenance_kind = 'line' AND line_start IS NOT NULL AND line_end IS NOT NULL "
            "AND line_start > 0 AND line_end >= line_start "
            "AND page_start IS NULL AND page_end IS NULL)",
            name="ck_rag_citation_provenance_range",
        ),
        UniqueConstraint("run_id", "source_label", name="uq_rag_citation_run_label"),
        UniqueConstraint("run_id", "rank", name="uq_rag_citation_run_rank"),
        UniqueConstraint("run_id", "chunk_id", name="uq_rag_citation_run_chunk"),
        UniqueConstraint("id", "department_id", "run_id", name="uq_rag_citation_id_department_run"),
        Index("ix_rag_citation_department_run", "department_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    extraction_id: Mapped[UUID] = mapped_column(nullable=False)
    indexing_id: Mapped[UUID] = mapped_column(nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(nullable=False)
    source_label: Mapped[str] = mapped_column(String(3), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    provenance_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = utc_timestamp()


class RagAnswerFeedback(Base):
    """Immutable structured feedback metadata for one completed answer run."""

    __tablename__ = "rag_answer_feedback"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "department_id"],
            ["rag_answer_runs.id", "rag_answer_runs.department_id"],
            name="fk_rag_feedback_run_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", "run_id", name="uq_rag_feedback_id_department_run"),
        UniqueConstraint(
            "department_id",
            "run_id",
            "submitted_by_user_id",
            name="uq_rag_feedback_owner",
        ),
        CheckConstraint(
            "sentiment IN ('helpful','unhelpful','report')",
            name="ck_rag_feedback_sentiment",
        ),
        CheckConstraint(
            "status IN ('open','triaged','resolved','dismissed')",
            name="ck_rag_feedback_status",
        ),
        CheckConstraint("version > 0", name="ck_rag_feedback_version"),
        CheckConstraint("expires_at > created_at", name="ck_rag_feedback_expiry"),
        CheckConstraint(
            "(status = 'open' AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL "
            "AND resolution_code IS NULL) OR "
            "(status = 'triaged' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND resolution_code IS NULL) OR "
            "(status = 'resolved' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND resolution_code IN "
            "('confirmed_quality_issue','confirmed_safety_issue','addressed_externally',"
            "'no_action_required')) OR "
            "(status = 'dismissed' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND resolution_code IN "
            "('duplicate','not_reproducible','out_of_scope','no_issue_found'))",
            name="ck_rag_feedback_lifecycle",
        ),
        Index(
            "ix_rag_feedback_owner_lookup",
            "department_id",
            "run_id",
            "submitted_by_user_id",
        ),
        Index(
            "ix_rag_feedback_review_queue",
            "department_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_rag_feedback_expiry_purge",
            "department_id",
            "expires_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    submitted_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RagAnswerFeedbackReason(Base):
    """Server-ordered reviewed reason code without free text."""

    __tablename__ = "rag_answer_feedback_reasons"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feedback_id", "department_id", "run_id"],
            [
                "rag_answer_feedback.id",
                "rag_answer_feedback.department_id",
                "rag_answer_feedback.run_id",
            ],
            name="fk_rag_feedback_reason_parent_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("rank BETWEEN 1 AND 5", name="ck_rag_feedback_reason_rank"),
        CheckConstraint(
            "reason_code IN ('clear','complete','well_supported','useful_citations',"
            "'incorrect','unsupported_claim','missing_information','wrong_citation',"
            "'irrelevant_source','unsafe_content','formatting_problem',"
            "'insufficient_when_expected','other_unspecified')",
            name="ck_rag_feedback_reason_code",
        ),
        UniqueConstraint("feedback_id", "reason_code", name="uq_rag_feedback_reason_code"),
    )

    feedback_id: Mapped[UUID] = mapped_column(primary_key=True)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    rank: Mapped[int] = mapped_column(primary_key=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = utc_timestamp()


class RagAnswerFeedbackSourceTarget(Base):
    """Exact citation target from the same feedback run and department."""

    __tablename__ = "rag_answer_feedback_source_targets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feedback_id", "department_id", "run_id"],
            [
                "rag_answer_feedback.id",
                "rag_answer_feedback.department_id",
                "rag_answer_feedback.run_id",
            ],
            name="fk_rag_feedback_target_parent_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["citation_id", "department_id", "run_id"],
            [
                "rag_answer_citations.id",
                "rag_answer_citations.department_id",
                "rag_answer_citations.run_id",
            ],
            name="fk_rag_feedback_target_citation_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("rank BETWEEN 1 AND 8", name="ck_rag_feedback_target_rank"),
        UniqueConstraint("feedback_id", "citation_id", name="uq_rag_feedback_target_citation"),
    )

    feedback_id: Mapped[UUID] = mapped_column(primary_key=True)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    citation_id: Mapped[UUID] = mapped_column(nullable=False)
    rank: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = utc_timestamp()


class EvaluationSuiteImportAttempt(Base):
    """Durable, content-free ownership record for an external suite publication."""

    __tablename__ = "evaluation_suite_import_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('registered','staged','published','committed','failed','abandoned')",
            name="ck_evaluation_suite_import_attempt_status",
        ),
        CheckConstraint(
            "artifact_manifest_sha256 IS NULL OR artifact_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_suite_import_attempt_manifest_hash",
        ),
        CheckConstraint(
            "canonical_cases_sha256 IS NULL OR canonical_cases_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_suite_import_attempt_cases_hash",
        ),
        CheckConstraint(
            "canonical_cases_byte_size IS NULL OR canonical_cases_byte_size > 0",
            name="ck_evaluation_suite_import_attempt_cases_size",
        ),
        CheckConstraint(
            "(artifact_manifest_sha256 IS NULL AND canonical_cases_sha256 IS NULL "
            "AND canonical_cases_byte_size IS NULL) OR "
            "(artifact_manifest_sha256 IS NOT NULL AND canonical_cases_sha256 IS NOT NULL "
            "AND canonical_cases_byte_size IS NOT NULL)",
            name="ck_evaluation_suite_import_attempt_artifact_compatibility",
        ),
        CheckConstraint(
            "(status = 'registered' AND artifact_manifest_sha256 IS NULL "
            "AND staged_at IS NULL AND published_at IS NULL AND committed_at IS NULL "
            "AND failed_at IS NULL AND abandoned_at IS NULL AND cleanup_confirmed_at IS NULL) OR "
            "(status = 'staged' AND artifact_manifest_sha256 IS NOT NULL "
            "AND staged_at IS NOT NULL AND published_at IS NULL AND committed_at IS NULL "
            "AND failed_at IS NULL AND abandoned_at IS NULL AND cleanup_confirmed_at IS NULL) OR "
            "(status = 'published' AND artifact_manifest_sha256 IS NOT NULL "
            "AND staged_at IS NOT NULL AND published_at IS NOT NULL "
            "AND published_at >= staged_at AND committed_at IS NULL "
            "AND failed_at IS NULL AND abandoned_at IS NULL AND cleanup_confirmed_at IS NULL) OR "
            "(status = 'committed' AND artifact_manifest_sha256 IS NOT NULL "
            "AND staged_at IS NOT NULL AND published_at IS NOT NULL "
            "AND committed_at IS NOT NULL AND published_at >= staged_at "
            "AND committed_at >= published_at AND failed_at IS NULL AND abandoned_at IS NULL "
            "AND cleanup_confirmed_at IS NULL) OR "
            "(status = 'failed' AND committed_at IS NULL AND failed_at IS NOT NULL "
            "AND abandoned_at IS NULL AND (published_at IS NULL OR staged_at IS NOT NULL) "
            "AND ((staged_at IS NULL AND artifact_manifest_sha256 IS NULL) OR "
            "(staged_at IS NOT NULL AND artifact_manifest_sha256 IS NOT NULL)) "
            "AND (staged_at IS NULL OR failed_at >= staged_at) "
            "AND (published_at IS NULL OR failed_at >= published_at) "
            "AND cleanup_confirmed_at IS NOT NULL AND cleanup_confirmed_at >= failed_at) OR "
            "(status = 'abandoned' AND committed_at IS NULL AND failed_at IS NULL "
            "AND abandoned_at IS NOT NULL AND (published_at IS NULL OR staged_at IS NOT NULL) "
            "AND ((staged_at IS NULL AND artifact_manifest_sha256 IS NULL) OR "
            "(staged_at IS NOT NULL AND artifact_manifest_sha256 IS NOT NULL)) "
            "AND (staged_at IS NULL OR abandoned_at >= staged_at) "
            "AND (published_at IS NULL OR abandoned_at >= published_at) "
            "AND cleanup_confirmed_at IS NOT NULL AND cleanup_confirmed_at >= abandoned_at)",
            name="ck_evaluation_suite_import_attempt_lifecycle",
        ),
        CheckConstraint("version > 0", name="ck_evaluation_suite_import_attempt_version"),
        Index(
            "ix_evaluation_suite_import_attempt_department_status_created",
            "department_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    imported_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    suite_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    stage_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    artifact_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    canonical_cases_sha256: Mapped[str | None] = mapped_column(String(64))
    canonical_cases_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EvaluationArtifactReconciliationOperation(Base):
    """Durable content-free batch authority for explicit artifact reconciliation."""

    __tablename__ = "evaluation_artifact_reconciliation_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('registered','completed','completed_with_blocks')",
            name="ck_evaluation_artifact_reconciliation_operation_status",
        ),
        CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL) OR "
            "(status IN ('completed','completed_with_blocks') AND completed_at IS NOT NULL)",
            name="ck_evaluation_artifact_reconciliation_operation_lifecycle",
        ),
        CheckConstraint(
            "version > 0", name="ck_evaluation_artifact_reconciliation_operation_version"
        ),
        Index(
            "ix_eval_artifact_reconcile_op_dept_status_created",
            "department_id",
            "status",
            "created_at",
        ),
        UniqueConstraint(
            "id",
            "department_id",
            name="uq_evaluation_artifact_reconciliation_operation_department",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EvaluationArtifactReconciliationOperationItem(Base):
    """Exact content-free ownership tuples recoverable after a reconciliation crash."""

    __tablename__ = "evaluation_artifact_reconciliation_operation_items"
    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('evaluation_run','evaluation_suite_import_attempt')",
            name="ck_evaluation_artifact_reconciliation_item_resource_type",
        ),
        CheckConstraint(
            "status IN ('registered','completed','blocked')",
            name="ck_evaluation_artifact_reconciliation_item_status",
        ),
        CheckConstraint(
            "(resource_type = 'evaluation_run' AND suite_id IS NOT NULL "
            "AND ownership_attempt_id IS NOT NULL AND stage_id IS NOT NULL "
            "AND stage_id = ownership_attempt_id AND attempt_number IS NOT NULL "
            "AND attempt_number > 0 AND code_revision ~ '^[0-9a-f]{40}$') OR "
            "(resource_type = 'evaluation_suite_import_attempt' AND suite_id IS NOT NULL "
            "AND ownership_attempt_id = resource_id AND stage_id IS NOT NULL "
            "AND attempt_number IS NULL AND code_revision IS NULL)",
            name="ck_evaluation_artifact_reconciliation_item_ownership",
        ),
        CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'blocked' AND completed_at IS NULL AND blocked_at IS NOT NULL "
            "AND blocked_reason_code IN ('staging_path_unsafe','artifact_ownership_mismatch',"
            "'artifact_manifest_invalid','artifact_permissions_invalid'))",
            name="ck_evaluation_artifact_reconciliation_item_lifecycle",
        ),
        UniqueConstraint(
            "operation_id",
            "resource_type",
            "resource_id",
            name="uq_evaluation_artifact_reconciliation_operation_item",
        ),
        Index(
            "ix_evaluation_artifact_reconciliation_item_operation_status",
            "operation_id",
            "status",
        ),
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            [
                "evaluation_artifact_reconciliation_operations.id",
                "evaluation_artifact_reconciliation_operations.department_id",
            ],
            name="fk_evaluation_artifact_reconciliation_item_operation_scope",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    ownership_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    stage_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int | None] = mapped_column(Integer)
    code_revision: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_reason_code: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = utc_timestamp()


class EvaluationSuite(Base):
    """Immutable department-owned evaluation suite metadata."""

    __tablename__ = "evaluation_suites"
    __table_args__ = (
        UniqueConstraint("id", "department_id", name="uq_evaluation_suite_department"),
        CheckConstraint("status IN ('active','archived')", name="ck_evaluation_suite_status"),
        CheckConstraint(
            "suite_contract_version = 'phase9-evaluation-suite-v1'",
            name="ck_evaluation_suite_contract",
        ),
        CheckConstraint(
            "artifact_contract_version = 'phase9-evaluation-artifact-v1'",
            name="ck_evaluation_suite_artifact_contract",
        ),
        CheckConstraint(
            "metric_contract_version = 'phase9-deterministic-metrics-v1'",
            name="ck_evaluation_suite_metric_contract",
        ),
        CheckConstraint(
            "answer_normalization_version = 'phase9-answer-normalization-v1'",
            name="ck_evaluation_suite_normalization_contract",
        ),
        CheckConstraint(
            "gate_policy_version = 'phase9-quality-gates-v1'",
            name="ck_evaluation_suite_gate_contract",
        ),
        CheckConstraint("case_count BETWEEN 1 AND 500", name="ck_evaluation_suite_case_count"),
        CheckConstraint(
            "answered_case_count >= 0 AND insufficient_case_count >= 0 "
            "AND answered_case_count + insufficient_case_count = case_count",
            name="ck_evaluation_suite_case_totals",
        ),
        CheckConstraint(
            "answered_case_count > 0",
            name="ck_evaluation_suite_applicable_metrics",
        ),
        CheckConstraint(
            "artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND canonical_cases_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_suite_hashes",
        ),
        CheckConstraint(
            "canonical_cases_byte_size BETWEEN 1 AND 16777216",
            name="ck_evaluation_suite_artifact_size",
        ),
        CheckConstraint(
            "retrieval_recall_at_5_min BETWEEN 0 AND 1 "
            "AND retrieval_mrr_at_20_min BETWEEN 0 AND 1 "
            "AND answer_status_accuracy_min BETWEEN 0 AND 1 "
            "AND citation_precision_min BETWEEN 0 AND 1 "
            "AND citation_recall_min BETWEEN 0 AND 1 "
            "AND normalized_exact_match_min BETWEEN 0 AND 1 "
            "AND character_f1_min BETWEEN 0 AND 1 "
            "AND invalid_contract_rate_max BETWEEN 0 AND 1",
            name="ck_evaluation_suite_gate_ranges",
        ),
        CheckConstraint("version > 0", name="ck_evaluation_suite_version"),
        CheckConstraint(
            "(status = 'active' AND archived_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL)",
            name="ck_evaluation_suite_lifecycle",
        ),
        Index(
            "ix_evaluation_suite_department_status_created",
            "department_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    imported_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    suite_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    metric_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    answer_normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    gate_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    answered_case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    insufficient_case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_cases_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_cases_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retrieval_recall_at_5_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    retrieval_mrr_at_20_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    answer_status_accuracy_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    citation_precision_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    citation_recall_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    normalized_exact_match_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    character_f1_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    invalid_contract_rate_max: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EvaluationRun(Base):
    """Content-free metadata and claim state for one evaluation execution."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["suite_id", "department_id"],
            ["evaluation_suites.id", "evaluation_suites.department_id"],
            name="fk_evaluation_run_suite_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id", "department_id", "suite_id", name="uq_evaluation_run_department_suite"
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_evaluation_run_status",
        ),
        CheckConstraint(
            "gate_status IN ('pending','passed','failed')",
            name="ck_evaluation_run_gate_status",
        ),
        CheckConstraint(
            "runner_contract_version = 'phase9-evaluation-runner-v1'",
            name="ck_evaluation_run_runner_contract",
        ),
        CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_evaluation_run_code_revision",
        ),
        CheckConstraint(
            "query_embedding_pipeline_version = 'phase7-qwen3-query-embedding-v1' "
            "AND query_embedding_model_id = 'Qwen/Qwen3-Embedding-0.6B' "
            "AND query_embedding_model_revision = "
            "'d23109d65ca9fdf61eef614209744716f337f50f' "
            "AND query_embedding_dimension = 1024 "
            "AND query_embedding_distance = 'cosine'",
            name="ck_evaluation_run_embedding_contract",
        ),
        CheckConstraint(
            "generation_model_id = 'Qwen/Qwen3-0.6B' "
            "AND generation_model_revision = "
            "'c1899de289a04d12100db370d81485cdf75e47ca' "
            "AND prompt_version = 'phase7-grounded-answer-prompt-v1' "
            "AND answer_contract_version = 'phase7-grounded-answer-v1'",
            name="ck_evaluation_run_generation_contract",
        ),
        CheckConstraint(
            "qdrant_collection = 'deptslm_chunks_qwen3_0_6b_1024_v1' "
            "AND vector_schema_version = 'phase6-qdrant-chunks-v1'",
            name="ck_evaluation_run_vector_contract",
        ),
        CheckConstraint(
            "base_seed BETWEEN 0 AND 9223372036854775807",
            name="ck_evaluation_run_seed",
        ),
        CheckConstraint(
            "case_count BETWEEN 1 AND 500 AND completed_case_count BETWEEN 0 AND case_count "
            "AND answered_case_count >= 0 AND insufficient_case_count >= 0 "
            "AND answered_case_count + insufficient_case_count <= completed_case_count",
            name="ck_evaluation_run_counts",
        ),
        CheckConstraint(
            "(retrieval_recall_at_5 IS NULL OR retrieval_recall_at_5 BETWEEN 0 AND 1) "
            "AND (retrieval_recall_at_10 IS NULL OR retrieval_recall_at_10 BETWEEN 0 AND 1) "
            "AND (retrieval_recall_at_20 IS NULL OR retrieval_recall_at_20 BETWEEN 0 AND 1) "
            "AND (retrieval_mrr_at_20 IS NULL OR retrieval_mrr_at_20 BETWEEN 0 AND 1) "
            "AND (answer_status_accuracy IS NULL OR answer_status_accuracy BETWEEN 0 AND 1) "
            "AND (citation_precision IS NULL OR citation_precision BETWEEN 0 AND 1) "
            "AND (citation_recall IS NULL OR citation_recall BETWEEN 0 AND 1) "
            "AND (normalized_exact_match IS NULL OR normalized_exact_match BETWEEN 0 AND 1) "
            "AND (character_f1 IS NULL OR character_f1 BETWEEN 0 AND 1) "
            "AND (invalid_contract_rate IS NULL OR invalid_contract_rate BETWEEN 0 AND 1)",
            name="ck_evaluation_run_metric_ranges",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'suite_artifact_missing','suite_artifact_mismatch','suite_contract_invalid',"
            "'suite_source_stale','department_unavailable','requester_unauthorized',"
            "'database_unavailable','qdrant_unavailable','retrieval_authority_failed',"
            "'source_artifact_missing','source_artifact_mismatch','runtime_unavailable',"
            "'runtime_timeout','invalid_query_embedding','generation_failed',"
            "'invalid_generation_response','invalid_citation','result_publication_failed',"
            "'claim_lost','cancelled')",
            name="ck_evaluation_run_error_code",
        ),
        CheckConstraint(
            "(result_manifest_sha256 IS NULL OR "
            "result_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(result_summary_sha256 IS NULL OR result_summary_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (case_results_sha256 IS NULL OR case_results_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (case_results_byte_size IS NULL OR case_results_byte_size > 0)",
            name="ck_evaluation_run_artifacts",
        ),
        CheckConstraint("attempt_number > 0 AND version > 0", name="ck_evaluation_run_versions"),
        CheckConstraint(
            "(status = 'queued' AND gate_status = 'pending' "
            "AND worker_id IS NULL AND claim_token IS NULL AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL AND started_at IS NULL AND finished_at IS NULL "
            "AND cancellation_requested_at IS NULL "
            "AND completed_case_count = 0 AND answered_case_count = 0 "
            "AND insufficient_case_count = 0 AND failed_gate_count IS NULL "
            "AND result_manifest_sha256 IS NULL AND result_summary_sha256 IS NULL "
            "AND case_results_sha256 IS NULL AND case_results_byte_size IS NULL "
            "AND publication_attempt_id IS NULL AND error_code IS NULL) OR status <> 'queued'",
            name="ck_evaluation_run_queued_lifecycle",
        ),
        CheckConstraint(
            "(status = 'running' AND gate_status = 'pending' "
            "AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND publication_attempt_id IS NOT NULL "
            "AND failed_gate_count IS NULL AND result_manifest_sha256 IS NULL "
            "AND result_summary_sha256 IS NULL AND case_results_sha256 IS NULL "
            "AND case_results_byte_size IS NULL AND error_code IS NULL) "
            "OR status <> 'running'",
            name="ck_evaluation_run_running_lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND gate_status IN ('passed','failed') "
            "AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND completed_case_count = case_count "
            "AND cancellation_requested_at IS NULL "
            "AND answered_case_count + insufficient_case_count = case_count "
            "AND retrieval_recall_at_5 IS NOT NULL AND retrieval_recall_at_10 IS NOT NULL "
            "AND retrieval_recall_at_20 IS NOT NULL AND retrieval_mrr_at_20 IS NOT NULL "
            "AND answer_status_accuracy IS NOT NULL AND citation_precision IS NOT NULL "
            "AND citation_recall IS NOT NULL AND normalized_exact_match IS NOT NULL "
            "AND character_f1 IS NOT NULL AND invalid_contract_rate IS NOT NULL "
            "AND failed_gate_count IS NOT NULL AND failed_gate_count BETWEEN 0 AND 8 "
            "AND result_manifest_sha256 IS NOT NULL AND result_summary_sha256 IS NOT NULL "
            "AND case_results_sha256 IS NOT NULL AND case_results_byte_size IS NOT NULL "
            "AND publication_attempt_id IS NOT NULL AND error_code IS NULL) "
            "OR status <> 'succeeded'",
            name="ck_evaluation_run_succeeded_lifecycle",
        ),
        CheckConstraint(
            "(status = 'failed' AND gate_status = 'pending' "
            "AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND error_code IS NOT NULL "
            "AND cancellation_requested_at IS NULL "
            "AND failed_gate_count IS NULL AND result_manifest_sha256 IS NULL "
            "AND result_summary_sha256 IS NULL AND case_results_sha256 IS NULL "
            "AND case_results_byte_size IS NULL) OR status <> 'failed'",
            name="ck_evaluation_run_failed_lifecycle",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND gate_status = 'pending' "
            "AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND error_code = 'cancelled' "
            "AND cancellation_requested_at IS NOT NULL "
            "AND failed_gate_count IS NULL AND result_manifest_sha256 IS NULL "
            "AND result_summary_sha256 IS NULL AND case_results_sha256 IS NULL "
            "AND case_results_byte_size IS NULL) OR status <> 'cancelled'",
            name="ck_evaluation_run_cancelled_lifecycle",
        ),
        Index(
            "ix_evaluation_run_department_status_created",
            "department_id",
            "status",
            "created_at",
        ),
        Index("ix_evaluation_run_suite_created", "department_id", "suite_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    gate_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    runner_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    query_embedding_pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    query_embedding_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    query_embedding_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    query_embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    query_embedding_distance: Mapped[str] = mapped_column(String(16), nullable=False)
    generation_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    generation_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    answer_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    qdrant_collection: Mapped[str] = mapped_column(String(128), nullable=False)
    vector_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    base_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answered_case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retrieval_recall_at_5: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    retrieval_recall_at_10: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    retrieval_recall_at_20: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    retrieval_mrr_at_20: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    answer_status_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    citation_precision: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    citation_recall: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    normalized_exact_match: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    character_f1: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    invalid_contract_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    failed_gate_count: Mapped[int | None] = mapped_column(Integer)
    result_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    result_summary_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    publication_attempt_id: Mapped[UUID | None] = mapped_column()
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EvaluationCaseResult(Base):
    """Numeric and content-free per-case evaluation outcome."""

    __tablename__ = "evaluation_case_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "department_id", "suite_id"],
            [
                "evaluation_runs.id",
                "evaluation_runs.department_id",
                "evaluation_runs.suite_id",
            ],
            name="fk_evaluation_case_result_run_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "expected_status IN ('answered','insufficient_information')",
            name="ck_evaluation_case_expected_status",
        ),
        CheckConstraint(
            "actual_status IN ('answered','insufficient_information','failed')",
            name="ck_evaluation_case_actual_status",
        ),
        CheckConstraint(
            "relevant_chunk_count >= 0 AND retrieved_relevant_at_5 >= 0 "
            "AND retrieved_relevant_at_10 >= retrieved_relevant_at_5 "
            "AND retrieved_relevant_at_20 >= retrieved_relevant_at_10 "
            "AND retrieved_relevant_at_20 <= relevant_chunk_count "
            "AND cited_count >= 0 AND cited_relevant_count BETWEEN 0 AND cited_count "
            "AND cited_relevant_count <= relevant_chunk_count",
            name="ck_evaluation_case_counts",
        ),
        CheckConstraint(
            "reciprocal_rank_at_20 BETWEEN 0 AND 1 "
            "AND citation_precision BETWEEN 0 AND 1 "
            "AND citation_recall BETWEEN 0 AND 1 "
            "AND normalized_exact_match IN (0,1) "
            "AND character_f1 BETWEEN 0 AND 1",
            name="ck_evaluation_case_metrics",
        ),
        CheckConstraint(
            "(expected_status = 'answered' AND relevant_chunk_count BETWEEN 1 AND 8) OR "
            "(expected_status = 'insufficient_information' AND relevant_chunk_count = 0)",
            name="ck_evaluation_case_expected_contract",
        ),
        CheckConstraint(
            "(actual_status = 'failed' AND error_code IS NOT NULL) OR "
            "(actual_status <> 'failed' AND error_code IS NULL)",
            name="ck_evaluation_case_error_lifecycle",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'suite_artifact_missing','suite_artifact_mismatch','suite_contract_invalid',"
            "'suite_source_stale','department_unavailable','requester_unauthorized',"
            "'database_unavailable','qdrant_unavailable','retrieval_authority_failed',"
            "'source_artifact_missing','source_artifact_mismatch','runtime_unavailable',"
            "'runtime_timeout','invalid_query_embedding','generation_failed',"
            "'invalid_generation_response','invalid_citation','result_publication_failed',"
            "'claim_lost','cancelled')",
            name="ck_evaluation_case_error_code",
        ),
        Index(
            "ix_evaluation_case_result_department_run",
            "department_id",
            "run_id",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    case_id: Mapped[UUID] = mapped_column(primary_key=True)
    expected_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_status: Mapped[str] = mapped_column(String(32), nullable=False)
    relevant_chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_relevant_at_5: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_relevant_at_10: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_relevant_at_20: Mapped[int] = mapped_column(Integer, nullable=False)
    reciprocal_rank_at_20: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    status_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cited_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cited_relevant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    citation_precision: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    citation_recall: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    normalized_exact_match: Mapped[Decimal] = mapped_column(Numeric(1, 0), nullable=False)
    character_f1: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    answer_contract_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    case_gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = utc_timestamp()


class AdapterEvaluationRun(Base):
    """Content-free paired baseline/candidate evaluation authority."""

    __tablename__ = "adapter_evaluation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_evaluation_run_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["suite_id", "department_id"],
            ["evaluation_suites.id", "evaluation_suites.department_id"],
            name="fk_adapter_evaluation_run_suite_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
            name="fk_adapter_evaluation_run_registry_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_evaluation_run_requester",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dependency_id", "department_id", "adapter_id"],
            [
                "adapter_upstream_dependencies.id",
                "adapter_upstream_dependencies.department_id",
                "adapter_upstream_dependencies.adapter_id",
            ],
            name="fk_adapter_evaluation_run_dependency",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "department_id",
            "adapter_id",
            "suite_id",
            name="uq_adapter_evaluation_run_scope",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_adapter_evaluation_run_status",
        ),
        CheckConstraint(
            "gate_status IN ('pending','passed','failed')",
            name="ck_adapter_evaluation_run_gate_status",
        ),
        CheckConstraint(
            "runner_contract_version = 'phase12-adapter-evaluation-v1' AND "
            "artifact_contract_version = 'phase12-adapter-evaluation-artifact-v1' AND "
            "metric_contract_version = 'phase9-deterministic-metrics-v1' AND "
            "gate_policy_version = 'phase9-quality-gates-v1' AND "
            "seed_policy_version = 'phase12-adapter-evaluation-seed-v1'",
            name="ck_adapter_evaluation_run_contracts",
        ),
        CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca'",
            name="ck_adapter_evaluation_run_model",
        ),
        CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$' AND base_seed BETWEEN 0 AND 9223372036854775807 "
            "AND expected_adapter_version > 0 AND adapter_version > 0 "
            "AND registry_attempt_version > 0 AND registry_attempt_number > 0 "
            "AND dependency_version > 0 AND suite_version > 0 AND case_count BETWEEN 1 AND 500 "
            "AND completed_case_count BETWEEN 0 AND case_count "
            "AND attempt_number > 0 AND version > 0",
            name="ck_adapter_evaluation_run_versions_counts",
        ),
        CheckConstraint(
            "registry_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "registry_adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND "
            "registry_adapter_model_sha256 ~ '^[0-9a-f]{64}$' AND "
            "registry_adapter_config_byte_size > 0 AND registry_adapter_model_byte_size > 0 AND "
            "suite_artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "suite_canonical_cases_sha256 ~ '^[0-9a-f]{64}$' AND "
            "suite_canonical_cases_byte_size > 0",
            name="ck_adapter_evaluation_run_authority_digests",
        ),
        CheckConstraint(
            "retrieval_recall_at_5_min BETWEEN 0 AND 1 "
            "AND retrieval_mrr_at_20_min BETWEEN 0 AND 1 "
            "AND answer_status_accuracy_min BETWEEN 0 AND 1 "
            "AND citation_precision_min BETWEEN 0 AND 1 "
            "AND citation_recall_min BETWEEN 0 AND 1 "
            "AND normalized_exact_match_min BETWEEN 0 AND 1 "
            "AND character_f1_min BETWEEN 0 AND 1 "
            "AND invalid_contract_rate_max BETWEEN 0 AND 1",
            name="ck_adapter_evaluation_run_gate_ranges",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_EVALUATION_ERROR_CODES)
            + ")",
            name="ck_adapter_evaluation_run_error_code",
        ),
        CheckConstraint(
            "(result_manifest_sha256 IS NULL OR result_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(result_summary_sha256 IS NULL OR result_summary_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(case_results_sha256 IS NULL OR case_results_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(case_results_byte_size IS NULL OR case_results_byte_size > 0)",
            name="ck_adapter_evaluation_run_artifacts",
        ),
        CheckConstraint(
            "(status = 'queued' AND gate_status = 'pending' AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND claimed_at IS NULL "
            "AND started_at IS NULL AND finished_at IS NULL AND cancellation_requested_at IS NULL "
            "AND cancelled_at IS NULL AND result_publication_attempt_id IS NULL "
            "AND completed_case_count = 0) "
            "OR status <> 'queued'",
            name="ck_adapter_evaluation_run_queued_lifecycle",
        ),
        CheckConstraint(
            "(status = 'running' AND gate_status = 'pending' AND worker_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND claimed_at IS NOT NULL AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND cancelled_at IS NULL "
            "AND result_publication_attempt_id IS NOT NULL) OR status <> 'running'",
            name="ck_adapter_evaluation_run_running_lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND gate_status IN ('passed','failed') "
            "AND worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NOT NULL AND result_manifest_sha256 IS NOT NULL "
            "AND result_summary_sha256 IS NOT NULL AND case_results_sha256 IS NOT NULL "
            "AND completed_case_count = case_count AND cancellation_requested_at IS NULL "
            "AND cancelled_at IS NULL AND error_code IS NULL) "
            "OR status <> 'succeeded'",
            name="ck_adapter_evaluation_run_succeeded_lifecycle",
        ),
        CheckConstraint(
            "(status = 'failed' AND gate_status = 'pending' AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND cancelled_at IS NULL AND error_code IS NOT NULL) OR status <> 'failed'",
            name="ck_adapter_evaluation_run_failed_lifecycle",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND gate_status = 'pending' AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND cancellation_requested_at IS NOT NULL AND cancelled_at IS NOT NULL "
            "AND error_code = 'cancelled') OR status <> 'cancelled'",
            name="ck_adapter_evaluation_run_cancelled_lifecycle",
        ),
        Index(
            "uq_adapter_evaluation_run_active",
            "department_id",
            "adapter_id",
            "suite_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        Index(
            "ix_adapter_evaluation_run_department_status_created",
            "department_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    gate_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    expected_adapter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    registry_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    registry_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_adapter_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_adapter_config_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    registry_adapter_model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_adapter_model_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dependency_id: Mapped[UUID] = mapped_column(nullable=False)
    dependency_version: Mapped[int] = mapped_column(Integer, nullable=False)
    suite_version: Mapped[int] = mapped_column(Integer, nullable=False)
    suite_artifact_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    suite_canonical_cases_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    suite_canonical_cases_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retrieval_recall_at_5_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    retrieval_mrr_at_20_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    answer_status_accuracy_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    citation_precision_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    citation_recall_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    normalized_exact_match_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    character_f1_min: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    invalid_contract_rate_max: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    metric_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    gate_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    seed_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    base_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_publication_attempt_id: Mapped[UUID | None] = mapped_column()
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    result_summary_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterEvaluationAttempt(Base):
    """Historical non-revivable worker/publication ownership for an evaluation."""

    __tablename__ = "adapter_evaluation_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_evaluation_attempt_run_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id", "department_id", "run_id", name="uq_adapter_evaluation_attempt_scope"
        ),
        UniqueConstraint("run_id", "attempt_number", name="uq_adapter_evaluation_attempt_number"),
        UniqueConstraint(
            "publication_attempt_id", name="uq_adapter_evaluation_attempt_publication"
        ),
        CheckConstraint(
            "status IN ('running','reclaimed','succeeded','failed','cancelled')",
            name="ck_adapter_evaluation_attempt_status",
        ),
        CheckConstraint(
            "attempt_number > 0 AND version > 0",
            name="ck_adapter_evaluation_attempt_versions",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_EVALUATION_ERROR_CODES)
            + ")",
            name="ck_adapter_evaluation_attempt_error_code",
        ),
        CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$' AND "
            "((status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('reclaimed','failed','cancelled') AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL) OR "
            "(status = 'succeeded' AND worker_id IS NULL AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND finished_at IS NOT NULL AND error_code IS NULL "
            "AND result_manifest_sha256 IS NOT NULL AND result_summary_sha256 IS NOT NULL "
            "AND case_results_sha256 IS NOT NULL AND case_results_byte_size > 0))",
            name="ck_adapter_evaluation_attempt_lifecycle",
        ),
        CheckConstraint(
            "(result_manifest_sha256 IS NULL OR result_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(result_summary_sha256 IS NULL OR result_summary_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(case_results_sha256 IS NULL OR case_results_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(case_results_byte_size IS NULL OR case_results_byte_size > 0)",
            name="ck_adapter_evaluation_attempt_artifacts",
        ),
        Index(
            "uq_adapter_evaluation_attempt_active",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error_code: Mapped[str | None] = mapped_column(String(64))
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    result_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    result_summary_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    claimed_at: Mapped[datetime] = utc_timestamp()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterEvaluationEvidence(Base):
    """Immutable content-free aggregate evidence for one evaluation target."""

    __tablename__ = "adapter_evaluation_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_evaluation_evidence_run_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "target", name="uq_adapter_evaluation_evidence_target"),
        CheckConstraint(
            "target IN ('baseline','candidate')",
            name="ck_adapter_evaluation_evidence_target",
        ),
        CheckConstraint(
            "gate_status IN ('passed','failed') AND failed_gate_count BETWEEN 0 AND 8",
            name="ck_adapter_evaluation_evidence_gate",
        ),
        CheckConstraint(
            "adapter_version > 0 AND base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca' AND "
            "metric_contract_version = 'phase9-deterministic-metrics-v1' AND "
            "gate_policy_version = 'phase9-quality-gates-v1' AND "
            "seed_policy_version = 'phase12-adapter-evaluation-seed-v1'",
            name="ck_adapter_evaluation_evidence_contract",
        ),
        CheckConstraint(
            "retrieval_recall_at_5 BETWEEN 0 AND 1 AND retrieval_recall_at_10 BETWEEN 0 AND 1 "
            "AND retrieval_recall_at_20 BETWEEN 0 AND 1 AND retrieval_mrr_at_20 BETWEEN 0 AND 1 "
            "AND answer_status_accuracy BETWEEN 0 AND 1 AND citation_precision BETWEEN 0 AND 1 "
            "AND citation_recall BETWEEN 0 AND 1 AND normalized_exact_match BETWEEN 0 AND 1 "
            "AND character_f1 BETWEEN 0 AND 1 AND invalid_contract_rate BETWEEN 0 AND 1",
            name="ck_adapter_evaluation_evidence_metric_ranges",
        ),
        Index(
            "ix_adapter_evaluation_evidence_department_run",
            "department_id",
            "run_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    adapter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    gate_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    seed_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    gate_status: Mapped[str] = mapped_column(String(16), nullable=False)
    failed_gate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_recall_at_5: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    retrieval_recall_at_10: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    retrieval_recall_at_20: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    retrieval_mrr_at_20: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    answer_status_accuracy: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    citation_precision: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    citation_recall: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    normalized_exact_match: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    character_f1: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    invalid_contract_rate: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    delta_retrieval_recall_at_5: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_retrieval_recall_at_10: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_retrieval_recall_at_20: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_retrieval_mrr_at_20: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_answer_status_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_citation_precision: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_citation_recall: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_normalized_exact_match: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_character_f1: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    delta_invalid_contract_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 18))
    created_at: Mapped[datetime] = utc_timestamp()


class AdapterEvaluationCaseResult(Base):
    """Numeric/content-free per-case result for one evaluation target."""

    __tablename__ = "adapter_evaluation_case_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_evaluation_case_result_run_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "target", "case_id", name="uq_adapter_evaluation_case_target"),
        CheckConstraint(
            "target IN ('baseline','candidate')",
            name="ck_adapter_evaluation_case_target",
        ),
        CheckConstraint(
            "actual_status IN ('answered','insufficient_information','failed')",
            name="ck_adapter_evaluation_case_status",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_EVALUATION_ERROR_CODES)
            + ")",
            name="ck_adapter_evaluation_case_error_code",
        ),
        CheckConstraint(
            "expected_status IN ('answered','insufficient_information') AND "
            "((expected_status = 'answered' AND relevant_chunk_count BETWEEN 1 AND 8) OR "
            "(expected_status = 'insufficient_information' AND relevant_chunk_count = 0)) AND "
            "((actual_status = 'failed' AND error_code IS NOT NULL) OR "
            "(actual_status <> 'failed' AND error_code IS NULL))",
            name="ck_adapter_evaluation_case_expected_lifecycle",
        ),
        CheckConstraint(
            "retrieval_candidate_count >= 0 AND retrieved_relevant_at_5 >= 0 AND "
            "retrieved_relevant_at_10 >= retrieved_relevant_at_5 AND "
            "retrieved_relevant_at_20 >= retrieved_relevant_at_10 AND "
            "retrieved_relevant_at_20 <= relevant_chunk_count AND cited_count >= 0 AND "
            "cited_relevant_count BETWEEN 0 AND cited_count AND "
            "cited_relevant_count <= relevant_chunk_count AND "
            "reciprocal_rank_at_20 BETWEEN 0 AND 1 AND "
            "citation_precision BETWEEN 0 AND 1 AND citation_recall BETWEEN 0 AND 1 AND "
            "normalized_exact_match BETWEEN 0 AND 1 AND character_f1 BETWEEN 0 AND 1",
            name="ck_adapter_evaluation_case_numeric_ranges",
        ),
        Index(
            "ix_adapter_evaluation_case_department_run",
            "department_id",
            "run_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    case_id: Mapped[UUID] = mapped_column(nullable=False)
    expected_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_status: Mapped[str] = mapped_column(String(32), nullable=False)
    relevant_chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_relevant_at_5: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_relevant_at_10: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_relevant_at_20: Mapped[int] = mapped_column(Integer, nullable=False)
    reciprocal_rank_at_20: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    answer_status_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cited_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cited_relevant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    citation_precision: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    citation_recall: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    normalized_exact_match: Mapped[Decimal] = mapped_column(Numeric(1, 0), nullable=False)
    character_f1: Mapped[Decimal] = mapped_column(Numeric(20, 18), nullable=False)
    answer_contract_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    case_gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = utc_timestamp()


class SftSourceBundle(Base):
    """Metadata-only ownership record for a human-authored external SFT source bundle."""

    __tablename__ = "sft_source_bundles"
    __table_args__ = (
        UniqueConstraint("id", "department_id", name="uq_sft_source_bundle_department"),
        CheckConstraint(
            "status IN ('active','archived','purged')", name="ck_sft_source_bundle_status"
        ),
        CheckConstraint(
            "artifact_contract_version = 'phase10-sft-source-v1'",
            name="ck_sft_source_bundle_artifact_contract",
        ),
        CheckConstraint(
            "normalization_version = 'phase10-sft-normalization-v1'",
            name="ck_sft_source_bundle_normalization_version",
        ),
        CheckConstraint(
            "example_contract_version = 'phase10-sft-example-v1'",
            name="ck_sft_source_bundle_example_contract",
        ),
        CheckConstraint("example_count BETWEEN 2 AND 100000", name="ck_sft_source_bundle_examples"),
        CheckConstraint("group_count >= 2", name="ck_sft_source_bundle_groups"),
        CheckConstraint(
            "source_reference_count >= example_count", name="ck_sft_source_bundle_references"
        ),
        CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$' AND examples_sha256 ~ '^[0-9a-f]{64}$' "
            "AND authority_snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sft_source_bundle_hashes",
        ),
        CheckConstraint(
            "examples_byte_size BETWEEN 1 AND 536870912", name="ck_sft_source_bundle_size"
        ),
        CheckConstraint("version > 0", name="ck_sft_source_bundle_version"),
        CheckConstraint(
            "(status = 'active' AND archived_at IS NULL AND purged_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL AND purged_at IS NULL) OR "
            "(status = 'purged' AND purged_at IS NOT NULL)",
            name="ck_sft_source_bundle_lifecycle",
        ),
        Index(
            "ix_sft_source_bundle_department_status_created",
            "department_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    imported_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    example_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_reference_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    examples_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    examples_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SftSourceImportAttempt(Base):
    """Durable content-free source-import staging ownership."""

    __tablename__ = "sft_source_import_attempts"
    __table_args__ = (
        UniqueConstraint("id", "department_id", "source_bundle_id", name="uq_sft_import_scope"),
        CheckConstraint(
            "status IN ('registered','staged','published','committed','failed','abandoned')",
            name="ck_sft_source_import_status",
        ),
        CheckConstraint("version > 0", name="ck_sft_source_import_version"),
        CheckConstraint(
            "(status = 'registered' AND staged_at IS NULL AND published_at IS NULL "
            "AND committed_at IS NULL AND failed_at IS NULL AND abandoned_at IS NULL) OR "
            "(status = 'staged' AND staged_at IS NOT NULL AND published_at IS NULL "
            "AND committed_at IS NULL AND failed_at IS NULL AND abandoned_at IS NULL) OR "
            "(status = 'published' AND staged_at IS NOT NULL AND published_at IS NOT NULL "
            "AND committed_at IS NULL AND failed_at IS NULL AND abandoned_at IS NULL) OR "
            "(status = 'committed' AND committed_at IS NOT NULL) OR "
            "(status = 'failed' AND failed_at IS NOT NULL) OR "
            "(status = 'abandoned' AND abandoned_at IS NOT NULL)",
            name="ck_sft_source_import_lifecycle",
        ),
        Index("ix_sft_source_import_department_status", "department_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    import_attempt_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    stage_id: Mapped[UUID] = mapped_column(nullable=False)
    imported_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    examples_sha256: Mapped[str | None] = mapped_column(String(64))
    authority_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    artifact_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    examples_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SftDatasetBuild(Base):
    """Content-free dataset-build state, review lifecycle, and worker claim authority."""

    __tablename__ = "sft_dataset_builds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["sft_source_bundles.id", "sft_source_bundles.department_id"],
            name="fk_sft_build_source_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", "source_bundle_id", name="uq_sft_build_scope"),
        UniqueConstraint("id", "department_id", name="uq_sft_build_department"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_sft_build_status",
        ),
        CheckConstraint(
            "review_status IN ('not_ready','pending','approved','rejected','archived','purged')",
            name="ck_sft_build_review_status",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'source_artifact_missing','source_artifact_mismatch','source_contract_invalid',"
            "'source_authority_changed','department_unavailable','requester_unauthorized',"
            "'dataset_publication_failed','claim_lost','cancelled','worker_shutdown',"
            "'worker_timeout','database_unavailable')",
            name="ck_sft_build_error_code",
        ),
        CheckConstraint("attempt_number > 0 AND version > 0", name="ck_sft_build_versions"),
        CheckConstraint(
            "artifact_contract_version = 'phase10-sft-dataset-v1'",
            name="ck_sft_build_artifact_contract",
        ),
        CheckConstraint(
            "example_contract_version = 'phase10-sft-example-v1'",
            name="ck_sft_build_example_contract",
        ),
        CheckConstraint(
            "normalization_version = 'phase10-sft-normalization-v1'",
            name="ck_sft_build_normalization_contract",
        ),
        CheckConstraint(
            "split_version = 'phase10-sft-group-split-v1'",
            name="ck_sft_build_split_contract",
        ),
        CheckConstraint("validation_ratio = 0.10", name="ck_sft_build_validation_ratio"),
        CheckConstraint(
            "source_example_count BETWEEN 2 AND 100000 AND source_group_count >= 2 "
            "AND source_reference_count >= source_example_count",
            name="ck_sft_build_source_counts",
        ),
        CheckConstraint(
            "(status = 'queued' AND review_status = 'not_ready' AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL AND publication_attempt_id IS NULL AND error_code IS NULL) OR "
            "status <> 'queued'",
            name="ck_sft_build_queued_lifecycle",
        ),
        CheckConstraint(
            "(status = 'running' AND review_status = 'not_ready' AND worker_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND publication_attempt_id IS NOT NULL AND error_code IS NULL) OR "
            "status <> 'running'",
            name="ck_sft_build_running_lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND review_status IN "
            "('pending','approved','rejected','archived','purged') "
            "AND finished_at IS NOT NULL AND train_example_count > 0 "
            "AND validation_example_count > 0 "
            "AND result_manifest_sha256 IS NOT NULL AND train_sha256 IS NOT NULL "
            "AND validation_sha256 IS NOT NULL AND provenance_sha256 IS NOT NULL "
            "AND error_code IS NULL) OR "
            "status <> 'succeeded'",
            name="ck_sft_build_succeeded_lifecycle",
        ),
        Index("ix_sft_build_department_status_created", "department_id", "status", "created_at"),
        Index("ix_sft_build_claim", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_ready")
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_attempt_id: Mapped[UUID | None] = mapped_column()
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    example_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    split_version: Mapped[str] = mapped_column(String(100), nullable=False)
    validation_ratio: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    source_example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_reference_count: Mapped[int] = mapped_column(Integer, nullable=False)
    train_example_count: Mapped[int | None] = mapped_column(Integer)
    validation_example_count: Mapped[int | None] = mapped_column(Integer)
    result_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    train_sha256: Mapped[str | None] = mapped_column(String(64))
    train_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    validation_sha256: Mapped[str | None] = mapped_column(String(64))
    validation_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    provenance_sha256: Mapped[str | None] = mapped_column(String(64))
    provenance_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    publication_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    artifact_cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    requested_at: Mapped[datetime] = utc_timestamp()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SftDatasetBuildAttempt(Base):
    """Durable, content-free ownership for every external build publication attempt."""

    __tablename__ = "sft_dataset_build_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["build_id", "department_id"],
            ["sft_dataset_builds.id", "sft_dataset_builds.department_id"],
            name="fk_sft_build_attempt_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", "build_id", name="uq_sft_build_attempt_scope"),
        UniqueConstraint("build_id", "attempt_number", name="uq_sft_build_attempt_number"),
        UniqueConstraint("publication_attempt_id", name="uq_sft_build_attempt_publication"),
        UniqueConstraint(
            "build_id",
            "department_id",
            "publication_attempt_id",
            "attempt_number",
            name="uq_sft_build_attempt_exact",
        ),
        CheckConstraint(
            "status IN ('running','reclaimed','succeeded','failed','cancelled')",
            name="ck_sft_build_attempt_status",
        ),
        CheckConstraint("attempt_number > 0 AND version > 0", name="ck_sft_build_attempt_versions"),
        CheckConstraint(
            "(status = 'running' AND claimed_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status = 'reclaimed' AND finished_at IS NOT NULL) OR "
            "(status = 'succeeded' AND published_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status IN ('failed','cancelled') AND finished_at IS NOT NULL)",
            name="ck_sft_build_attempt_lifecycle",
        ),
        Index(
            "ix_sft_build_attempt_department_status",
            "department_id",
            "status",
            "created_at",
        ),
        Index(
            "uq_sft_build_attempt_active",
            "build_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    build_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    ownership_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    registered_at: Mapped[datetime] = utc_timestamp()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SftArtifactReconciliationOperation(Base):
    """Content-free durable Phase 10 artifact reconciliation batch."""

    __tablename__ = "sft_artifact_reconciliation_operations"
    __table_args__ = (
        UniqueConstraint("id", "department_id", name="uq_sft_reconciliation_operation_scope"),
        CheckConstraint(
            "operation_type IN ('reconcile','purge')",
            name="ck_sft_reconciliation_operation_type",
        ),
        CheckConstraint(
            "status IN ('registered','completed','completed_with_blocks')",
            name="ck_sft_reconciliation_operation_status",
        ),
        CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL) OR "
            "(status IN ('completed','completed_with_blocks') AND completed_at IS NOT NULL)",
            name="ck_sft_reconciliation_operation_lifecycle",
        ),
        CheckConstraint(
            "limit_value BETWEEN 1 AND 1000", name="ck_sft_reconciliation_operation_limit"
        ),
        Index("ix_sft_reconciliation_operation_department", "department_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False, default="reconcile")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_timestamp()


class SftArtifactReconciliationOperationItem(Base):
    __tablename__ = "sft_artifact_reconciliation_operation_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            [
                "sft_artifact_reconciliation_operations.id",
                "sft_artifact_reconciliation_operations.department_id",
            ],
            name="fk_sft_reconciliation_item_operation_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('registered','completed','blocked')",
            name="ck_sft_reconciliation_item_status",
        ),
        CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'blocked' AND completed_at IS NULL AND blocked_at IS NOT NULL "
            "AND blocked_reason_code IS NOT NULL)",
            name="ck_sft_reconciliation_item_lifecycle",
        ),
        CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN "
            "('staging_path_unsafe','artifact_ownership_mismatch','artifact_manifest_invalid',"
            "'artifact_permissions_invalid','artifact_state_changed')",
            name="ck_sft_reconciliation_item_reason",
        ),
        CheckConstraint(
            "resource_type IN ('source_stage','source_final','dataset_stage','dataset_final')",
            name="ck_sft_reconciliation_item_resource_type",
        ),
        UniqueConstraint(
            "operation_id",
            "resource_type",
            "resource_id",
            "attempt_id",
            name="uq_sft_reconciliation_item",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    ownership_manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    blocked_reason_code: Mapped[str | None] = mapped_column(String(48))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_timestamp()


class AdapterImportSource(Base):
    """Content-free authority for one immutable externally produced adapter source."""

    __tablename__ = "adapter_import_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["claimed_adapter_id", "department_id", "id"],
            ["adapters.id", "adapters.department_id", "adapters.source_bundle_id"],
            name="fk_adapter_import_source_claimed_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["authoritative_attempt_id", "department_id", "id"],
            [
                "adapter_import_attempts.id",
                "adapter_import_attempts.department_id",
                "adapter_import_attempts.source_bundle_id",
            ],
            name="fk_adapter_import_source_authoritative_attempt_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", name="uq_adapter_import_source_department"),
        CheckConstraint(
            "status IN ('staging','committed','claimed','consumed','rejected','abandoned',"
            "'purge_pending','purged')",
            name="ck_adapter_import_source_status",
        ),
        CheckConstraint("version > 0", name="ck_adapter_import_source_version"),
        CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_adapter_import_source_code_revision",
        ),
        CheckConstraint(
            "(adapter_config_sha256 IS NULL OR adapter_config_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(adapter_model_sha256 IS NULL OR adapter_model_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(intake_manifest_sha256 IS NULL OR intake_manifest_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_adapter_import_source_hashes",
        ),
        CheckConstraint(
            "(adapter_config_byte_size IS NULL OR adapter_config_byte_size > 0) AND "
            "(adapter_model_byte_size IS NULL OR adapter_model_byte_size > 0) AND "
            "(intake_manifest_byte_size IS NULL OR intake_manifest_byte_size > 0) AND "
            "(tensor_payload_byte_size IS NULL OR tensor_payload_byte_size > 0)",
            name="ck_adapter_import_source_sizes",
        ),
        CheckConstraint(
            "tensor_count IS NULL OR tensor_count = " + str(EXPECTED_TENSOR_COUNT),
            name="ck_adapter_import_source_tensor_count",
        ),
        CheckConstraint(
            "tensor_element_count IS NULL OR tensor_element_count = "
            + str(EXPECTED_TENSOR_ELEMENTS),
            name="ck_adapter_import_source_tensor_elements",
        ),
        CheckConstraint(
            "tensor_dtype IS NULL OR "
            "(tensor_dtype = 'F16' AND tensor_payload_byte_size = "
            + str(EXPECTED_TENSOR_BYTES["F16"])
            + ") OR (tensor_dtype = 'BF16' AND tensor_payload_byte_size = "
            + str(EXPECTED_TENSOR_BYTES["BF16"])
            + ") OR (tensor_dtype = 'F32' AND tensor_payload_byte_size = "
            + str(EXPECTED_TENSOR_BYTES["F32"])
            + ")",
            name="ck_adapter_import_source_tensor_contract",
        ),
        CheckConstraint(
            "(status IN ('committed','claimed','consumed','purge_pending','purged') "
            "AND intake_manifest_byte_size > 0) OR "
            "(status IN ('staging','rejected','abandoned') AND intake_manifest_byte_size IS NULL)",
            name="ck_adapter_import_source_manifest_size",
        ),
        CheckConstraint(
            "source_contract_version = '" + ADAPTER_SOURCE_CONTRACT_VERSION + "' AND "
            "intake_contract_version = '" + ADAPTER_INTAKE_CONTRACT_VERSION + "' AND "
            "config_contract_version = '" + ADAPTER_CONFIG_CONTRACT_VERSION + "' AND "
            "tensor_contract_version = '" + ADAPTER_TENSOR_CONTRACT_VERSION + "' AND "
            "base_model_id = '" + BASE_MODEL_ID + "' AND "
            "base_model_revision = '" + BASE_MODEL_REVISION + "' AND "
            "base_model_license = '" + BASE_MODEL_LICENSE + "' AND "
            "peft_version = '" + PEFT_FORMAT_REFERENCE_VERSION + "' AND "
            "safetensors_format = '" + SAFETENSORS_FORMAT_REFERENCE_VERSION + "'",
            name="ck_adapter_import_source_contract",
        ),
        CheckConstraint(
            "(status = 'staging' AND authoritative_attempt_id IS NULL AND "
            "committed_at IS NULL AND rejected_at IS NULL AND abandoned_at IS NULL "
            "AND purged_at IS NULL AND error_code IS NULL AND claimed_adapter_id IS NULL "
            "AND claimed_at IS NULL AND consumed_at IS NULL) OR "
            "(status = 'committed' AND authoritative_attempt_id IS NOT NULL AND "
            "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
            "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
            "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
            "tensor_count = "
            + str(EXPECTED_TENSOR_COUNT)
            + " AND tensor_element_count = "
            + str(EXPECTED_TENSOR_ELEMENTS)
            + " AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
            "rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND "
            "error_code IS NULL AND claimed_adapter_id IS NULL AND claimed_at IS NULL "
            "AND consumed_at IS NULL) OR "
            "(status = 'rejected' AND rejected_at IS NOT NULL AND committed_at IS NULL "
            "AND abandoned_at IS NULL AND purged_at IS NULL AND "
            "authoritative_attempt_id IS NULL AND claimed_adapter_id IS NULL AND "
            "claimed_at IS NULL AND consumed_at IS NULL AND "
            "error_code IN ('adapter_config_invalid',"
            "'adapter_config_unsupported','adapter_header_invalid','adapter_header_too_large',"
            "'adapter_file_too_large','adapter_tensor_set_invalid','adapter_tensor_shape_invalid',"
            "'adapter_tensor_dtype_invalid','adapter_tensor_offsets_invalid',"
            "'adapter_tensor_size_invalid','adapter_input_invalid','adapter_input_unsafe')) OR "
            "(status = 'abandoned' AND abandoned_at IS NOT NULL AND rejected_at IS NULL "
            "AND committed_at IS NULL AND purged_at IS NULL AND "
            "authoritative_attempt_id IS NULL AND claimed_adapter_id IS NULL AND "
            "claimed_at IS NULL AND consumed_at IS NULL AND "
            "error_code IN ('adapter_source_changed',"
            "'adapter_source_publication_failed','adapter_source_authority_changed',"
            "'department_unavailable','requester_unauthorized','database_unavailable')) OR "
            "(status = 'claimed' AND "
            "authoritative_attempt_id IS NOT NULL AND "
            "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
            "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
            "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
            "tensor_count = "
            + str(EXPECTED_TENSOR_COUNT)
            + " AND tensor_element_count = "
            + str(EXPECTED_TENSOR_ELEMENTS)
            + " AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
            "rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND "
            "error_code IS NULL AND claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL "
            "AND consumed_at IS NULL) OR "
            "(status = 'consumed' AND authoritative_attempt_id IS NOT NULL AND "
            "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
            "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
            "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
            "tensor_count = "
            + str(EXPECTED_TENSOR_COUNT)
            + " AND tensor_element_count = "
            + str(EXPECTED_TENSOR_ELEMENTS)
            + " AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
            "rejected_at IS NULL AND abandoned_at IS NULL AND purged_at IS NULL AND "
            "error_code IS NULL AND claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL "
            "AND consumed_at IS NOT NULL) OR "
            "(status = 'purge_pending' AND authoritative_attempt_id IS NOT NULL AND "
            "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
            "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
            "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
            "tensor_count = "
            + str(EXPECTED_TENSOR_COUNT)
            + " AND tensor_element_count = "
            + str(EXPECTED_TENSOR_ELEMENTS)
            + " AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
            "purged_at IS NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND "
            "error_code IS NULL AND "
            "((claimed_adapter_id IS NULL AND claimed_at IS NULL AND consumed_at IS NULL) OR "
            "(claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL))) OR "
            "(status = 'purged' AND authoritative_attempt_id IS NOT NULL AND "
            "adapter_config_sha256 IS NOT NULL AND adapter_config_byte_size > 0 AND "
            "adapter_model_sha256 IS NOT NULL AND adapter_model_byte_size > 0 AND "
            "intake_manifest_sha256 IS NOT NULL AND tensor_dtype IS NOT NULL AND "
            "tensor_count = "
            + str(EXPECTED_TENSOR_COUNT)
            + " AND tensor_element_count = "
            + str(EXPECTED_TENSOR_ELEMENTS)
            + " AND tensor_payload_byte_size > 0 AND committed_at IS NOT NULL AND "
            "purged_at IS NOT NULL AND rejected_at IS NULL AND abandoned_at IS NULL AND "
            "error_code IS NULL AND "
            "((claimed_adapter_id IS NULL AND claimed_at IS NULL AND consumed_at IS NULL) OR "
            "(claimed_adapter_id IS NOT NULL AND claimed_at IS NOT NULL)))",
            name="ck_adapter_import_source_lifecycle",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_IMPORT_ERROR_CODES)
            + ")",
            name="ck_adapter_import_source_error_code",
        ),
        Index(
            "ix_adapter_import_source_department_status_created",
            "department_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    imported_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staging")
    authoritative_attempt_id: Mapped[UUID | None] = mapped_column()
    claimed_adapter_id: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    intake_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    config_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    tensor_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    base_model_license: Mapped[str] = mapped_column(String(40), nullable=False)
    peft_version: Mapped[str] = mapped_column(String(32), nullable=False)
    safetensors_format: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_config_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter_config_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    adapter_model_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter_model_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    intake_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    intake_manifest_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    tensor_dtype: Mapped[str | None] = mapped_column(String(8))
    tensor_count: Mapped[int | None] = mapped_column(Integer)
    tensor_element_count: Mapped[int | None] = mapped_column(BigInteger)
    tensor_payload_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterImportAttempt(Base):
    """Content-free ownership for every Phase 12.1B publication attempt."""

    __tablename__ = "adapter_import_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_import_attempt_source_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id", "department_id", "source_bundle_id", name="uq_adapter_import_attempt_scope"
        ),
        UniqueConstraint(
            "source_bundle_id", "attempt_number", name="uq_adapter_import_attempt_number"
        ),
        UniqueConstraint("publication_attempt_id", name="uq_adapter_import_publication_attempt"),
        UniqueConstraint(
            "id",
            "department_id",
            "source_bundle_id",
            "publication_attempt_id",
            "attempt_number",
            name="uq_adapter_import_attempt_exact",
        ),
        CheckConstraint(
            "status IN ('registered','validated','staged','published','committed','failed',"
            "'abandoned')",
            name="ck_adapter_import_attempt_status",
        ),
        CheckConstraint(
            "attempt_number > 0 AND version > 0",
            name="ck_adapter_import_attempt_versions",
        ),
        CheckConstraint(
            "ownership_manifest IS NULL OR json_typeof(ownership_manifest) = 'object'",
            name="ck_adapter_import_attempt_manifest_object",
        ),
        CheckConstraint(
            "(status = 'registered' AND validated_at IS NULL AND staged_at IS NULL AND "
            "published_at IS NULL AND committed_at IS NULL AND finished_at IS NULL AND "
            "cleanup_confirmed_at IS NULL AND ownership_manifest IS NULL AND "
            "error_code IS NULL) OR "
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
            "AND ownership_manifest IS NOT NULL AND cleanup_confirmed_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status IN ('failed','abandoned') AND finished_at IS NOT NULL AND "
            "committed_at IS NULL AND error_code IS NOT NULL AND "
            "(cleanup_confirmed_at IS NULL OR cleanup_confirmed_at >= finished_at))",
            name="ck_adapter_import_attempt_lifecycle",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_IMPORT_ERROR_CODES)
            + ")",
            name="ck_adapter_import_attempt_error_code",
        ),
        Index(
            "ix_adapter_import_attempt_department_status_created_id",
            "department_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "uq_adapter_import_attempt_active",
            "source_bundle_id",
            unique=True,
            postgresql_where=text("status IN ('registered','validated','staged','published')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    ownership_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    registered_at: Mapped[datetime] = utc_timestamp()
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Adapter(Base):
    """Content-free authority for one immutable Phase 12.1C adapter registry entry."""

    __tablename__ = "adapters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_source_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_adapter_training_job_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_build_id", "department_id"],
            ["sft_dataset_builds.id", "sft_dataset_builds.department_id"],
            name="fk_adapter_dataset_build_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
            name="fk_adapter_source_attempt_exact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "training_job_id",
                "department_id",
                "training_job_publication_attempt_id",
                "training_job_attempt_number",
            ],
            [
                "training_job_attempts.training_job_id",
                "training_job_attempts.department_id",
                "training_job_attempts.publication_attempt_id",
                "training_job_attempts.attempt_number",
            ],
            name="fk_adapter_training_attempt_exact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "dataset_build_id",
                "department_id",
                "dataset_publication_attempt_id",
                "dataset_publication_attempt_number",
            ],
            [
                "sft_dataset_build_attempts.build_id",
                "sft_dataset_build_attempts.department_id",
                "sft_dataset_build_attempts.publication_attempt_id",
                "sft_dataset_build_attempts.attempt_number",
            ],
            name="fk_adapter_dataset_attempt_exact",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", name="uq_adapter_department"),
        UniqueConstraint("source_bundle_id", "department_id", name="uq_adapter_source_scope"),
        UniqueConstraint(
            "id", "department_id", "source_bundle_id", name="uq_adapter_source_claim_scope"
        ),
        UniqueConstraint(
            "id",
            "department_id",
            "training_job_id",
            "dataset_build_id",
            name="uq_adapter_governance_scope",
        ),
        CheckConstraint(
            "status IN ('queued','running','validated','validation_failed','failed',"
            "'purge_pending','purged')",
            name="ck_adapter_status",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_REGISTRY_ERROR_CODES)
            + ")",
            name="ck_adapter_error_code",
        ),
        CheckConstraint(
            "code_revision ~ '^[0-9a-f]{40}$' AND source_code_revision ~ '^[0-9a-f]{40}$' "
            "AND training_job_code_revision ~ '^[0-9a-f]{40}$' "
            "AND dataset_code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_adapter_code_revisions",
        ),
        CheckConstraint(
            "source_contract_version = 'phase12-adapter-source-v1' AND "
            "intake_contract_version = 'phase12-adapter-intake-v1' AND "
            "config_contract_version = 'phase12-adapter-config-v1' AND "
            "tensor_contract_version = 'phase12-adapter-tensors-v1'",
            name="ck_adapter_source_contracts",
        ),
        CheckConstraint(
            "artifact_contract_version = 'phase12-adapter-artifact-v1' AND "
            "registry_manifest_contract_version = 'phase12-adapter-manifest-v1' AND "
            "declared_external_training_association IS TRUE AND "
            "training_provenance_verified IS FALSE",
            name="ck_adapter_registry_contracts",
        ),
        CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca' AND "
            "base_model_license = 'Apache-2.0' AND peft_version = '0.18.1' AND "
            "safetensors_format = '0.7.0' AND "
            "training_job_artifact_contract_version = 'phase11-training-job-v1' AND "
            "training_job_manifest_contract_version = 'phase11-training-job-manifest-v1' AND "
            "training_configuration_contract_version = 'phase11-training-config-v1' AND "
            "training_dataset_info_contract_version = 'phase11-dataset-info-v1' AND "
            "training_execution_profile_contract_version = 'phase11-execution-profile-v1' AND "
            "llamafactory_version = '0.9.5' AND "
            "training_job_profile_id IN ("
            "'phase11-qwen3-0.6b-lora-v1','phase11-qwen3-0.6b-qlora-nf4-v1') AND "
            "dataset_artifact_contract_version = 'phase10-sft-dataset-v1' AND "
            "dataset_example_contract_version = 'phase10-sft-example-v1' AND "
            "dataset_normalization_version = 'phase10-sft-normalization-v1' AND "
            "dataset_split_version = 'phase10-sft-group-split-v1' AND "
            "dataset_rights_attested IS TRUE AND evaluation_contamination_reviewed IS TRUE",
            name="ck_adapter_upstream_contracts",
        ),
        CheckConstraint(
            "source_intake_manifest_byte_size > 0 AND source_adapter_config_byte_size > 0 AND "
            "source_adapter_model_byte_size > 0 AND training_job_manifest_byte_size > 0 AND "
            "training_job_config_byte_size > 0 AND training_job_dataset_info_byte_size > 0 AND "
            "training_job_train_byte_size > 0 AND training_job_validation_byte_size > 0 AND "
            "dataset_train_byte_size > 0 AND dataset_validation_byte_size > 0 AND "
            "dataset_provenance_byte_size > 0 AND dataset_train_example_count > 0 AND "
            "dataset_validation_example_count > 0 AND dataset_source_example_count >= 2 AND "
            "dataset_source_group_count >= 2 AND "
            "dataset_source_reference_count >= dataset_source_example_count AND "
            "tensor_count = 392 AND "
            "tensor_element_count = 10092544 AND "
            "((tensor_dtype IN ('F16','BF16') AND tensor_payload_byte_size = 20185088) OR "
            "(tensor_dtype = 'F32' AND tensor_payload_byte_size = 40370176))",
            name="ck_adapter_exact_sizes",
        ),
        CheckConstraint(
            "tensor_dtype IN ('F16','BF16','F32') AND tensor_count = 392 "
            "AND tensor_element_count = 10092544 AND tensor_payload_byte_size > 0",
            name="ck_adapter_tensor_contract",
        ),
        CheckConstraint(
            "(registry_adapter_model_sha256 IS NULL AND "
            "registry_adapter_model_byte_size IS NULL) OR "
            "(source_adapter_model_sha256 = registry_adapter_model_sha256 AND "
            "source_adapter_model_byte_size = registry_adapter_model_byte_size)",
            name="ck_adapter_model_digest_match",
        ),
        CheckConstraint(
            "source_adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND "
            "source_adapter_model_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_config_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_dataset_info_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_train_sha256 ~ '^[0-9a-f]{64}$' AND "
            "training_job_validation_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_train_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_validation_sha256 ~ '^[0-9a-f]{64}$' AND "
            "dataset_provenance_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_adapter_source_hashes",
        ),
        CheckConstraint(
            "(registry_manifest_sha256 IS NULL OR registry_manifest_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(registry_adapter_config_sha256 IS NULL OR "
            "registry_adapter_config_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(registry_adapter_model_sha256 IS NULL OR "
            "registry_adapter_model_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_adapter_registry_hashes",
        ),
        CheckConstraint(
            "attempt_number > 0 AND version > 0 AND source_version > 0 "
            "AND source_attempt_version > 0 AND training_job_version > 0 "
            "AND training_job_attempt_version > 0 AND dataset_build_version > 0 "
            "AND dataset_attempt_version > 0",
            name="ck_adapter_versions",
        ),
        CheckConstraint(
            "(status = 'queued' AND worker_id IS NULL AND claim_token IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL AND validated_at IS NULL AND error_code IS NULL "
            "AND purged_at IS NULL "
            "AND verified_governance_lineage IS FALSE AND verified_artifact_compatibility IS FALSE "
            "AND registry_manifest_sha256 IS NULL AND registry_adapter_config_sha256 IS NULL "
            "AND registry_adapter_config_byte_size IS NULL "
            "AND registry_adapter_model_sha256 IS NULL "
            "AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'running' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL AND validated_at IS NULL "
            "AND error_code IS NULL AND purged_at IS NULL "
            "AND verified_governance_lineage IS FALSE "
            "AND verified_artifact_compatibility IS FALSE AND registry_manifest_sha256 IS NULL "
            "AND registry_adapter_config_sha256 IS NULL "
            "AND registry_adapter_config_byte_size IS NULL "
            "AND registry_adapter_model_sha256 IS NULL "
            "AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'validated' AND worker_id IS NULL AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND validated_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND purged_at IS NULL AND error_code IS NULL "
            "AND verified_governance_lineage IS TRUE "
            "AND verified_artifact_compatibility IS TRUE AND registry_manifest_sha256 IS NOT NULL "
            "AND registry_adapter_config_sha256 IS NOT NULL "
            "AND registry_adapter_config_byte_size > 0 "
            "AND registry_adapter_model_sha256 IS NOT NULL "
            "AND registry_adapter_model_byte_size > 0) OR "
            "(status IN ('validation_failed','failed') AND worker_id IS NULL "
            "AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND validated_at IS NULL AND finished_at IS NOT NULL "
            "AND purged_at IS NULL AND error_code IS NOT NULL "
            "AND verified_governance_lineage IS FALSE "
            "AND verified_artifact_compatibility IS FALSE "
            "AND registry_manifest_sha256 IS NULL AND registry_adapter_config_sha256 IS NULL "
            "AND registry_adapter_config_byte_size IS NULL "
            "AND registry_adapter_model_sha256 IS NULL "
            "AND registry_adapter_model_byte_size IS NULL) OR "
            "(status = 'purge_pending' AND worker_id IS NULL AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND validated_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND purged_at IS NULL AND error_code IS NULL "
            "AND verified_governance_lineage IS TRUE AND verified_artifact_compatibility IS TRUE "
            "AND registry_manifest_sha256 IS NOT NULL "
            "AND registry_adapter_config_sha256 IS NOT NULL "
            "AND registry_adapter_config_byte_size > 0 "
            "AND registry_adapter_model_sha256 IS NOT NULL "
            "AND registry_adapter_model_byte_size > 0) OR "
            "(status = 'purged' AND worker_id IS NULL AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND validated_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND purged_at IS NOT NULL AND error_code IS NULL "
            "AND verified_governance_lineage IS TRUE AND verified_artifact_compatibility IS TRUE "
            "AND registry_manifest_sha256 IS NOT NULL "
            "AND registry_adapter_config_sha256 IS NOT NULL "
            "AND registry_adapter_config_byte_size > 0 "
            "AND registry_adapter_model_sha256 IS NOT NULL "
            "AND registry_adapter_model_byte_size > 0)",
            name="ck_adapter_lifecycle",
        ),
        Index("ix_adapter_department_status_created", "department_id", "status", "created_at"),
        Index("ix_adapter_claim", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_attempt_id: Mapped[UUID] = mapped_column(unique=True, nullable=False)
    execution_scope_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    queued_at: Mapped[datetime] = utc_timestamp()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    source_authoritative_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    source_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    source_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_imported_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    source_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    intake_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    config_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    tensor_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_intake_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_intake_manifest_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_adapter_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_adapter_config_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_adapter_model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_adapter_model_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    peft_version: Mapped[str] = mapped_column(String(32), nullable=False)
    safetensors_format: Mapped[str] = mapped_column(String(32), nullable=False)
    tensor_dtype: Mapped[str] = mapped_column(String(8), nullable=False)
    tensor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tensor_element_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tensor_payload_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    training_job_id: Mapped[UUID] = mapped_column(nullable=False)
    training_job_version: Mapped[int] = mapped_column(Integer, nullable=False)
    training_job_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    training_job_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    training_job_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    training_job_code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    training_job_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    training_job_manifest_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    training_job_execution_scope_id: Mapped[UUID] = mapped_column(nullable=False)
    training_job_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    training_job_config_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    training_job_dataset_info_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    training_job_dataset_info_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    training_job_train_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    training_job_train_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    training_job_validation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    training_job_validation_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    training_job_profile_id: Mapped[str] = mapped_column(String(80), nullable=False)
    training_job_artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    training_job_manifest_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    training_configuration_contract_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    training_dataset_info_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    training_execution_profile_contract_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    llamafactory_version: Mapped[str] = mapped_column(String(32), nullable=False)

    dataset_build_id: Mapped[UUID] = mapped_column(nullable=False)
    dataset_build_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    dataset_publication_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    dataset_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    dataset_artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_example_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_split_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_train_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_train_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_validation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_validation_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_provenance_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_provenance_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_train_example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_validation_example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_source_example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_source_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_source_reference_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_rights_attested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluation_contamination_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    base_model_license: Mapped[str] = mapped_column(String(40), nullable=False)
    artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    registry_manifest_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    declared_external_training_association: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verified_governance_lineage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    verified_artifact_compatibility: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    training_provenance_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    registry_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    registry_adapter_config_sha256: Mapped[str | None] = mapped_column(String(64))
    registry_adapter_config_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    registry_adapter_model_sha256: Mapped[str | None] = mapped_column(String(64))
    registry_adapter_model_byte_size: Mapped[int | None] = mapped_column(BigInteger)


class AdapterRegistryAttempt(Base):
    """Durable content-free ownership for every registry publication attempt."""

    __tablename__ = "adapter_registry_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_registry_attempt_adapter_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id", "department_id", "adapter_id", name="uq_adapter_registry_attempt_scope"
        ),
        UniqueConstraint("adapter_id", "attempt_number", name="uq_adapter_registry_attempt_number"),
        UniqueConstraint("publication_attempt_id", name="uq_adapter_registry_attempt_publication"),
        UniqueConstraint(
            "id",
            "department_id",
            "adapter_id",
            "publication_attempt_id",
            "attempt_number",
            name="uq_adapter_registry_attempt_exact",
        ),
        CheckConstraint(
            "status IN ('registered','running','staged','published','succeeded',"
            "'validation_failed','failed','reclaimed')",
            name="ck_adapter_registry_attempt_status",
        ),
        CheckConstraint(
            "attempt_number > 0 AND version > 0", name="ck_adapter_registry_attempt_versions"
        ),
        CheckConstraint(
            "ownership_manifest IS NULL OR json_typeof(ownership_manifest) = 'object'",
            name="ck_adapter_registry_attempt_manifest_object",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_REGISTRY_ERROR_CODES)
            + ")",
            name="ck_adapter_registry_attempt_error_code",
        ),
        CheckConstraint(
            "(status IN ('staged','published','succeeded') AND ownership_manifest IS NOT NULL) "
            "OR status NOT IN ('staged','published','succeeded')",
            name="ck_adapter_registry_attempt_manifest_lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND staged_at IS NOT NULL AND published_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('validation_failed','failed','reclaimed') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL) "
            "OR status NOT IN ('succeeded','validation_failed','failed','reclaimed')",
            name="ck_adapter_registry_attempt_lifecycle",
        ),
        CheckConstraint(
            "((status IN ('registered','running','staged','published','succeeded') "
            "AND cleanup_confirmed_at IS NULL) OR "
            "status IN ('validation_failed','failed','reclaimed')) "
            "AND "
            "((status = 'registered' AND worker_id IS NULL AND claimed_at IS NULL AND "
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
            "error_code IS NOT NULL AND "
            "(cleanup_confirmed_at IS NULL OR cleanup_confirmed_at >= finished_at)))",
            name="ck_adapter_registry_attempt_exact_lifecycle",
        ),
        Index(
            "ix_adapter_registry_attempt_department_status_created_id",
            "department_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "uq_adapter_registry_attempt_active",
            "adapter_id",
            unique=True,
            postgresql_where=text("status IN ('registered','running','staged','published')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    execution_scope_id: Mapped[UUID] = mapped_column(nullable=False)
    worker_id: Mapped[UUID | None] = mapped_column()
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="registered")
    ownership_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    registered_at: Mapped[datetime] = utc_timestamp()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterUpstreamDependency(Base):
    """Retention fence for the exact Phase 11 job and Phase 10 dataset."""

    __tablename__ = "adapter_upstream_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_dependency_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_adapter_dependency_training_job_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_build_id", "department_id"],
            ["sft_dataset_builds.id", "sft_dataset_builds.department_id"],
            name="fk_adapter_dependency_dataset_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["adapter_id", "department_id", "training_job_id", "dataset_build_id"],
            [
                "adapters.id",
                "adapters.department_id",
                "adapters.training_job_id",
                "adapters.dataset_build_id",
            ],
            name="fk_adapter_dependency_adapter_snapshot",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("adapter_id", name="uq_adapter_dependency_adapter"),
        UniqueConstraint("id", "department_id", "adapter_id", name="uq_adapter_dependency_scope"),
        CheckConstraint("status IN ('active','released')", name="ck_adapter_dependency_status"),
        CheckConstraint("version > 0", name="ck_adapter_dependency_version"),
        CheckConstraint(
            "(status = 'active' AND released_at IS NULL) OR "
            "(status = 'released' AND released_at IS NOT NULL)",
            name="ck_adapter_dependency_lifecycle",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    training_job_id: Mapped[UUID] = mapped_column(nullable=False)
    dataset_build_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = utc_timestamp()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TrainingJob(Base):
    """Metadata-only Phase 11 configuration-bundle generation state."""

    __tablename__ = "training_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_build_id", "department_id"],
            ["sft_dataset_builds.id", "sft_dataset_builds.department_id"],
            name="fk_training_job_dataset_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", name="uq_training_job_department"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_training_job_status",
        ),
        CheckConstraint(
            "review_status IN ('not_ready','pending','approved','rejected','archived','purged')",
            name="ck_training_job_review_status",
        ),
        CheckConstraint(
            "profile_id IN ('phase11-qwen3-0.6b-lora-v1','phase11-qwen3-0.6b-qlora-nf4-v1')",
            name="ck_training_job_profile",
        ),
        CheckConstraint(
            "base_model_id = 'Qwen/Qwen3-0.6B' AND "
            "base_model_revision = 'c1899de289a04d12100db370d81485cdf75e47ca' "
            "AND base_model_license = 'Apache-2.0' AND llamafactory_version = '0.9.5'",
            name="ck_training_job_model_contract",
        ),
        CheckConstraint(
            "artifact_contract_version = 'phase11-training-job-v1' AND "
            "manifest_contract_version = 'phase11-training-job-manifest-v1' AND "
            "configuration_contract_version = 'phase11-training-config-v1' AND "
            "dataset_info_contract_version = 'phase11-dataset-info-v1' AND "
            "execution_profile_contract_version = 'phase11-execution-profile-v1'",
            name="ck_training_job_artifact_contracts",
        ),
        CheckConstraint(
            "dataset_artifact_contract_version = 'phase10-sft-dataset-v1' AND "
            "dataset_example_contract_version = 'phase10-sft-example-v1' AND "
            "dataset_normalization_version = 'phase10-sft-normalization-v1' AND "
            "dataset_split_version = 'phase10-sft-group-split-v1'",
            name="ck_training_job_dataset_contracts",
        ),
        CheckConstraint(
            "dataset_status = 'succeeded' AND dataset_review_status = 'approved'",
            name="ck_training_job_dataset_snapshot_lifecycle",
        ),
        CheckConstraint(
            "dataset_publication_attempt_number > 0 AND dataset_train_example_count > 0 "
            "AND dataset_validation_example_count > 0 AND dataset_source_example_count >= 2 "
            "AND dataset_source_group_count >= 2 AND "
            "dataset_source_reference_count >= dataset_source_example_count",
            name="ck_training_job_dataset_snapshot_counts",
        ),
        CheckConstraint("maximum_record_content_bytes = 7680", name="ck_training_job_record_limit"),
        CheckConstraint("attempt_number > 0 AND version > 0", name="ck_training_job_versions"),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            "'dataset_unavailable','dataset_artifact_mismatch','dataset_contract_invalid',"
            "'dataset_record_invalid','dataset_authority_changed','department_unavailable',"
            "'requester_unauthorized','training_job_publication_failed','claim_lost',"
            "'cancelled','worker_shutdown','worker_timeout','database_unavailable')",
            name="ck_training_job_error_code",
        ),
        CheckConstraint(
            "(status = 'queued' AND review_status = 'not_ready' AND worker_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL AND publication_attempt_id IS NULL AND error_code IS NULL) "
            "OR status <> 'queued'",
            name="ck_training_job_queued_lifecycle",
        ),
        CheckConstraint(
            "(status = 'running' AND review_status = 'not_ready' AND worker_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND publication_attempt_id IS NOT NULL "
            "AND error_code IS NULL) OR status <> 'running'",
            name="ck_training_job_running_lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND review_status IN "
            "('pending','approved','rejected','archived','purged') "
            "AND finished_at IS NOT NULL AND train_example_count > 0 "
            "AND validation_example_count > 0 AND publication_attempt_id IS NOT NULL "
            "AND publication_manifest IS NOT NULL AND json_typeof(publication_manifest) = 'object' "
            "AND result_manifest_sha256 IS NOT NULL AND training_config_sha256 IS NOT NULL "
            "AND training_config_byte_size > 0 AND dataset_info_sha256 IS NOT NULL "
            "AND dataset_info_byte_size > 0 AND train_sha256 IS NOT NULL "
            "AND train_byte_size > 0 AND validation_sha256 IS NOT NULL "
            "AND validation_byte_size > 0 "
            "AND error_code IS NULL) OR status <> 'succeeded'",
            name="ck_training_job_succeeded_lifecycle",
        ),
        Index("ix_training_job_department_status_created", "department_id", "status", "created_at"),
        Index("ix_training_job_claim", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    dataset_build_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_ready")
    profile_id: Mapped[str] = mapped_column(String(80), nullable=False)
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    base_model_license: Mapped[str] = mapped_column(String(40), nullable=False)
    llamafactory_version: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    manifest_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    configuration_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_info_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    execution_profile_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_example_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_normalization_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_split_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_build_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    dataset_status: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    dataset_publication_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    dataset_train_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_train_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_validation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_validation_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_provenance_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_provenance_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_train_example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_validation_example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_source_example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_source_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_source_reference_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_rights_attested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluation_contamination_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    execution_scope_id: Mapped[UUID] = mapped_column(nullable=False)
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_attempt_id: Mapped[UUID | None] = mapped_column()
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    train_example_count: Mapped[int | None] = mapped_column(Integer)
    validation_example_count: Mapped[int | None] = mapped_column(Integer)
    maximum_record_content_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    result_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    training_config_sha256: Mapped[str | None] = mapped_column(String(64))
    training_config_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    dataset_info_sha256: Mapped[str | None] = mapped_column(String(64))
    dataset_info_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    train_sha256: Mapped[str | None] = mapped_column(String(64))
    train_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    validation_sha256: Mapped[str | None] = mapped_column(String(64))
    validation_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    publication_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    artifact_cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    requested_at: Mapped[datetime] = utc_timestamp()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TrainingJobAttempt(Base):
    """Durable content-free ownership for each Phase 11 publication attempt."""

    __tablename__ = "training_job_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_training_job_attempt_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "training_job_id", "attempt_number", name="uq_training_job_attempt_number"
        ),
        UniqueConstraint("publication_attempt_id", name="uq_training_job_attempt_publication"),
        UniqueConstraint(
            "training_job_id",
            "department_id",
            "publication_attempt_id",
            name="uq_training_job_attempt_scope_publication",
        ),
        UniqueConstraint(
            "training_job_id",
            "department_id",
            "publication_attempt_id",
            "attempt_number",
            name="uq_training_job_attempt_exact",
        ),
        CheckConstraint(
            "status IN ('registered','running','staged','published','succeeded',"
            "'failed','cancelled','reclaimed')",
            name="ck_training_job_attempt_status",
        ),
        CheckConstraint(
            "attempt_number > 0 AND version > 0", name="ck_training_job_attempt_versions"
        ),
        CheckConstraint(
            "(status = 'registered' AND claimed_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND claimed_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status = 'staged' AND claimed_at IS NOT NULL AND staged_at IS NOT NULL "
            "AND ownership_manifest IS NOT NULL AND json_typeof(ownership_manifest) = 'object' "
            "AND finished_at IS NULL) OR "
            "(status = 'published' AND claimed_at IS NOT NULL AND staged_at IS NOT NULL "
            "AND published_at IS NOT NULL AND ownership_manifest IS NOT NULL "
            "AND json_typeof(ownership_manifest) = 'object' "
            "AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND claimed_at IS NOT NULL AND staged_at IS NOT NULL "
            "AND published_at IS NOT NULL AND ownership_manifest IS NOT NULL "
            "AND json_typeof(ownership_manifest) = 'object' "
            "AND finished_at IS NOT NULL) OR "
            "(status IN ('failed','cancelled','reclaimed') AND finished_at IS NOT NULL)",
            name="ck_training_job_attempt_lifecycle",
        ),
        Index("ix_training_job_attempt_department_status", "department_id", "status", "created_at"),
        Index(
            "uq_training_job_attempt_active",
            "training_job_id",
            unique=True,
            postgresql_where=text("status IN ('registered','running','staged','published')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    training_job_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    ownership_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    registered_at: Mapped[datetime] = utc_timestamp()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TrainingJobArtifactOperation(Base):
    """Durable Phase 11 reconciliation or purge operation metadata."""

    __tablename__ = "training_job_artifact_operations"
    __table_args__ = (
        UniqueConstraint("id", "department_id", name="uq_training_job_operation_scope"),
        CheckConstraint(
            "operation_type IN ('reconcile','purge')", name="ck_training_job_operation_type"
        ),
        CheckConstraint(
            "status IN ('registered','completed','completed_with_blocks')",
            name="ck_training_job_operation_status",
        ),
        CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL) OR "
            "(status IN ('completed','completed_with_blocks') AND completed_at IS NOT NULL)",
            name="ck_training_job_operation_lifecycle",
        ),
        CheckConstraint("limit_value BETWEEN 1 AND 1000", name="ck_training_job_operation_limit"),
        CheckConstraint(
            "(operation_type = 'reconcile' AND retention_days IS NULL) OR "
            "(operation_type = 'purge' AND retention_days BETWEEN 30 AND 730)",
            name="ck_training_job_operation_retention",
        ),
        CheckConstraint(
            "purged_job_count >= 0 AND version > 0",
            name="ck_training_job_operation_progress",
        ),
        Index("ix_training_job_operation_department", "department_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT"), nullable=False
    )
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    retention_days: Mapped[int | None] = mapped_column(Integer)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    purged_job_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()


class TrainingJobPurgeReservation(Base):
    """Durable authority fence for a purge operation before external deletion."""

    __tablename__ = "training_job_purge_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            [
                "training_job_artifact_operations.id",
                "training_job_artifact_operations.department_id",
            ],
            name="fk_training_job_purge_reservation_operation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_training_job_purge_reservation_job_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('registered','deletion_authorized','tombstone_bound','terminalized')",
            name="ck_training_job_purge_reservation_status",
        ),
        CheckConstraint(
            "expected_review_status IN ('rejected','archived')",
            name="ck_training_job_purge_reservation_review",
        ),
        CheckConstraint(
            "retention_days BETWEEN 30 AND 730 AND expected_job_version > 0 AND version > 0",
            name="ck_training_job_purge_reservation_values",
        ),
        CheckConstraint(
            "authoritative_publication_attempt_id IS NOT NULL AND "
            "authoritative_manifest IS NOT NULL AND tombstone_operation_id IS NOT NULL",
            name="ck_training_job_purge_reservation_authority",
        ),
        CheckConstraint(
            "tombstone_operation_id = operation_id",
            name="ck_training_job_purge_reservation_tombstone_operation",
        ),
        CheckConstraint(
            "(status = 'registered' AND deletion_authorized_at IS NULL "
            "AND tombstone_bound_at IS NULL AND tombstone_identity IS NULL "
            "AND terminalized_at IS NULL) "
            "OR (status = 'deletion_authorized' AND deletion_authorized_at IS NOT NULL "
            "AND tombstone_bound_at IS NULL AND tombstone_identity IS NULL "
            "AND terminalized_at IS NULL) OR (status = 'tombstone_bound' "
            "AND deletion_authorized_at IS NOT NULL AND tombstone_bound_at IS NOT NULL "
            "AND tombstone_identity IS NOT NULL AND json_typeof(tombstone_identity) = 'object' "
            "AND terminalized_at IS NULL) OR (status = 'terminalized' "
            "AND terminalized_at IS NOT NULL)",
            name="ck_training_job_purge_reservation_lifecycle",
        ),
        UniqueConstraint(
            "operation_id", "training_job_id", name="uq_training_job_purge_reservation_operation"
        ),
        Index(
            "uq_training_job_purge_reservation_active",
            "training_job_id",
            unique=True,
            postgresql_where=text(
                "status IN ('registered','deletion_authorized','tombstone_bound')"
            ),
        ),
        Index(
            "ix_training_job_purge_reservation_operation",
            "operation_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    training_job_id: Mapped[UUID] = mapped_column(nullable=False)
    expected_job_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    retention_anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    authoritative_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    authoritative_manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    tombstone_operation_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    registered_at: Mapped[datetime] = utc_timestamp()
    deletion_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstone_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstone_identity: Mapped[dict[str, object] | None] = mapped_column(JSON)
    terminalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()


class TrainingJobArtifactOperationItem(Base):
    """Exact attempt/resource-surface cleanup item; never carries content or paths."""

    __tablename__ = "training_job_artifact_operation_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            [
                "training_job_artifact_operations.id",
                "training_job_artifact_operations.department_id",
            ],
            name="fk_training_job_operation_item_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["training_job_id", "department_id"],
            ["training_jobs.id", "training_jobs.department_id"],
            name="fk_training_job_operation_item_job_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["training_job_id", "department_id", "publication_attempt_id"],
            [
                "training_job_attempts.training_job_id",
                "training_job_attempts.department_id",
                "training_job_attempts.publication_attempt_id",
            ],
            name="fk_training_job_operation_item_attempt_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "resource_surface IN ('stage','final')", name="ck_training_job_item_surface"
        ),
        CheckConstraint(
            "status IN ('registered','completed','blocked')", name="ck_training_job_item_status"
        ),
        CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN "
            "('staging_path_unsafe','artifact_ownership_mismatch','artifact_manifest_invalid',"
            "'artifact_permissions_invalid')",
            name="ck_training_job_item_reason",
        ),
        CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'blocked' AND completed_at IS NULL AND blocked_at IS NOT NULL "
            "AND blocked_reason_code IS NOT NULL)",
            name="ck_training_job_item_lifecycle",
        ),
        UniqueConstraint(
            "operation_id",
            "training_job_id",
            "publication_attempt_id",
            "resource_surface",
            name="uq_training_job_operation_item",
        ),
        Index("ix_training_job_operation_item_status", "operation_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    training_job_id: Mapped[UUID] = mapped_column(nullable=False)
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    resource_surface: Mapped[str] = mapped_column(String(16), nullable=False)
    ownership_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    blocked_reason_code: Mapped[str | None] = mapped_column(String(48))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_timestamp()


class AdapterPurgeOperation(Base):
    """Independent durable authority for one adapter-byte purge."""

    __tablename__ = "adapter_purge_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_purge_operation_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_purge_operation_source_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id", "department_id"],
            ["memberships.user_id", "memberships.department_id"],
            name="fk_adapter_purge_operation_requester_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_purge_operation_requester_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        UniqueConstraint("id", "department_id", name="uq_adapter_purge_operation_scope"),
        CheckConstraint(
            "status IN ('registered','deleting','completed','completed_with_blocks','blocked')",
            name="ck_adapter_purge_operation_status",
        ),
        CheckConstraint(
            "limit_value BETWEEN 1 AND 1000 AND item_limit_value BETWEEN 2 AND 2000",
            name="ck_adapter_purge_operation_limits",
        ),
        CheckConstraint(
            "expected_adapter_version > 0 AND expected_source_version > 0 AND "
            "expected_source_attempt_version > 0 AND expected_registry_attempt_version > 0 "
            "AND source_attempt_number > 0 AND registry_attempt_number > 0 AND version > 0",
            name="ck_adapter_purge_operation_versions",
        ),
        CheckConstraint(
            "json_typeof(authority_snapshot) = 'object'",
            name="ck_adapter_purge_operation_snapshot",
        ),
        CheckConstraint(
            "eligible_item_count >= 0 AND completed_item_count >= 0 AND blocked_item_count >= 0 "
            "AND completed_item_count + blocked_item_count <= eligible_item_count",
            name="ck_adapter_purge_operation_counts",
        ),
        CheckConstraint(
            "(status IN ('registered','deleting') AND completed_at IS NULL) OR "
            "(status IN ('completed','completed_with_blocks','blocked') "
            "AND completed_at IS NOT NULL)",
            name="ck_adapter_purge_operation_lifecycle",
        ),
        Index(
            "uq_adapter_purge_operation_active",
            "department_id",
            "adapter_id",
            unique=True,
            postgresql_where=text("status IN ('registered','deleting')"),
        ),
        Index("ix_adapter_purge_operation_department_created", "department_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    item_limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    expected_adapter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_source_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_registry_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_authoritative_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    source_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    source_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    registry_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    registry_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    authority_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    eligible_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterPurgeReservation(Base):
    """Exact source or registry authority fence for one purge operation."""

    __tablename__ = "adapter_purge_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            ["adapter_purge_operations.id", "adapter_purge_operations.department_id"],
            name="fk_adapter_purge_reservation_operation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_purge_reservation_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_purge_reservation_source_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        UniqueConstraint("id", "department_id", name="uq_adapter_purge_reservation_scope"),
        UniqueConstraint(
            "operation_id", "surface_type", name="uq_adapter_purge_reservation_surface"
        ),
        CheckConstraint(
            "surface_type IN ('source_final','registry_final')",
            name="ck_adapter_purge_reservation_surface",
        ),
        CheckConstraint(
            "((surface_type = 'source_final' AND import_attempt_id IS NOT NULL AND "
            "registry_attempt_id IS NULL AND expected_resource_status = 'consumed' "
            "AND expected_attempt_status = 'committed') OR "
            "(surface_type = 'registry_final' AND import_attempt_id IS NULL AND "
            "registry_attempt_id IS NOT NULL AND expected_resource_status = 'validated' "
            "AND expected_attempt_status = 'succeeded'))",
            name="ck_adapter_purge_reservation_authority",
        ),
        CheckConstraint(
            "publication_attempt_id IS NOT NULL AND attempt_number > 0 AND "
            "expected_resource_version > 0 AND expected_attempt_version > 0 AND version > 0",
            name="ck_adapter_purge_reservation_versions",
        ),
        CheckConstraint(
            "json_typeof(authority_manifest) = 'object' AND "
            "json_typeof(authority_snapshot) = 'object'",
            name="ck_adapter_purge_reservation_snapshot",
        ),
        CheckConstraint(
            "status IN ('registered','deletion_authorized','tombstone_bound','deleting',"
            "'completed','blocked')",
            name="ck_adapter_purge_reservation_status",
        ),
        CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_PURGE_BLOCKED_REASONS)
            + ")",
            name="ck_adapter_purge_reservation_reason",
        ),
        CheckConstraint("next_entry_index >= 0", name="ck_adapter_purge_reservation_progress"),
        Index(
            "uq_adapter_purge_reservation_active_source",
            "department_id",
            "source_bundle_id",
            unique=True,
            postgresql_where=text(
                "surface_type = 'source_final' AND status IN "
                "('registered','deletion_authorized','tombstone_bound','deleting')"
            ),
        ),
        Index(
            "uq_adapter_purge_reservation_active_registry",
            "department_id",
            "adapter_id",
            unique=True,
            postgresql_where=text(
                "surface_type = 'registry_final' AND status IN "
                "('registered','deletion_authorized','tombstone_bound','deleting')"
            ),
        ),
        Index(
            "ix_adapter_purge_reservation_operation_status", "operation_id", "status", "created_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    surface_type: Mapped[str] = mapped_column(String(16), nullable=False)
    import_attempt_id: Mapped[UUID | None] = mapped_column()
    registry_attempt_id: Mapped[UUID | None] = mapped_column()
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_resource_status: Mapped[str] = mapped_column(String(24), nullable=False)
    expected_attempt_status: Mapped[str] = mapped_column(String(24), nullable=False)
    authority_manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    authority_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    expected_tombstone_namespace: Mapped[dict[str, object] | None] = mapped_column(JSON)
    observed_identity: Mapped[dict[str, object] | None] = mapped_column(JSON)
    tombstone_identity: Mapped[dict[str, object] | None] = mapped_column(JSON)
    deletion_plan: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    next_entry_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_flight_entry: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    blocked_reason_code: Mapped[str | None] = mapped_column(String(64))
    registered_at: Mapped[datetime] = utc_timestamp()
    deletion_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstone_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    directory_unlink_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterPurgeItem(Base):
    """One exact surface item with durable descriptor/tombstone progress."""

    __tablename__ = "adapter_purge_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            ["adapter_purge_operations.id", "adapter_purge_operations.department_id"],
            name="fk_adapter_purge_item_operation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reservation_id", "department_id"],
            ["adapter_purge_reservations.id", "adapter_purge_reservations.department_id"],
            name="fk_adapter_purge_item_reservation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_purge_item_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_purge_item_source_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        UniqueConstraint(
            "operation_id", "reservation_id", name="uq_adapter_purge_item_reservation"
        ),
        CheckConstraint(
            "surface_type IN ('source_final','registry_final')",
            name="ck_adapter_purge_item_surface",
        ),
        CheckConstraint(
            "expected_item_version > 0 AND attempt_number > 0 AND version > 0",
            name="ck_adapter_purge_item_versions",
        ),
        CheckConstraint(
            "json_typeof(ownership_manifest) = 'object'", name="ck_adapter_purge_item_manifest"
        ),
        CheckConstraint(
            "status IN ('registered','verified','tombstone_bound','deleting',"
            "'completed','blocked')",
            name="ck_adapter_purge_item_status",
        ),
        CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_PURGE_BLOCKED_REASONS)
            + ")",
            name="ck_adapter_purge_item_reason",
        ),
        CheckConstraint("next_entry_index >= 0", name="ck_adapter_purge_item_progress"),
        Index("ix_adapter_purge_item_operation_status", "operation_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    reservation_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    surface_type: Mapped[str] = mapped_column(String(16), nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    source_bundle_id: Mapped[UUID] = mapped_column(nullable=False)
    import_attempt_id: Mapped[UUID | None] = mapped_column()
    registry_attempt_id: Mapped[UUID | None] = mapped_column()
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_item_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ownership_manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    observed_identity: Mapped[dict[str, object] | None] = mapped_column(JSON)
    tombstone_identity: Mapped[dict[str, object] | None] = mapped_column(JSON)
    deletion_plan: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    expected_tombstone_namespace: Mapped[dict[str, object] | None] = mapped_column(JSON)
    next_entry_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_flight_entry: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    blocked_reason_code: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    move_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstone_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    directory_unlink_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterArtifactOperation(Base):
    """Durable, bounded Phase 12.1E-A reconciliation authority."""

    __tablename__ = "adapter_artifact_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["requested_by_user_id", "department_id"],
            ["memberships.user_id", "memberships.department_id"],
            name="fk_adapter_artifact_operation_requester_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_artifact_operation_requester_identity",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", name="uq_adapter_artifact_operation_scope"),
        CheckConstraint("operation_type = 'reconcile'", name="ck_adapter_artifact_operation_type"),
        CheckConstraint(
            "status IN ('registered','completed','completed_with_blocks')",
            name="ck_adapter_artifact_operation_status",
        ),
        CheckConstraint(
            "limit_value BETWEEN 1 AND 1000",
            name="ck_adapter_artifact_operation_limit",
        ),
        CheckConstraint(
            "minimum_age_seconds BETWEEN 300 AND 86400",
            name="ck_adapter_artifact_operation_minimum_age",
        ),
        CheckConstraint(
            "eligible_count >= 0 AND completed_count >= 0 AND blocked_count >= 0 AND "
            "completed_count + blocked_count <= eligible_count",
            name="ck_adapter_artifact_operation_counts",
        ),
        CheckConstraint(
            "(status = 'registered' AND completed_at IS NULL) OR "
            "(status IN ('completed','completed_with_blocks') AND completed_at IS NOT NULL)",
            name="ck_adapter_artifact_operation_lifecycle",
        ),
        CheckConstraint(
            "(status = 'completed' AND blocked_count = 0) OR "
            "(status = 'completed_with_blocks' AND blocked_count > 0) OR status = 'registered'",
            name="ck_adapter_artifact_operation_blocked_lifecycle",
        ),
        CheckConstraint("version > 0", name="ck_adapter_artifact_operation_version"),
        Index(
            "uq_adapter_artifact_operation_active",
            "department_id",
            unique=True,
            postgresql_where=text("operation_type = 'reconcile' AND status = 'registered'"),
        ),
        Index(
            "ix_adapter_artifact_operation_department_created",
            "department_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "departments.id", name="fk_adapter_artifact_operation_department", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False, default="reconcile")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterArtifactReconciliationCursor(Base):
    """Content-free durable scan progress for one family/status stream."""

    __tablename__ = "adapter_artifact_reconciliation_cursors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_artifact_reconciliation_cursor_department",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "family IN ('source','registry')",
            name="ck_adapter_artifact_reconciliation_cursor_family",
        ),
        CheckConstraint(
            "(family = 'source' AND status IN "
            "('failed','abandoned','registered','validated','staged','published')) OR "
            "(family = 'registry' AND status IN "
            "('validation_failed','failed','reclaimed'))",
            name="ck_adapter_artifact_reconciliation_cursor_status",
        ),
        CheckConstraint(
            "((cursor_created_at IS NULL AND cursor_attempt_id IS NULL) OR "
            "(cursor_created_at IS NOT NULL AND cursor_attempt_id IS NOT NULL))",
            name="ck_adapter_artifact_reconciliation_cursor_pair",
        ),
        CheckConstraint("version > 0", name="ck_adapter_artifact_reconciliation_cursor_version"),
        UniqueConstraint(
            "department_id",
            "family",
            "status",
            name="uq_adapter_artifact_reconciliation_cursor_scope",
        ),
    )

    department_id: Mapped[UUID] = mapped_column(primary_key=True)
    family: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), primary_key=True)
    cursor_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_attempt_id: Mapped[UUID | None] = mapped_column()
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterArtifactOperationItem(Base):
    """One exact surface generation owned by a reconciliation operation.

    Blocked rows are immutable history; a reviewed retry is represented by a
    fresh item in a later operation and remains subject to the active-surface
    uniqueness indexes below.
    """

    __tablename__ = "adapter_artifact_operation_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            ["adapter_artifact_operations.id", "adapter_artifact_operations.department_id"],
            name="fk_adapter_artifact_item_operation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["source_bundle_id", "department_id"],
            ["adapter_import_sources.id", "adapter_import_sources.department_id"],
            name="fk_adapter_artifact_item_source_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_artifact_item_adapter_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "surface_type IN ('source_stage','source_final','registry_stage','registry_final')",
            name="ck_adapter_artifact_item_surface",
        ),
        CheckConstraint(
            "((surface_type IN ('source_stage','source_final') AND source_bundle_id IS NOT NULL "
            "AND import_attempt_id IS NOT NULL AND adapter_id IS NULL "
            "AND registry_attempt_id IS NULL) OR "
            "(surface_type IN ('registry_stage','registry_final') AND adapter_id IS NOT NULL "
            "AND registry_attempt_id IS NOT NULL AND source_bundle_id IS NULL "
            "AND import_attempt_id IS NULL))",
            name="ck_adapter_artifact_item_surface_authority",
        ),
        CheckConstraint(
            "publication_attempt_id IS NOT NULL AND attempt_number > 0 AND "
            "expected_resource_version > 0 AND expected_attempt_version > 0",
            name="ck_adapter_artifact_item_attempt_authority",
        ),
        CheckConstraint(
            "status IN ('registered','verified','tombstone_bound','deleting',"
            "'completed','blocked')",
            name="ck_adapter_artifact_item_status",
        ),
        CheckConstraint(
            "ownership_manifest IS NULL OR json_typeof(ownership_manifest) = 'object'",
            name="ck_adapter_artifact_item_manifest_object",
        ),
        CheckConstraint(
            "observed_identity IS NULL OR json_typeof(observed_identity) IN ('object','array')",
            name="ck_adapter_artifact_item_observed_json",
        ),
        CheckConstraint(
            "tombstone_identity IS NULL OR json_typeof(tombstone_identity) IN ('object','array')",
            name="ck_adapter_artifact_item_tombstone_json",
        ),
        CheckConstraint(
            "deletion_plan IS NULL OR json_typeof(deletion_plan) IN ('object','array')",
            name="ck_adapter_artifact_item_plan_json",
        ),
        CheckConstraint(
            "in_flight_entry IS NULL OR json_typeof(in_flight_entry) IN ('object','array')",
            name="ck_adapter_artifact_item_in_flight_json",
        ),
        CheckConstraint(
            "expected_tombstone_namespace IS NULL OR "
            "json_typeof(expected_tombstone_namespace) = 'object'",
            name="ck_adapter_artifact_item_move_namespace_json",
        ),
        CheckConstraint(
            "next_entry_index >= 0",
            name="ck_adapter_artifact_item_progress",
        ),
        CheckConstraint(
            "((status NOT IN ('deleting','completed')) OR "
            "(status = 'deleting' AND json_typeof(observed_identity) = 'object' "
            "AND json_typeof(tombstone_identity) = 'object' "
            "AND json_typeof(expected_tombstone_namespace) = 'object' "
            "AND json_typeof(deletion_plan) = 'array' "
            "AND next_entry_index BETWEEN 0 AND json_array_length(deletion_plan) "
            "AND (directory_unlink_started_at IS NULL OR "
            "next_entry_index = json_array_length(deletion_plan)) "
            "AND (in_flight_entry IS NULL OR "
            "(json_typeof(in_flight_entry) = 'object' "
            "AND next_entry_index < json_array_length(deletion_plan) "
            "AND in_flight_entry->>'name' = "
            "(deletion_plan->next_entry_index)->>'name')))) OR "
            "(status = 'completed' AND (deletion_plan IS NULL AND next_entry_index = 0 OR "
            "(json_typeof(deletion_plan) = 'array' AND "
            "next_entry_index = json_array_length(deletion_plan)))))",
            name="ck_adapter_artifact_item_progress_exact",
        ),
        CheckConstraint(
            "blocked_reason_code IS NULL OR blocked_reason_code IN "
            "('staging_path_unsafe','artifact_ownership_mismatch','artifact_manifest_invalid',"
            "'artifact_permissions_invalid','artifact_authority_changed','artifact_tombstone_conflict')",
            name="ck_adapter_artifact_item_reason",
        ),
        CheckConstraint(
            "((status = 'registered' AND observed_identity IS NULL AND tombstone_identity IS NULL "
            "AND deletion_plan IS NULL AND next_entry_index = 0 AND in_flight_entry IS NULL "
            "AND verified_at IS NULL AND move_authorized_at IS NULL "
            "AND expected_tombstone_namespace IS NULL AND tombstone_bound_at IS NULL "
            "AND deletion_started_at IS NULL AND directory_unlink_started_at IS NULL "
            "AND completed_at IS NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL) OR "
            "(status = 'verified' AND json_typeof(observed_identity) = 'object' "
            "AND json_typeof(deletion_plan) = 'array' "
            "AND tombstone_identity IS NULL AND verified_at IS NOT NULL "
            "AND next_entry_index = 0 AND in_flight_entry IS NULL "
            "AND tombstone_bound_at IS NULL AND deletion_started_at IS NULL "
            "AND directory_unlink_started_at IS NULL "
            "AND ((move_authorized_at IS NULL AND expected_tombstone_namespace IS NULL) OR "
            "(move_authorized_at IS NOT NULL AND expected_tombstone_namespace IS NOT NULL)) "
            "AND completed_at IS NULL "
            "AND blocked_at IS NULL AND blocked_reason_code IS NULL) OR "
            "(status = 'tombstone_bound' AND json_typeof(observed_identity) = 'object' "
            "AND json_typeof(deletion_plan) = 'array' "
            "AND json_typeof(tombstone_identity) = 'object' "
            "AND json_typeof(expected_tombstone_namespace) = 'object' "
            "AND tombstone_bound_at IS NOT NULL "
            "AND next_entry_index = 0 AND in_flight_entry IS NULL "
            "AND deletion_started_at IS NULL AND directory_unlink_started_at IS NULL "
            "AND move_authorized_at IS NOT NULL AND expected_tombstone_namespace IS NOT NULL "
            "AND completed_at IS NULL AND blocked_at IS NULL AND blocked_reason_code IS NULL) OR "
            "(status = 'deleting' AND json_typeof(observed_identity) = 'object' "
            "AND json_typeof(deletion_plan) = 'array' "
            "AND json_typeof(tombstone_identity) = 'object' "
            "AND json_typeof(expected_tombstone_namespace) = 'object' "
            "AND move_authorized_at IS NOT NULL "
            "AND deletion_started_at IS NOT NULL "
            "AND deletion_plan IS NOT NULL "
            "AND completed_at IS NULL AND blocked_at IS NULL AND blocked_reason_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND blocked_at IS NULL "
            "AND blocked_reason_code IS NULL AND in_flight_entry IS NULL) OR "
            "(status = 'blocked' AND blocked_at IS NOT NULL AND completed_at IS NULL "
            "AND blocked_reason_code IS NOT NULL))",
            name="ck_adapter_artifact_item_lifecycle",
        ),
        CheckConstraint("version > 0", name="ck_adapter_artifact_item_version"),
        UniqueConstraint(
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
        Index(
            "uq_adapter_artifact_item_active_source_stage",
            "department_id",
            "surface_type",
            "source_bundle_id",
            "import_attempt_id",
            unique=True,
            postgresql_where=text(
                "surface_type = 'source_stage' AND status IN "
                "('registered','verified','tombstone_bound','deleting')"
            ),
        ),
        Index(
            "uq_adapter_artifact_item_active_registry_stage",
            "department_id",
            "surface_type",
            "adapter_id",
            "publication_attempt_id",
            unique=True,
            postgresql_where=text(
                "surface_type = 'registry_stage' AND status IN "
                "('registered','verified','tombstone_bound','deleting')"
            ),
        ),
        Index(
            "uq_adapter_artifact_item_active_source_final",
            "department_id",
            "surface_type",
            "source_bundle_id",
            unique=True,
            postgresql_where=text(
                "surface_type = 'source_final' AND status IN "
                "('registered','verified','tombstone_bound','deleting')"
            ),
        ),
        Index(
            "uq_adapter_artifact_item_active_registry_final",
            "department_id",
            "surface_type",
            "adapter_id",
            unique=True,
            postgresql_where=text(
                "surface_type = 'registry_final' AND status IN "
                "('registered','verified','tombstone_bound','deleting')"
            ),
        ),
        Index(
            "ix_adapter_artifact_item_operation_status",
            "operation_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    surface_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_bundle_id: Mapped[UUID | None] = mapped_column()
    adapter_id: Mapped[UUID | None] = mapped_column()
    import_attempt_id: Mapped[UUID | None] = mapped_column()
    registry_attempt_id: Mapped[UUID | None] = mapped_column()
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ownership_manifest: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
    observed_identity: Mapped[dict[str, object] | list[object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    tombstone_identity: Mapped[dict[str, object] | list[object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    deletion_plan: Mapped[dict[str, object] | list[object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    expected_tombstone_namespace: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    next_entry_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_flight_entry: Mapped[dict[str, object] | list[object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    directory_unlink_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="registered")
    blocked_reason_code: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    move_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstone_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterReview(Base):
    """Immutable, department-scoped human governance decision for one evaluation."""

    __tablename__ = "adapter_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_review_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evaluation_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_review_evaluation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
            name="fk_adapter_review_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dependency_id", "department_id", "adapter_id"],
            [
                "adapter_upstream_dependencies.id",
                "adapter_upstream_dependencies.department_id",
                "adapter_upstream_dependencies.adapter_id",
            ],
            name="fk_adapter_review_dependency_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["suite_id", "department_id"],
            ["evaluation_suites.id", "evaluation_suites.department_id"],
            name="fk_adapter_review_suite_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_review_requester",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_review_reviewer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", name="uq_adapter_review_scope"),
        UniqueConstraint(
            "evaluation_id", "department_id", name="uq_adapter_review_evaluation_once"
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','archived')",
            name="ck_adapter_review_status",
        ),
        CheckConstraint(
            "adapter_version > 0 AND evaluation_version > 0 AND suite_version > 0 AND version > 0",
            name="ck_adapter_review_versions",
        ),
        CheckConstraint(
            "registry_attempt_version > 0 AND registry_attempt_number > 0 AND "
            "registry_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "registry_adapter_config_sha256 ~ '^[0-9a-f]{64}$' AND "
            "registry_adapter_config_byte_size > 0 AND "
            "registry_adapter_model_sha256 ~ '^[0-9a-f]{64}$' AND "
            "registry_adapter_model_byte_size > 0 AND "
            "suite_artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "suite_canonical_cases_sha256 ~ '^[0-9a-f]{64}$' AND "
            "suite_canonical_cases_byte_size > 0 AND "
            "result_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "result_summary_sha256 ~ '^[0-9a-f]{64}$' AND "
            "case_results_sha256 ~ '^[0-9a-f]{64}$' AND "
            "case_results_byte_size > 0 AND "
            "code_revision ~ '^[0-9a-f]{40}$'",
            name="ck_adapter_review_authority",
        ),
        CheckConstraint(
            "reviewed_by_user_id IS NULL OR decided_at IS NOT NULL",
            name="ck_adapter_review_decision_actor",
        ),
        CheckConstraint(
            "status = 'pending' OR decided_at IS NOT NULL",
            name="ck_adapter_review_decision_lifecycle",
        ),
        Index(
            "uq_adapter_review_pending_adapter",
            "department_id",
            "adapter_id",
            "adapter_version",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_adapter_review_approved_adapter",
            "department_id",
            "adapter_id",
            "adapter_version",
            unique=True,
            postgresql_where=text("status = 'approved' AND archived_at IS NULL"),
        ),
        Index("ix_adapter_review_department_created", "department_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_id: Mapped[UUID] = mapped_column(nullable=False)
    evaluation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_evidence_id: Mapped[UUID] = mapped_column(nullable=False)
    candidate_evidence_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    suite_version: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    registry_attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    registry_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_execution_scope_id: Mapped[UUID] = mapped_column(nullable=False)
    registry_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_adapter_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_adapter_config_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    registry_adapter_model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_adapter_model_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dependency_id: Mapped[UUID] = mapped_column(nullable=False)
    dependency_version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    metric_contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    gate_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    seed_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    code_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    suite_artifact_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    suite_canonical_cases_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    suite_canonical_cases_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_summary_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    case_results_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    case_results_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    requested_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column()
    started_at: Mapped[datetime] = utc_timestamp()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DepartmentAdapterDeployment(Base):
    """One explicit deployment pointer per department; absent means implicit base."""

    __tablename__ = "department_adapter_deployments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_deployment_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_deployment_review_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evaluation_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_deployment_evaluation_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("target_kind IN ('base','adapter')", name="ck_adapter_deployment_target"),
        CheckConstraint(
            "deployment_version > 0 AND version > 0", name="ck_adapter_deployment_versions"
        ),
        CheckConstraint(
            "(target_kind = 'base' AND adapter_id IS NULL AND adapter_version IS NULL "
            "AND review_id IS NULL AND review_version IS NULL AND evaluation_id IS NULL "
            "AND evaluation_version IS NULL AND suite_id IS NULL) OR "
            "(target_kind = 'adapter' AND adapter_id IS NOT NULL AND adapter_version > 0 "
            "AND review_id IS NOT NULL AND review_version > 0 AND evaluation_id IS NOT NULL "
            "AND evaluation_version > 0 AND suite_id IS NOT NULL)",
            name="ck_adapter_deployment_target_shape",
        ),
        UniqueConstraint("department_id", name="uq_adapter_deployment_department"),
        UniqueConstraint("id", "department_id", name="uq_adapter_deployment_id_department"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    adapter_id: Mapped[UUID | None] = mapped_column()
    adapter_version: Mapped[int | None] = mapped_column(Integer)
    review_id: Mapped[UUID | None] = mapped_column()
    review_version: Mapped[int | None] = mapped_column(Integer)
    evaluation_id: Mapped[UUID | None] = mapped_column()
    evaluation_version: Mapped[int | None] = mapped_column(Integer)
    suite_id: Mapped[UUID | None] = mapped_column()
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterDeploymentOperation(Base):
    """Durable metadata-only queue authority for one deployment mutation."""

    __tablename__ = "adapter_deployment_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_deployment_operation_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_identities.id"],
            name="fk_adapter_deployment_operation_requester",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_operation_target_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["current_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_operation_current_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_deployment_operation_target_review_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_evaluation_id", "department_id", "target_adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_deployment_operation_target_evaluation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "registry_attempt_id",
                "department_id",
                "target_adapter_id",
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
            name="fk_adapter_deployment_operation_registry_attempt_exact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dependency_id", "department_id", "target_adapter_id"],
            [
                "adapter_upstream_dependencies.id",
                "adapter_upstream_dependencies.department_id",
                "adapter_upstream_dependencies.adapter_id",
            ],
            name="fk_adapter_deployment_operation_dependency_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_retention_id", "department_id", "target_adapter_id"],
            [
                "adapter_rollback_retentions.id",
                "adapter_rollback_retentions.department_id",
                "adapter_rollback_retentions.adapter_id",
            ],
            name="fk_adapter_deployment_operation_target_retention_exact",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "operation_type IN ('promote','rollback_adapter','rollback_base')",
            name="ck_adapter_deployment_operation_type",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_adapter_deployment_operation_status",
        ),
        CheckConstraint(
            "expected_deployment_version >= 0 AND attempt_number > 0 AND version > 0",
            name="ck_adapter_deployment_operation_versions",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ("
            + ",".join("'" + code + "'" for code in ADAPTER_GOVERNANCE_ERROR_CODES)
            + ")",
            name="ck_adapter_deployment_operation_error",
        ),
        CheckConstraint(
            "((operation_type = 'rollback_base' AND target_adapter_id IS NULL AND "
            "target_review_id IS NULL AND target_evaluation_id IS NULL AND "
            "target_retention_id IS NULL AND registry_attempt_id IS NULL AND "
            "dependency_id IS NULL AND suite_id IS NULL) OR "
            "(operation_type IN ('promote','rollback_adapter') AND "
            "target_adapter_id IS NOT NULL AND target_review_id IS NOT NULL AND "
            "target_evaluation_id IS NOT NULL AND registry_attempt_id IS NOT NULL AND "
            "dependency_id IS NOT NULL AND suite_id IS NOT NULL AND "
            "(operation_type = 'promote' OR (target_retention_id IS NOT NULL AND "
            "target_retention_version > 0))))",
            name="ck_adapter_deployment_operation_target_shape",
        ),
        CheckConstraint(
            "(status IN ('queued','running') AND finished_at IS NULL) OR "
            "(status IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL)",
            name="ck_adapter_deployment_operation_lifecycle",
        ),
        UniqueConstraint("id", "department_id", name="uq_adapter_deployment_operation_scope"),
        Index(
            "uq_adapter_deployment_operation_active",
            "department_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
        Index(
            "ix_adapter_deployment_operation_department_created",
            "department_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    operation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    expected_deployment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_adapter_id: Mapped[UUID | None] = mapped_column()
    target_adapter_version: Mapped[int | None] = mapped_column(Integer)
    target_review_id: Mapped[UUID | None] = mapped_column()
    target_review_version: Mapped[int | None] = mapped_column(Integer)
    target_evaluation_id: Mapped[UUID | None] = mapped_column()
    target_evaluation_version: Mapped[int | None] = mapped_column(Integer)
    target_retention_id: Mapped[UUID | None] = mapped_column()
    target_retention_version: Mapped[int | None] = mapped_column(Integer)
    current_target_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="base")
    current_adapter_id: Mapped[UUID | None] = mapped_column()
    current_adapter_version: Mapped[int | None] = mapped_column(Integer)
    current_deployment_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_attempt_id: Mapped[UUID | None] = mapped_column()
    registry_attempt_version: Mapped[int | None] = mapped_column(Integer)
    registry_publication_attempt_id: Mapped[UUID | None] = mapped_column()
    registry_attempt_number: Mapped[int | None] = mapped_column(Integer)
    registry_execution_scope_id: Mapped[UUID | None] = mapped_column()
    registry_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    registry_adapter_config_sha256: Mapped[str | None] = mapped_column(String(64))
    registry_adapter_config_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    registry_adapter_model_sha256: Mapped[str | None] = mapped_column(String(64))
    registry_adapter_model_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    dependency_id: Mapped[UUID | None] = mapped_column()
    dependency_version: Mapped[int | None] = mapped_column(Integer)
    suite_id: Mapped[UUID | None] = mapped_column()
    suite_version: Mapped[int | None] = mapped_column(Integer)
    suite_artifact_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    suite_canonical_cases_sha256: Mapped[str | None] = mapped_column(String(64))
    suite_canonical_cases_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    result_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    result_summary_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_sha256: Mapped[str | None] = mapped_column(String(64))
    case_results_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    runner_contract_version: Mapped[str | None] = mapped_column(String(100))
    artifact_contract_version: Mapped[str | None] = mapped_column(String(100))
    metric_contract_version: Mapped[str | None] = mapped_column(String(100))
    gate_policy_version: Mapped[str | None] = mapped_column(String(100))
    seed_policy_version: Mapped[str | None] = mapped_column(String(100))
    code_revision: Mapped[str | None] = mapped_column(String(40))
    worker_id: Mapped[UUID | None] = mapped_column()
    claim_token: Mapped[UUID | None] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    queued_at: Mapped[datetime] = utc_timestamp()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdapterDeploymentEvent(Base):
    """Append-only, content-free deployment-governance history."""

    __tablename__ = "adapter_deployment_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_deployment_event_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_user_id"],
            ["user_identities.id"],
            name="fk_adapter_deployment_event_actor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operation_id", "department_id"],
            ["adapter_deployment_operations.id", "adapter_deployment_operations.department_id"],
            name="fk_adapter_deployment_event_operation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["from_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_event_from_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["to_adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_deployment_event_to_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approved_review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_deployment_event_review_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evaluation_id", "department_id", "to_adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_deployment_event_evaluation_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ("
            "'promote','rollback_adapter','rollback_base','rollback_retention_release'"
            ")",
            name="ck_adapter_deployment_event_type",
        ),
        CheckConstraint(
            "from_target_kind IN ('base','adapter') AND to_target_kind IN ('base','adapter')",
            name="ck_adapter_deployment_event_targets",
        ),
        CheckConstraint(
            "((from_target_kind = 'base' AND from_adapter_id IS NULL "
            "AND from_adapter_version IS NULL) OR "
            "(from_target_kind = 'adapter' AND from_adapter_id IS NOT NULL "
            "AND from_adapter_version > 0)) AND "
            "((to_target_kind = 'base' AND to_adapter_id IS NULL "
            "AND to_adapter_version IS NULL) OR "
            "(to_target_kind = 'adapter' AND to_adapter_id IS NOT NULL "
            "AND to_adapter_version > 0))",
            name="ck_adapter_deployment_event_target_shape",
        ),
        CheckConstraint(
            "((to_target_kind = 'base' AND approved_review_id IS NULL "
            "AND approved_review_version IS NULL AND evaluation_id IS NULL "
            "AND evaluation_version IS NULL AND suite_id IS NULL) OR "
            "(to_target_kind = 'adapter' AND approved_review_id IS NOT NULL "
            "AND approved_review_version > 0 AND evaluation_id IS NOT NULL "
            "AND evaluation_version > 0 AND suite_id IS NOT NULL))",
            name="ck_adapter_deployment_event_authority_shape",
        ),
        CheckConstraint(
            "deployment_version_before >= 0 AND ((event_type = "
            "'rollback_retention_release' AND "
            "deployment_version_after = deployment_version_before) "
            "OR (event_type <> 'rollback_retention_release' AND "
            "deployment_version_after > deployment_version_before))",
            name="ck_adapter_deployment_event_versions",
        ),
        Index(
            "ix_adapter_deployment_event_department_created", "department_id", "created_at", "id"
        ),
        UniqueConstraint("id", "department_id", name="uq_adapter_deployment_event_scope"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column()
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    deployment_version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    deployment_version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    from_target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    from_adapter_id: Mapped[UUID | None] = mapped_column()
    from_adapter_version: Mapped[int | None] = mapped_column(Integer)
    to_target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    to_adapter_id: Mapped[UUID | None] = mapped_column()
    to_adapter_version: Mapped[int | None] = mapped_column(Integer)
    approved_review_id: Mapped[UUID | None] = mapped_column()
    approved_review_version: Mapped[int | None] = mapped_column(Integer)
    evaluation_id: Mapped[UUID | None] = mapped_column()
    evaluation_version: Mapped[int | None] = mapped_column(Integer)
    suite_id: Mapped[UUID | None] = mapped_column()
    base_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    rollback_retention_id: Mapped[UUID | None] = mapped_column()
    actor_user_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = utc_timestamp()


class AdapterRollbackRetention(Base):
    """Explicit authority retaining one exact adapter version for rollback."""

    __tablename__ = "adapter_rollback_retentions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_adapter_rollback_retention_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["adapter_id", "department_id"],
            ["adapters.id", "adapters.department_id"],
            name="fk_adapter_rollback_retention_adapter_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approved_review_id", "department_id"],
            ["adapter_reviews.id", "adapter_reviews.department_id"],
            name="fk_adapter_rollback_retention_review_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evaluation_id", "department_id", "adapter_id", "suite_id"],
            [
                "adapter_evaluation_runs.id",
                "adapter_evaluation_runs.department_id",
                "adapter_evaluation_runs.adapter_id",
                "adapter_evaluation_runs.suite_id",
            ],
            name="fk_adapter_rollback_retention_evaluation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["creation_event_id", "department_id"],
            ["adapter_deployment_events.id", "adapter_deployment_events.department_id"],
            name="fk_adapter_rollback_retention_creation_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["release_event_id", "department_id"],
            ["adapter_deployment_events.id", "adapter_deployment_events.department_id"],
            name="fk_adapter_rollback_retention_release_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "department_id", name="uq_adapter_rollback_retention_scope"),
        UniqueConstraint(
            "id", "department_id", "adapter_id", name="uq_adapter_rollback_retention_adapter_scope"
        ),
        CheckConstraint(
            "status IN ('active','released')", name="ck_adapter_rollback_retention_status"
        ),
        CheckConstraint(
            "adapter_version > 0 AND review_version > 0 AND evaluation_version > 0 AND version > 0",
            name="ck_adapter_rollback_retention_versions",
        ),
        CheckConstraint(
            "release_reason IS NULL OR release_reason IN ('reactivated','manual_release')",
            name="ck_adapter_rollback_retention_reason",
        ),
        CheckConstraint(
            "(status = 'active' AND released_at IS NULL AND release_event_id IS NULL) OR "
            "(status = 'released' AND released_at IS NOT NULL AND release_event_id IS NOT NULL)",
            name="ck_adapter_rollback_retention_lifecycle",
        ),
        Index(
            "uq_adapter_rollback_retention_active",
            "department_id",
            "adapter_id",
            "adapter_version",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_adapter_rollback_retention_department_created", "department_id", "created_at", "id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    department_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_version: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_review_id: Mapped[UUID] = mapped_column(nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_id: Mapped[UUID] = mapped_column(nullable=False)
    evaluation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    suite_id: Mapped[UUID] = mapped_column(nullable=False)
    creation_event_id: Mapped[UUID] = mapped_column(nullable=False)
    release_event_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    release_reason: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = utc_timestamp()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PersistentAuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(action) > 0", name="ck_audit_action_nonempty"),
        CheckConstraint("length(resource_type) > 0", name="ck_audit_resource_type_nonempty"),
        CheckConstraint("result IN ('allowed','denied')", name="ck_audit_result"),
        CheckConstraint("length(reason_code) > 0", name="ck_audit_reason_nonempty"),
        Index("ix_audit_department_created", "department_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_subject: Mapped[str | None] = mapped_column(String(512))
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_identities.id", ondelete="RESTRICT")
    )
    department_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = utc_timestamp()

"""PostgreSQL persistence models through Phase 8."""

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
            "'invalid_citation','department_unavailable','database_unavailable')",
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
            "operation_id", "resource_type", "resource_id", name="uq_sft_reconciliation_item"
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

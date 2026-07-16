"""Validated public schemas through Phase 6."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth import DepartmentRole, MembershipStatus


class ORMResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DepartmentResponse(ORMResponse):
    id: UUID
    slug: str
    display_name: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class DepartmentListResponse(BaseModel):
    items: list[DepartmentResponse]
    limit: int
    offset: int


class DepartmentUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("display_name")
    @classmethod
    def trim_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value


class DepartmentArchive(BaseModel):
    confirm_slug: str


class MembershipResponse(BaseModel):
    id: UUID
    department_id: UUID
    subject: str
    role: DepartmentRole
    status: MembershipStatus
    expires_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class MembershipListResponse(BaseModel):
    items: list[MembershipResponse]
    limit: int
    offset: int


class MembershipCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=512)
    role: DepartmentRole
    expires_at: datetime | None = None

    @field_validator("subject")
    @classmethod
    def preserve_opaque_subject(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("subject must not be blank")
        return value

    @field_validator("expires_at")
    @classmethod
    def require_aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class MembershipUpdate(BaseModel):
    role: DepartmentRole | None = None
    status: MembershipStatus | None = None
    expires_at: datetime | None = None
    clear_expiry: bool = False

    @model_validator(mode="after")
    def require_unambiguous_change(self) -> MembershipUpdate:
        supplied = self.model_fields_set
        if "expires_at" in supplied and self.clear_expiry:
            raise ValueError("expires_at and clear_expiry cannot be used together")
        if not ({"role", "status", "expires_at"} & supplied) and not self.clear_expiry:
            raise ValueError("at least one membership change is required")
        return self

    @field_validator("expires_at")
    @classmethod
    def require_aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class DocumentResponse(ORMResponse):
    """Safe document metadata; internal identity and storage fields are excluded."""

    id: UUID
    department_id: UUID
    original_filename: str
    media_type: str
    byte_size: int
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    limit: int
    offset: int


class ExtractionResponse(ORMResponse):
    """Safe job metadata without claims, identities, hashes, paths, or content."""

    id: UUID
    department_id: UUID
    document_id: UUID
    status: str
    pipeline_version: str
    parser_name: str | None
    parser_version: str | None
    normalization_version: str
    chunking_version: str
    normalized_byte_size: int | None
    chunk_count: int | None
    error_code: str | None
    attempt_number: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExtractionListResponse(BaseModel):
    items: list[ExtractionResponse]
    limit: int
    offset: int


class ChunkResponse(ORMResponse):
    """Safe provenance metadata; chunk text and its digest remain external."""

    id: UUID
    extraction_id: UUID
    ordinal: int
    char_start: int
    char_end: int
    byte_size: int
    provenance_kind: str
    page_start: int | None
    page_end: int | None
    line_start: int | None
    line_end: int | None


class ChunkListResponse(BaseModel):
    items: list[ChunkResponse]
    limit: int
    offset: int


class VectorIndexingResponse(ORMResponse):
    """Safe indexing metadata without claims, paths, credentials, hashes, or vectors."""

    id: UUID
    department_id: UUID
    document_id: UUID
    extraction_id: UUID
    status: str
    embedding_pipeline_version: str
    embedding_model_id: str
    embedding_dimension: int
    distance: str
    vector_schema_version: str
    expected_chunk_count: int
    point_count: int | None
    error_code: str | None
    attempt_number: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class VectorIndexingListResponse(BaseModel):
    items: list[VectorIndexingResponse]
    limit: int
    offset: int

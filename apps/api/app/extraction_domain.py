"""Reviewed Phase 5 extraction identifiers and safe failure vocabulary."""

PIPELINE_VERSION = "phase5-extraction-v1"
NORMALIZATION_VERSION = "phase5-normalization-v1"
CHUNKING_VERSION = "phase5-character-chunker-v1"

SAFE_EXTRACTION_ERROR_CODES = frozenset(
    {
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
    }
)

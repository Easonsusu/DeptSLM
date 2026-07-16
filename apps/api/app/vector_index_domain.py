"""Reviewed Phase 6 embedding and vector-indexing contract."""

EMBEDDING_PIPELINE_VERSION = "phase6-qwen3-embedding-v1"
EMBEDDING_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_MODEL_REVISION = "d23109d65ca9fdf61eef614209744716f337f50f"
EMBEDDING_DIMENSION = 1024
EMBEDDING_DISTANCE = "cosine"
VECTOR_SCHEMA_VERSION = "phase6-qdrant-chunks-v1"
QDRANT_COLLECTION = "deptslm_chunks_qwen3_0_6b_1024_v1"
QDRANT_VECTOR_NAME = "dense"
QDRANT_VERSION = "1.13.4"

SAFE_VECTOR_INDEX_ERROR_CODES = frozenset(
    {
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
    }
)

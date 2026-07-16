# Phase 6 Qdrant Boundary

## Fixed collection

Phase 6 pins Qdrant server `1.13.4` and qdrant-client `1.13.3`. The only collection is `deptslm_chunks_qwen3_0_6b_1024_v1`, containing one named vector `dense` with size 1024 and cosine distance. Collection names are constants and are never accepted from an API or client.

Ordinary workers verify but never create, delete, or repair collection schema. Bootstrap is explicit and idempotent:

```bash
./scripts/compose.sh run --rm vector-admin bootstrap
```

Bootstrap creates the collection only when absent, verifies the exact named-vector contract, creates required payload indexes, and fails closed on mismatch. It never recreates a mismatched collection or touches unknown collections. Required indexes are keyword indexes for `department_id`, `document_id`, `extraction_id`, `indexing_id`, `vector_attempt_id`, and `embedding_pipeline_version`, plus Boolean `published`; `department_id` is tenant-enabled.

## Point and filter contract

The PostgreSQL `DocumentChunk.id` is the canonical Qdrant UUID point ID. Payload contains only:

- `department_id`, `document_id`, `extraction_id`, `chunk_id`
- `indexing_id`, `vector_attempt_id`, `ordinal`
- `provenance_kind` and the matching page or line range
- `embedding_pipeline_version`, `published`

Payload never contains chunk/normalized text, filenames, paths, hashes, users, issuers, subjects, tokens, credentials, database information, or model paths.

All direct client calls live in `deptslm_worker.qdrant_adapter`. Every adapter operation requires a typed `DepartmentScope`; no raw department string, optional/global scope, caller-provided filter, or caller-provided collection exists. Internally constructed filters always require exact `department_id`. Attempt count, inspection, activation, and deletion also require exact indexing and vector-attempt IDs. Search additionally requires `published=true` and the current embedding pipeline. Returned payloads and canonical UUIDs are validated; malformed or foreign values fail closed.

## Staging and consistency

Each claim receives a fresh random `vector_attempt_id`. Bounded upserts use `wait=true` and start with `published=false`. Finalization locks department, document, extraction, then indexing; revalidates PostgreSQL authority and lease ownership; verifies the exact staged count; activates only the exact scoped attempt; verifies the exact published count; then records PostgreSQL success and `document.vector_index.complete` in one database transaction.

Qdrant and PostgreSQL have no distributed transaction. A crash after activation but before database commit can leave physically published but untrusted points. The internal future-retrieval method therefore cross-checks every candidate against a succeeded PostgreSQL indexing row, stored document, succeeded extraction, and exact chunk/attempt scope. Failed, running, cancelled, orphaned, malformed, foreign, and soft-deleted-document points cannot pass. Phase 6 exposes no HTTP search endpoint and returns no chunk text.

Handled failure deletes only the current exact department/indexing/attempt points. Reclaim first deletes only the exact prior attempt returned by PostgreSQL. Cleanup failure blocks progress with `qdrant_cleanup_failed`; startup never performs broad deletion. Soft deletion cancels queued jobs transactionally and blocks running finalization. Successful vectors may remain physically retained until a later reviewed purge phase, but PostgreSQL authority makes them unusable.

The local single-node Compose service is a development convenience, not a production clustering, TLS, availability, backup, or tenant-security claim.

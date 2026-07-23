# Phase 6 Qdrant Boundary

## Fixed collection

Phase 6 pins Qdrant server `1.13.4` and qdrant-client `1.13.3`. The only collection is `deptslm_chunks_qwen3_0_6b_1024_v1`. Its vector configuration must be a named-vector mapping whose key set is exactly `{dense}`; `dense` is size 1024 with cosine distance. Unnamed vectors, missing or renamed `dense`, extra named vectors, wrong dimensions, and wrong distance all fail closed. Collection names are constants and are never accepted from an API or client.

Ordinary workers verify but never create, delete, or repair collection schema. Bootstrap is explicit and idempotent:

```bash
./scripts/compose.sh run --rm vector-admin bootstrap
```

Bootstrap creates the collection only when absent, verifies the exact named-vector contract, creates required payload indexes, and fails closed on mismatch. The adapter permits no point count, inspection, upsert, activation, deletion, or search until the current process has accepted the complete collection contract. It never recreates or cleans a mismatched collection and never touches unknown collections. Required indexes are keyword indexes for `department_id`, `document_id`, `extraction_id`, `indexing_id`, `vector_attempt_id`, and `embedding_pipeline_version`, plus Boolean `published`; `department_id` is tenant-enabled.

## Point and filter contract

The PostgreSQL `DocumentChunk.id` is the canonical Qdrant UUID point ID. Payload contains only:

- `department_id`, `document_id`, `extraction_id`, `chunk_id`
- `indexing_id`, `vector_attempt_id`, `ordinal`
- `provenance_kind` and the matching page or line range
- `embedding_pipeline_version`, `published`

Payload never contains chunk/normalized text, filenames, paths, hashes, users, issuers, subjects, tokens, credentials, database information, or model paths.

All direct client calls live in `deptslm_worker.qdrant_adapter`. Every adapter operation requires a typed `DepartmentScope`; no raw department string, optional/global scope, caller-provided filter, or caller-provided collection exists. Internally constructed filters always require exact `department_id`. Attempt count, inspection, activation, and deletion also require exact indexing and vector-attempt IDs. Search additionally requires `published=true` and the current embedding pipeline. Returned payloads and canonical UUIDs are validated; malformed or foreign values fail closed.

Phase 7 does not add a second adapter or public search route. The API uses this same reviewed typed boundary for a bounded internal query and passes every candidate through the existing PostgreSQL retrieval authority. Qdrant scores and payloads alone never authorize evidence. The query vector is transient and is neither persisted nor returned.

## Staging and consistency

Each claim receives a fresh random `vector_attempt_id`. Before every claim-owned Qdrant mutation, the worker requires an exact live PostgreSQL row, worker, claim token, vector attempt, scope, fixed model/vector contract, and server-time lease. Bounded upserts use `wait=true` and start with `published=false`. Finalization locks department, document, extraction, then indexing; revalidates PostgreSQL authority immediately before activation; verifies the exact staged count; activates only the exact scoped attempt; verifies the exact published count; then records PostgreSQL success and `document.vector_index.complete` in one database transaction.

Qdrant and PostgreSQL have no distributed transaction. A crash after activation but before database commit can leave physically published but untrusted points. The internal future-retrieval method therefore cross-checks every candidate against a succeeded PostgreSQL indexing row, stored document, succeeded extraction, and exact chunk/attempt scope. Failed, running, cancelled, orphaned, malformed, foreign, and soft-deleted-document points cannot pass. Phase 6 exposes no HTTP search endpoint and returns no chunk text.

Handled failure deletes only the current exact department/indexing/attempt points, and only when the collection was verified, the attempt may have written points, and the claim remains live. A returned delete is not success until exact unpublished and published counts are both zero. Reclaim deletes and verifies only the exact prior attempt returned by PostgreSQL before processing, rechecks replacement ownership, and repeats prior-attempt cleanup before activation to remove late stale writes. Cleanup failure blocks success, requeue, or replacement progress with `qdrant_cleanup_failed`; startup never performs broad deletion.

PostgreSQL cannot transactionally cancel a Qdrant request already in flight. A stale write completing after the final cleanup can remain physically unpublished, and activation followed by a failed PostgreSQL commit can leave a published orphan. Neither is retrieval authority: search requires `published=true` and the current pipeline, and every candidate must also match a committed succeeded PostgreSQL row, stored document, succeeded extraction, and exact chunk/attempt ownership. Soft deletion blocks the same authority even if vectors remain physically stored.

The local single-node Compose service is a development convenience, not a production clustering, TLS, availability, backup, or tenant-security claim.

An activated point remains physically possible after a failed PostgreSQL commit, and soft-deleted-document points may remain stored. Neither is answer authority: Phase 7 requires a committed succeeded indexing row, stored document, succeeded extraction, exact current attempt/chunk ownership, and unchanged selected artifacts before generation and accepted completion.

Phase 9 adds no Qdrant client or filter. Evaluation calls the same typed `DepartmentScope` production retrieval path, fixed `published=true` and current-pipeline filter, and PostgreSQL succeeded-state candidate authority. Authorized candidate UUIDs exist ephemerally only for deterministic scoring and never enter PostgreSQL, public APIs, or result artifacts.

# Phase 6 Vector Indexing

## Scope

Phase 6 adds department-scoped PostgreSQL indexing jobs and an indexing-worker path for succeeded Phase 5 chunks. API handlers enqueue or read content-free metadata only. They never read chunk text, load a model, or contact Qdrant. No public semantic search, query embedding, RAG, reranking, answer generation, citation, frontend indexing UI, or training behavior is implemented.

## Database and API

Alembic revision `0004_phase6_vector_indexing` adds `document_vector_indexings`. Each attempt binds non-null department, document, and extraction IDs through composite `RESTRICT` foreign keys and records the fixed embedding/model/vector contract, expected and completed point counts, safe status/error metadata, retry history, claim ownership, lease, and timestamps. PostgreSQL stores no vectors, text, paths, URLs, keys, raw dependency responses, or model locations.

States are `queued`, `running`, `succeeded`, `failed`, and `cancelled`. Lifecycle checks keep queued rows claim/result-free, require complete ownership for running rows, require exact point count for success, and require an allowlisted error for failure/cancellation. Partial indexes permit one active pipeline attempt and one succeeded current contract per extraction; failed rows allow explicit retry.

Endpoints are:

- `POST /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings` (`202`)
- `GET /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings`
- `GET /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings/{indexing_id}`
- `POST /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings/{indexing_id}/retry` (`202`)

Enqueue/retry require same-department `system_admin`, `department_admin`, or `instructor`; all five active roles may read safe status. There is no cross-department system-admin bypass. Enqueue requires a stored document and succeeded nonempty extraction; retry creates a new row only from a failed attempt. Success audits are `document.vector_index.enqueue`, `document.vector_index.retry`, and worker-only `document.vector_index.complete`.

## Worker lifecycle

```bash
./scripts/compose.sh run --rm indexing-worker python -m deptslm_worker.indexer --once
./scripts/compose.sh run --rm indexing-worker python -m deptslm_worker.indexer --poll
```

Claims use PostgreSQL server time, `FOR UPDATE SKIP LOCKED`, random worker UUIDs, and fresh claim/vector-attempt IDs. The claim commits before artifact or network work. Heartbeat, requeue, failure, activation, and finalization require exact worker, claim, attempt, running state, fixed model/vector contract, and a lease strictly in the future. Every claim-owned Qdrant mutation first performs this short PostgreSQL server-time ownership check; database failure is never treated as ownership. Expiry is non-revivable.

The worker reads only the exact Phase 5 final allowlist beneath `DEPTSLM_DATA_DIR/extracted_text/<department>/<document>/<extraction>/`. Descriptor-relative no-follow opens reject symlinks and unexpected entries. It verifies manifest scope and version identifiers, exact normalized/chunk hashes and sizes, final output size, incremental bounded JSONL, contiguous ordinals, and exact PostgreSQL chunk IDs, digests, byte sizes, offsets, and page/line provenance before embedding each batch. It never buffers the complete chunk artifact, uses original filenames, or logs content, paths, or hashes.

One secret-free offline embedding subprocess loads the pinned model once per job. Batches are bounded by count, total characters, and a calculated worst-case encoded JSON request size. Request writes are nonblocking and share one deadline with response receipt; timeout, heartbeat, shutdown, claim loss, and child exit remain active while either side of the pipe is stalled. Every ordered 1024-dimensional vector is validated before a content-free point is staged. No PostgreSQL transaction remains open during inference or normal Qdrant writes.

Cleanup is permitted only after the fixed collection schema was accepted and only while the exact PostgreSQL claim remains live. Exact deletion uses department, indexing, and vector-attempt filters, then requires both unpublished and published zero counts. On graceful shutdown, the process group stops, the exact current attempt is cleaned, and the job requeues only if the lease is still valid. A hard crash relies on expiry; reclaim preserves the old attempt ID, performs verified cleanup before processing, and repeats the same old-attempt cleanup immediately before activating the replacement. Any model, artifact, database, Qdrant, point-count, claim, or cleanup failure prevents success.

## Soft deletion and limitations

Document soft deletion cancels queued indexing attempts with `document_unavailable` in the same PostgreSQL transaction. Running workers fail finalization after document revalidation. No Qdrant call occurs in deletion. Succeeded vectors may remain physically retained, but the internal retrieval cross-check rejects deleted documents; automatic physical purge is deferred.

Google Drive remains a local-development convenience for `DEPTSLM_DATA_DIR`, not reviewed production database, object, model, or vector storage. PostgreSQL cannot transactionally fence a Qdrant request already in flight. Exact pre-activation stale cleanup removes a late completed stale write observed before activation, while `published=true` plus committed PostgreSQL `succeeded` authority keeps later or activated orphans untrusted. PostgreSQL/Qdrant commits are not atomic, local Qdrant is not a production deployment, the constrained embedding subprocess is not a kernel sandbox, and operational reconciliation, backups, TLS, clustering, and production secrets remain deferred.

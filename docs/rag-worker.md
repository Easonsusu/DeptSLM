# Phase 5 RAG Worker

## Commands

The RAG worker currently performs extraction and chunking only:

```bash
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --once
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --poll
```

`--once` claims at most one PostgreSQL job and exits successfully when the queue is empty. `--poll` waits for work using `DEPTSLM_WORKER_POLL_SECONDS`. Each process generates a random worker UUID; hostnames, usernames, email addresses, and machine paths are not worker identity.

## Claim and lease behavior

Claims use row locking with `SKIP LOCKED`. A claim sets a fresh token, worker UUID, start/claim time, and lease, then commits before source I/O. PostgreSQL server time is authoritative. Heartbeat, failure, requeue, and finalization require the matching extraction ID, worker ID, claim token, running status, and a lease strictly in the future. Expiry is non-revivable: reclaimed work receives a new token and the prior token cannot regain authority.

On reclaim, the new worker retains the old token only long enough to recursively clean that exact department/document/extraction/claim staging scope without following symlinks. Cleanup is idempotent and retried narrowly; unrelated claims and unknown final directories are untouched. If exact cleanup cannot complete, the new claim fails safely without parsing or publishing.

`SIGTERM` and `SIGINT` request shutdown. The worker terminates the parser process group, removes the source snapshot, scratch content, and exact claim staging, and returns the job to `queued` only if its lease remains valid. A hard crash relies on lease expiry and a future reclaim; staging can remain until that happens.

## Container boundary

The image runs as a non-root user with a read-only root filesystem, all Linux capabilities dropped, and `no-new-privileges`. It publishes no port, receives no authentication settings, does not depend on Qdrant, and runs no migration. Compose mounts only `uploads` read-only and `extracted_text` read-write; adapters, model caches, datasets, other runtime areas, and repository source are not mounted.

The parent needs `DATABASE_URL`; the parser subprocess does not receive it. The child receives only the verified source-snapshot descriptor, fixed output/result descriptors, and a separate scratch descriptor—not the live source or a publishable directory descriptor. Google Drive remains a local-development convenience, not production worker/object storage.

## Settings

| Variable | Default | Reviewed bound |
| --- | ---: | ---: |
| `DEPTSLM_EXTRACTION_TIMEOUT_SECONDS` | 120 | 1–600 |
| `DEPTSLM_MAX_EXTRACTED_BYTES` | 104857600 | 1–524288000 |
| `DEPTSLM_MAX_PDF_PAGES` | 1000 | 1–5000 |
| `DEPTSLM_CHUNK_MAX_CHARS` | 1200 | 256–8192 |
| `DEPTSLM_CHUNK_OVERLAP_CHARS` | 200 | 0–4096 and at most half the chunk size |
| `DEPTSLM_MAX_CHUNKS_PER_DOCUMENT` | 100000 | 1–1000000 |
| `DEPTSLM_EXTRACTION_LEASE_SECONDS` | 300 | 1–3600 and timeout plus at least 30 |
| `DEPTSLM_WORKER_POLL_SECONDS` | 5 | 1–60 |
| `DEPTSLM_DEPARTMENT_EXTRACTED_QUOTA_BYTES` | 4294967296 | at least max extracted bytes |

Explicit values are ASCII decimals and invalid configuration stops startup. Worker settings are separate from API authentication settings.

## Limitations

There is no network listener, production broker, automatic retry loop, cancellation API, OCR, malware scanner, Qdrant, embedding, model, or RAG behavior. Constrained Python subprocesses are not a kernel malware sandbox and arbitrary parser-code execution is not considered safely contained. Seccomp, microVMs, antivirus, CDR, and a dedicated parser service remain deferred. Hard-crash final-output orphan reconciliation and production storage are also deferred.

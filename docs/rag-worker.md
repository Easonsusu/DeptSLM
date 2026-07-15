# Phase 5 RAG Worker

## Commands

The RAG worker currently performs extraction and chunking only:

```bash
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --once
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --poll
```

`--once` claims at most one PostgreSQL job and exits successfully when the queue is empty. `--poll` waits for work using `DEPTSLM_WORKER_POLL_SECONDS`. Each process generates a random worker UUID; hostnames, usernames, email addresses, and machine paths are not worker identity.

## Claim and lease behavior

Claims use row locking with `SKIP LOCKED`. A claim sets a fresh token, worker UUID, start/claim time, and lease, then commits before source I/O. Heartbeats extend ownership in short independent transactions. Expired work can be reclaimed under a new token. Every heartbeat and finalization matches extraction ID, worker ID, and claim token, so a stale process cannot publish after losing ownership.

`SIGTERM` and `SIGINT` request shutdown. The worker terminates the parser process group, removes exact claim staging, and returns the job to `queued` only if it still owns the claim. A hard crash relies on lease expiry.

## Container boundary

The image runs as a non-root user with a read-only root filesystem, all Linux capabilities dropped, and `no-new-privileges`. It publishes no port, receives no authentication settings, does not depend on Qdrant, and runs no migration. Compose mounts only `uploads` read-only and `extracted_text` read-write; adapters, model caches, datasets, other runtime areas, and repository source are not mounted.

The parent needs `DATABASE_URL`; the parser subprocess does not receive it. Google Drive remains a local-development convenience, not production worker/object storage.

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

There is no network listener, production broker, automatic retry loop, cancellation API, OCR, malware scanner, Qdrant, embedding, model, or RAG behavior. Constrained Python subprocesses are not a production malware sandbox. Hard-crash output orphan reconciliation and production storage are deferred.

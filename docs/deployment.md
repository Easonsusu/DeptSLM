# Deployment and Local Development

## Phase 8 status

DeptSLM is not production ready. Phase 8 adds PostgreSQL-only structured feedback, review, retention, and explicit purge to the completed Phase 7 grounded-answer boundary. Public vector search, conversations, history, streaming, reranking, automated evaluation, scheduled purge, malware scanning, OCR, training, production identity/storage, secrets management, backups, clustering, and production operations remain deferred.

## Planned local services

| Service | Role | Current expectation |
| --- | --- | --- |
| `web` | Next.js user interface | One-turn department answer form plus the landing page; no stored history. |
| `api` | FastAPI control plane | Auth, content workflows, internal scoped retrieval, citation validation, and final authority; no model inference dependencies. |
| `postgres` | Application metadata database and worker queues | Identities, memberships, content/job metadata, content-free answer/citation provenance, and audit events. |
| `qdrant` | Local vector service | Pinned 1.13.4, localhost ports, API-key protected, fixed Phase 6 collection; no production claim. |
| `rag-worker` | Extraction jobs | Source verification, constrained parsing, normalization, and chunking; no Qdrant/model dependency. |
| `indexing-worker` | Phase 6 embedding/indexing jobs | Read-only extracted/model mounts, offline pinned model, typed department Qdrant adapter; no public retrieval. |
| `model-admin` | Explicit model preparation | Writes only the external model cache when invoked; receives no database or Qdrant credentials. |
| `vector-admin` | Explicit Qdrant bootstrap | Verifies the fixed collection contract; receives no database, model-cache, or document access. |
| `rag-runtime` | Private Phase 7 inference | HTTP supervisor plus one killable persistent model child; offline query embedding and non-thinking generation; no database/Qdrant/API-auth credentials or host port. |
| `training-worker` | Future LLaMA-Factory jobs | Structural placeholder; no fine-tuning is implemented. |

Phase 6 pins `Qwen/Qwen3-Embedding-0.6B` at immutable revision `d23109d65ca9fdf61eef614209744716f337f50f`; Phase 7 pins `Qwen/Qwen3-0.6B` at revision `c1899de289a04d12100db370d81485cdf75e47ca`. Explicit administration downloads them outside Git while normal processes stay offline. LlamaIndex and LLaMA-Factory remain future components. pypdf remains extraction-only; model inference dependencies remain outside API/extraction/indexing images.

## Prerequisites

- macOS for the provided Google Drive setup script
- a mounted Google Drive desktop folder for persistent local runtime artifacts
- Git
- Docker Desktop with the `docker compose` command
- enough local resources for PostgreSQL and Qdrant

Running application skeletons directly outside containers requires Node.js 20 or newer and Python 3.11 or newer, as declared by the current manifests. Do not assume globally installed tool versions when a repository file provides one.

## Configure external runtime storage

Runtime artifacts must never be stored inside the checkout. First run:

```bash
./scripts/setup_google_drive_storage.sh
```

The script searches likely directories under:

```text
~/Library/CloudStorage/GoogleDrive-*
```

It detects the existing personal-drive folder (`My Drive` or the localized `我的雲端硬碟`), creates `DeptSLM` and the required artifact subdirectories without deleting existing files, then prints the `DEPTSLM_DATA_DIR` value. With multiple accounts it chooses the strongest unambiguous match and stops without writing if the best candidates are tied.

Create a local, untracked environment file:

```bash
cp .env.example .env
```

Set these values as appropriate for the local environment:

- `DEPTSLM_DATA_DIR`: the absolute path printed by the setup script
- `DATABASE_URL`: the API PostgreSQL connection URL using `postgresql+psycopg://`
- `DEPTSLM_QDRANT_URL`: indexing/admin-only Qdrant URL; local Compose uses `http://qdrant:6333`
- `DEPTSLM_QDRANT_API_KEY`: long non-placeholder untracked key, also configured on local Qdrant
- `DEPTSLM_QDRANT_COLLECTION`: fixed `deptslm_chunks_qwen3_0_6b_1024_v1`
- `DEPTSLM_EMBEDDING_MODEL_REVISION`: exact immutable reviewed SHA
- `DEPTSLM_GENERATION_MODEL_REVISION`: exact immutable reviewed generation SHA
- `DEPTSLM_RAG_RUNTIME_TOKEN`: long non-placeholder untracked internal bearer token
- `DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS`: strict feedback retention in days, default `180`, allowed `30` through `730`
- `API_PORT`: API host port, normally `8000`
- `WEB_PORT`: web host port, normally `3000`
- `ENVIRONMENT`: local environment name, normally `development`

Do not commit `.env`. Do not put production credentials in `.env.example` or Compose defaults.

## Validate and start

Build the API image and apply the schema through Compose before startup:

```bash
./scripts/compose.sh build api
./scripts/compose.sh run --rm api python -m alembic upgrade head
```

This command uses the Compose-internal `postgres` hostname from `.env`. When running Alembic directly from the host in `apps/api`, set `DATABASE_URL` to a host-accessible URL such as `postgresql+psycopg://deptslm:deptslm@localhost:5432/deptslm`; the Compose hostname does not resolve from the host.

Bootstrap the first local department through the same image:

```bash
./scripts/compose.sh run --rm api python -m app.admin bootstrap-department \
  --slug computer-science \
  --display-name "Computer Science" \
  --admin-issuer https://local-issuer.invalid \
  --admin-subject opaque-admin-subject
```

Bootstrap remains disabled outside explicit reviewed local/test environments. Compose passes `DEPTSLM_AUTH_MODE`, issuer, audience, and secret only to the API container. Keep the generated secret only in the untracked `.env`; it is not passed to web, PostgreSQL, Qdrant, or workers.

Before startup, render the resolved Compose configuration through the repository wrapper:

```bash
./scripts/compose.sh config
```

The wrapper loads `DEPTSLM_DATA_DIR` from the shell or local `.env`, resolves it, and refuses missing, relative, root, nonexistent, non-writable, source-overlapping, or incomplete paths before Docker can create a bind mount. It also supplies the guard required by `docker-compose.yml`, so invoking `docker compose` directly is rejected. Review the rendered configuration, then build and start the services:

```bash
./scripts/compose.sh up --build
```

With the default ports, basic checks are:

```bash
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/version
```

Open `http://localhost:3000` for the landing page. These checks prove only that the Phase 0 skeletons respond; they do not prove database, vector search, storage, model, RAG, or training readiness.

Inspect status and logs with:

```bash
./scripts/compose.sh ps
./scripts/compose.sh logs api web
```

Run one extraction attempt or the long-lived poller with:

```bash
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --once
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --poll
```

The worker depends only on PostgreSQL health, publishes no port, receives no auth secret, mounts uploads read-only and extracted text read-write, and runs no migrations. See [rag-worker.md](rag-worker.md) for settings, leases, and sandbox limitations.

Prepare the model and Qdrant schema explicitly, then run one indexing attempt or poller:

```bash
./scripts/compose.sh run --rm model-admin \
  python -m deptslm_worker.model_admin prepare-embedding
./scripts/compose.sh run --rm vector-admin bootstrap
./scripts/compose.sh run --rm indexing-worker \
  python -m deptslm_worker.indexer --once
./scripts/compose.sh run --rm indexing-worker \
  python -m deptslm_worker.indexer --poll
```

Preparation is never automatic. For gated access only, add `-e HF_TOKEN` after `run --rm` to forward an already-exported, untracked token only to `model-admin`; the public model needs no token. Normal indexing receives no token, has networking only to PostgreSQL/Qdrant, mounts extracted text/model cache read-only, and cannot create the collection. Qdrant settings are passed only to Qdrant, the indexing worker, `vector-admin`, and the Phase 7 API retrieval boundary—not web, extraction, parser, model preparation, the model runtime, or training workers.

Prepare both exact Phase 7 models and start the private runtime as part of the stack:

```bash
./scripts/compose.sh run --rm model-admin \
  python -m deptslm_worker.model_admin prepare-rag-models
./scripts/compose.sh up --build rag-runtime api web
```

The runtime mounts only external `model_cache` read-only and joins only the internal RAG network. The API receives its private URL/token plus Qdrant access for reviewed retrieval, but contains no Transformers or sentence-transformers dependency. The runtime receives none of the API database, Qdrant, JWT, upload, or extraction settings. Do not add database, Qdrant, app-auth, Hugging Face token, cloud credential, or proxy variables to the runtime service: startup fails closed. The HTTP bearer token remains only in the supervisor and is omitted from the model child environment.

Model execution is single-concurrency by contract. The supervisor uses bounded framed IPC and fixed deadlines; timeout, disconnect, cancellation, shutdown, malformed output, or child failure terminates and reaps the process group before a clean child may serve another request. Generation tokenizes the complete chat template without truncation, caps operational input at 8,192 tokens, reserves 512 new tokens within the exact 40,960-token model context, and caps query-embedding input at 2,048 tokens.

Stop local services with:

```bash
./scripts/compose.sh down
```

Do not add a volume-deletion flag unless destruction of local service state is explicitly intended and reviewed.

## Structured-feedback retention

Compose passes `DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS` only to the API. It adds no service, secret, mount, or automatic job. Expired feedback is hidden using PostgreSQL server time before physical deletion. An active same-department system or department administrator must invoke `python -m app.admin purge-rag-feedback` explicitly; see [feedback-retention.md](feedback-retention.md). Local Compose scheduling, PostgreSQL storage, backups, and audit retention are not a production privacy or retention guarantee.

## Runtime mounts and persistence

Services that write file artifacts must receive `DEPTSLM_DATA_DIR` explicitly and use only its approved subdirectories. A missing value must fail clearly; Compose or application code must not create fallback directories in the checkout. Department-owned paths must be isolated by a validated `department_id` in future phases.

PostgreSQL and live Qdrant state are bind-mounted beneath `DEPTSLM_DATA_DIR/service_state`, never inside the repository. This Compose stack is for local development only. Before using real data, review whether a synchronized folder is safe for these databases and document migration behavior, backup and restore, retention, deletion, sync implications, and recovery testing. Portable Qdrant snapshots belong under `DEPTSLM_DATA_DIR/vector_snapshots`.

Google Drive is appropriate for the requested local artifact layout, but it is not a production database or object-store design. Avoid concurrent database access through synced files and do not assume that synchronization is atomic, complete, or a substitute for backups.

## Tests and CI

CI must not depend on a developer's Google Drive or reuse real data. It should create a temporary directory, export that absolute path as `DEPTSLM_DATA_DIR`, run the relevant checks, and discard the directory afterward. Test inputs must be small and synthetic.

GitHub Actions provides PostgreSQL 16 and Qdrant 1.13.4 with isolated test credentials. Locally, run `python -m pytest -m "not postgres and not qdrant"` without services, or provide isolated PostgreSQL/Qdrant settings. Neither suite silently skips in CI, and the fake embedding provider is accepted only with exact `ENVIRONMENT=test`. CI never downloads the real model.

CI builds API, extraction-worker, indexing-worker, and private RAG-runtime targets. It verifies migration `0006_phase8_rag_feedback`, confirms dependency/credential isolation and absence of model weights, runs extraction/indexing empty-queue and fake-runtime request smoke tests, exercises Qdrant bootstrap/tenant isolation/retrieval authority, and runs PostgreSQL migration/API/feedback-retention coverage with temporary `uploads`, `extracted_text`, and `model_cache`. Controlled child tests prove timeout/restart, cancellation/shutdown, framing bounds, capacity release, and a child environment without the runtime token or other secrets. Feedback tests prove PostgreSQL-only isolation and content-free schemas; they require no storage mount. Fake models are allowed only in exact test mode; real-model smokes remain opt-in. CI never uses Google Drive or downloads a model.

At minimum, future deployment checks should cover:

- Compose configuration rendering
- web lint, type-check, test, and build commands
- API lint, type-check, and test commands
- API health and version smoke checks
- clear failure when required external storage is missing
- prevention of writes into the repository
- department-boundary and untrusted-retrieval tests once those features exist

Use the actual commands declared by each app's manifests; Phase 0 does not prescribe a monorepo task runner.

## Production deployment is deferred

Docker Compose is for local development, not the production architecture. A production design must be approved before real university data is used and should address at least:

- TLS, ingress, domains, and network segmentation
- SSO, role-based access, department isolation, and audit trails
- managed secrets and credential rotation
- durable PostgreSQL, Qdrant, object storage, and backups
- queueing, worker scaling, retries, idempotency, and cancellation
- model licensing, serving hardware, autoscaling, quotas, and cost controls
- sandboxed document extraction and upload scanning
- prompt-injection defenses and grounded-answer evaluation
- monitoring, tracing, alerting, retention, disaster recovery, and incident response
- safe database migrations and rollback
- adapter approval, deployment, and rollback

No Phase 0 file should be interpreted as a production security or availability guarantee.

## Troubleshooting

- **The storage script cannot find Google Drive:** confirm Google Drive for desktop is installed, signed in, and mounted under `~/Library/CloudStorage`. Do not create a runtime folder in the repo as a workaround.
- **`DEPTSLM_DATA_DIR` contains spaces:** keep the full absolute value in `.env`; scripts and Compose mounts must quote it correctly.
- **Compose rejects the storage path:** set `DEPTSLM_DATA_DIR` to the external absolute path printed by the setup script, then rerun `./scripts/compose.sh config`. Never bypass the wrapper with a repository-local path.
- **A worker exits without work:** `--once` intentionally succeeds on an empty queue. Use the matching `--poll` command for extraction or indexing.
- **The indexer reports model unavailable:** run the explicit pinned preparation command. Never copy model weights into Git or enable network fallback.
- **Qdrant schema mismatch:** verify the fixed collection and payload indexes. Bootstrap never deletes/recreates a mismatch; repair requires a separately reviewed operational decision.

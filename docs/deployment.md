# Deployment and Local Development

## Phase 5 status

DeptSLM is not production ready. Phase 5 adds a PostgreSQL extraction queue and constrained PDF/text/Markdown RAG worker to the Phase 4 upload foundation. Malware scanning, OCR, Qdrant, embeddings, RAG, model serving, training, production identity/storage, secrets management, backups, and production operations remain deferred.

## Planned local services

| Service | Role | Current expectation |
| --- | --- | --- |
| `web` | Next.js user interface | Basic landing page only. |
| `api` | FastAPI control plane | System, auth, department, membership, upload, and extraction-metadata APIs. |
| `postgres` | Application metadata database and Phase 5 queue | Identities, departments, memberships, documents, extraction/chunk metadata, leases, and audit events. |
| `qdrant` | Vector search | Local service placeholder; no ingestion is implemented. |
| `rag-worker` | Extraction and future retrieval jobs | Phase 5 PostgreSQL queue, source verification, constrained parsing, normalization, and chunking; no retrieval. |
| `training-worker` | Future LLaMA-Factory jobs | Structural placeholder; no fine-tuning is implemented. |

Qwen3, Qwen3-Embedding, LlamaIndex, and LLaMA-Factory are target components, but Phase 5 does not download models or add heavy training/inference dependencies. pypdf is isolated to worker/test dependencies.

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
- `QDRANT_URL`: the future Qdrant service URL
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

Stop local services with:

```bash
./scripts/compose.sh down
```

Do not add a volume-deletion flag unless destruction of local service state is explicitly intended and reviewed.

## Runtime mounts and persistence

Services that write file artifacts must receive `DEPTSLM_DATA_DIR` explicitly and use only its approved subdirectories. A missing value must fail clearly; Compose or application code must not create fallback directories in the checkout. Department-owned paths must be isolated by a validated `department_id` in future phases.

PostgreSQL and live Qdrant state are bind-mounted beneath `DEPTSLM_DATA_DIR/service_state`, never inside the repository. These Phase 0 definitions are local placeholders. Before using real data, review whether a synchronized folder is safe for these databases and document migration behavior, backup and restore, retention, deletion, sync implications, and recovery testing. Portable Qdrant snapshots belong under `DEPTSLM_DATA_DIR/vector_snapshots`.

Google Drive is appropriate for the requested local artifact layout, but it is not a production database or object-store design. Avoid concurrent database access through synced files and do not assume that synchronization is atomic, complete, or a substitute for backups.

## Tests and CI

CI must not depend on a developer's Google Drive or reuse real data. It should create a temporary directory, export that absolute path as `DEPTSLM_DATA_DIR`, run the relevant checks, and discard the directory afterward. Test inputs must be small and synthetic.

GitHub Actions provides PostgreSQL 16 and sets an isolated `DATABASE_TEST_URL`. Locally, run `python -m pytest -m "not postgres"` without PostgreSQL, or point `DATABASE_TEST_URL` to an isolated test database and run migrations followed by `python -m pytest`. PostgreSQL tests never fall back to SQLite and CI fails if they are skipped.

CI builds both API and worker images. It verifies migration `0003_phase5_extraction`, confirms pypdf is present only in worker/test dependencies, imports the installed runner, and runs a one-shot empty-queue smoke test. PostgreSQL 16 runs migration, API, lease, worker, and concurrency tests with temporary `uploads` and `extracted_text` directories. CI never uses Google Drive.

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
- **The RAG worker exits without work:** `--once` intentionally succeeds on an empty queue. Use `--poll` for continuous extraction. Qdrant/retrieval remains deferred.
- **A model is unavailable:** expected in Phase 0; do not add model weights to Git.

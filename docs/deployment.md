# Deployment and Local Development

## Phase 12 status

DeptSLM is not a production deployment. Phase 9 completed the internal evaluation runner with external immutable suites, content-free result artifacts, and PostgreSQL-backed leases. It reuses the completed Phase 7 grounded-answer boundary; it is not a public evaluation API. Phase 10 completed an isolated dataset-builder worker that writes only private external SFT dataset artifacts. Phase 11 completed a separate worker that generates immutable LlamaFactory configuration bundles. Phase 12.0 through Phase 12.4 are completed, and Roadmap v2 Phase 14.0 and 14.1 are complete. Phase 14.2 adds an opt-in private Linux/NVIDIA runtime that may execute the exact offline LoRA/QLoRA contract and retain only non-authoritative candidate output; it does not publish or route adapters. Phase 12.4 captures immutable deployment snapshots and routes only adapter-target generation to a separate private runtime. It verifies the exact registry final through a read-only mount; query embedding and retrieval remain on the existing base runtime. Public vector search, conversations, history, streaming, reranking, scheduled evaluation, malware scanning, OCR, production identity/storage, secrets management, backups, clustering, and production operations remain deferred. Real GPU validation is opt-in; Phase 14.3 and Phase 15 have not started.

Phase 12.1D, E-A, E-B, and E-C are complete; Phase 12.2, Phase 12.3, and Phase 12.4 are complete. The public adapter API remains limited to closed content-free metadata reads, evaluation metadata, and explicit governance metadata; the existing RAG answer route is the only public generation surface and exposes no deployment selector. The source CLI is administrator-controlled and dry-run by default. The registry, evaluation, and governance workers are internal PostgreSQL-enqueued workers; governance verifies a read-only registry final and does not load or route an adapter. The separate maintenance profile runs E-A reconciliation, E-B purge, or E-C lifecycle release only when explicitly invoked; it has no model, Qdrant, dataset, training-job, or runtime mount. PostgreSQL, external storage, and runtime processes remain non-atomic.

The planned intake begins with an administrator-controlled CLI that creates an
immutable server-ID-derived import bundle from only the two payload files.
DeptSLM generates a closed, content-free intake manifest binding exact source,
attempt, department, safe internal request reference, payload digests and sizes,
contract version, and code revision; external manifests and host paths are never
authority. Import sources have separate staging, committed, claimed, consumed,
rejected, abandoned, purge-pending, and purged states, and one committed source
may be consumed by only one exact adapter version. The separate adapter-registry
worker mounts PostgreSQL, adapter imports read-only, Phase 11 training-job
bundles read-only, and registry storage read-write. The API and browser
receive no adapter storage mount or weight-upload route. A rollback-retention
reference may be removed by a reviewed pre-purge mutation. Registry and source
purge are separate, bounded, descriptor-relative, crash-resumable operations;
upstream retention dependencies release only after both byte surfaces are
committed purged. The registry manifest records governance lineage and a
declared external training association, not proven training provenance.

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
| `adapter-runtime` | Phase 12.4 deployed-adapter generation | Separate private HTTP supervisor and one killable target child; exact offline PEFT load from descriptor-verified registry copies; only read-only `model_cache` and `adapters/registry`, private tmpfs, no host port, no database/Qdrant/evaluation/API-auth credentials. |
| `training-worker` | Phase 10 SFT dataset builder | PostgreSQL plus `training_datasets` only; no model, Qdrant, RAG, or adapter stack. |
| `training-job-worker` | Phase 11 immutable job-bundle generator | PostgreSQL plus `training_datasets` only; no model, tokenizer, LlamaFactory package, Qdrant, RAG, or adapter stack. |
| `training-execution-worker` | Phase 14.2 execution control plane | Opt-in `training` profile; PostgreSQL, exact Phase 11 snapshot verification, `training_runs`, read-only `training_datasets`, and private runtime IPC; no model cache or training stack. Its `DEPTSLM_TRAINING_EXECUTION_CODE_REVISION` is independent from the Phase 11 worker's `DEPTSLM_TRAINING_JOB_CODE_REVISION`. |
| `training-runtime` | Phase 14.2 private offline trainer | Opt-in `training` profile; Linux/x86_64 one-GPU runtime with the exact model-cache directory and IPC volume only; no `training_datasets`, `training_runs`, database, network, or public port. |
| `adapter-registry-worker` | Phase 12.1C immutable registry publisher | PostgreSQL, read-only adapter imports and Phase 11 bundles, and private registry staging/final storage only; no model, Qdrant, RAG, evaluation, or public API. |
| `adapter-evaluator` | Phase 12.2 paired baseline/candidate evaluator | `adapter-evaluation` profile; PostgreSQL, extracted-text read-only, Qdrant, base RAG runtime, candidate runtime, and private `eval_results` only; no API or model-cache mount. |
| `adapter-eval-runtime` | Phase 12.2 isolated candidate runtime | `adapter-evaluation` profile; private model-cache and registry-final read-only mounts, separate token, offline child process, and no database, Qdrant, API-auth, or cloud credentials. |
| `adapter-governance-worker` | Phase 12.3 review/deployment governance worker | PostgreSQL plus the exact external `adapters/registry` final mount read-only; its dedicated settings loader requires no `uploads`, imports, staging, model, tokenizer, Qdrant, RAG-runtime, dataset, training-job, evaluation-result, upload, extraction, or adapter-write mount. |
| `adapter-maintenance` | Manual Phase 12.1E-A reconciliation, Phase 12.1E-B purge, or Phase 12.1E-C lifecycle release | `maintenance` profile; PostgreSQL plus the external adapters root only; no API, model, Qdrant, dataset, training-job, or adapter-runtime stack. E-C itself reads storage only. |

### Phase 12.1E-B purge command

Run the maintenance image explicitly; it is never started as a background
service and dry-run is the default:

```bash
./scripts/compose.sh --profile maintenance run --rm adapter-maintenance \
  python -m app.admin purge-adapter-artifacts \
  --department-id <department-uuid> --adapter-id <adapter-uuid> \
  --actor-issuer <issuer> --actor-subject <subject>
```

Add `--apply` only after reviewing the bounded dry-run counts. The command
deletes no source bytes until the exact registry final has been independently
verified and deleted. It writes no content to PostgreSQL, API responses, logs,
or CLI output, and never touches Phase 10/11 artifacts, audit history,
backups, or other retained copies. See
[adapter-artifact-purge.md](adapter-artifact-purge.md).

### Phase 12.1E-C lifecycle-release command

The same manual profile can perform the separate metadata-only release after
E-B completes. It is dry-run by default and accepts no artifact path or
manifest selector:

```bash
./scripts/compose.sh --profile maintenance run --rm adapter-maintenance \
  python -m app.admin release-adapter-upstream-dependency \
  --department-id <department-uuid> --adapter-id <adapter-uuid> \
  --expected-adapter-version <version> --expected-source-version <version> \
  --expected-dependency-version <version> \
  --actor-issuer <issuer> --actor-subject <subject>
```

Add `--apply` only after the exact E-B authority and read-only storage-absence
proof succeed. E-C changes only one dependency and adapter version; it never
deletes, moves, or reads adapter artifact content. See
[adapter-lifecycle-release.md](adapter-lifecycle-release.md).

Phase 6 pins `Qwen/Qwen3-Embedding-0.6B` at immutable revision `d23109d65ca9fdf61eef614209744716f337f50f`; Phase 7 and the Phase 12.4 production adapter runtime pin `Qwen/Qwen3-0.6B` at revision `c1899de289a04d12100db370d81485cdf75e47ca`. Phase 14.2 uses the same base revision in a separate pinned Linux runtime image (`nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356`) with lock SHA `22e92e62895cdddc49ba6ab3545d2134dd6dbfb44616646b72cb09caa19cc5a5` and environment fingerprint `123be4e6f366a28a0d56a7f51451397cdb05b5cb6a82a42ce66f487531c3978c`. Explicit administration downloads models outside Git while normal processes stay offline. Phase 12.4 keeps model inference dependencies outside the API, extraction, indexing, and base `rag-runtime` images; only the separate adapter-runtime image includes PEFT/Transformers/safetensors. Phase 11 does not execute LLaMA-Factory. Phase 12.1A fixes a model-free static adapter contract using PEFT `0.18.1`, Transformers `4.55.0`, safetensors `0.7.0`, and bounded metadata only; Phase 12.1B invokes that contract in an isolated child and Phase 12.1C reuses it in a separate registry child. Phase 12.1E-A provides bounded reconciliation, E-B provides a separate purge authority, and E-C provides a metadata-only upstream-retention release; Phase 12.4 alone routes approved generation and never changes governance state.

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
umask 077
cp .env.example .env
chmod 600 .env
```

`scripts/compose.sh` rejects a symlink, non-regular, foreign-owned, or
group/other-readable `.env`. `./scripts/compose.sh config` first validates the
resolved Compose graph silently, then displays only a non-interpolated safe
configuration; `config --environment` is refused because it can disclose
interpolation values. Secret values are never a normal wrapper output.

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

Compose passes `DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS` only to the API. It adds no service, secret, mount, or automatic job. Expired feedback is hidden using one PostgreSQL statement-time visibility snapshot before physical deletion. An active same-department system or department administrator must invoke `python -m app.admin purge-rag-feedback` explicitly. That command loads only `DATABASE_URL`; it neither requires nor inspects `DEPTSLM_DATA_DIR` or any runtime mount. See [feedback-retention.md](feedback-retention.md). Local Compose scheduling, PostgreSQL storage, backups, and audit retention are not a production privacy or retention guarantee.

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
- Phase 12.2 review completion and Phase 12.3/12.4 adapter approval,
  deployment, and rollback

No Phase 0 file should be interpreted as a production security or availability guarantee.

## Troubleshooting

- **The storage script cannot find Google Drive:** confirm Google Drive for desktop is installed, signed in, and mounted under `~/Library/CloudStorage`. Do not create a runtime folder in the repo as a workaround.
- **`DEPTSLM_DATA_DIR` contains spaces:** keep the full absolute value in `.env`; scripts and Compose mounts must quote it correctly.
- **Compose rejects the storage path:** set `DEPTSLM_DATA_DIR` to the external absolute path printed by the setup script, then rerun `./scripts/compose.sh config`. Never bypass the wrapper with a repository-local path.
- **A worker exits without work:** `--once` intentionally succeeds on an empty queue. Use the matching `--poll` command for extraction or indexing.
- **The indexer reports model unavailable:** run the explicit pinned preparation command. Never copy model weights into Git or enable network fallback.
- **Qdrant schema mismatch:** verify the fixed collection and payload indexes. Bootstrap never deletes/recreates a mismatch; repair requires a separately reviewed operational decision.
- **The evaluator exits at startup:** provide a non-zero `DEPTSLM_EVALUATION_WORKER_ID`, an exact lowercase 40-character `DEPTSLM_EVALUATION_CODE_REVISION`, the existing Phase 7 Qdrant/runtime settings, and mounted `extracted_text` read-only plus `eval_results` read-write. Do not mount model cache or provide Hugging Face tokens.

The evaluator image is non-root, read-only, capability-dropped, has no host port, and contains no Torch, Transformers, sentence-transformers, model weights, or automatic model preparation. It mounts only `extracted_text` read-only and `eval_results` read-write; the API, RAG runtime, extraction worker, and indexing worker do not mount `eval_results`. Local Compose, deterministic fake-runtime tests, and fixed-seed runs are development evidence only; they do not establish production evaluation validity, determinism, privacy, availability, or performance.

Artifact recovery is an explicit administrator operation, not a scheduler:

```text
python -m app.evaluation_admin reconcile-artifacts --department-id <UUID> --actor-issuer <issuer> --actor-subject <subject> --limit <1-1000> [--apply]
```

It is dry-run by default and requires an active same-department system or department administrator. The command reports only IDs, statuses, timestamps, and staging/final presence; it never prints suite content, hashes, or paths. A durable batch lets a later authorized command resume after a crash, but it cannot delete backups or historical audit rows.

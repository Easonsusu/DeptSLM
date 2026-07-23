# Contributing to DeptSLM

DeptSLM is developed in small, reviewable phases. Contributors must preserve the repository's storage, department-isolation, and untrusted-content boundaries.

## Before starting

1. Read `AGENTS.md`, `docs/roadmap.md`, and the relevant design documents.
2. Confirm the issue or task belongs to the current phase.
3. Create a focused branch from current `main`.
4. Keep planned capabilities separate from implemented behavior.

Phase 5 extraction behavior remains in its PostgreSQL queue and constrained parser boundary. Phase 6 Qdrant behavior must use only the reviewed adapter, fixed collection/model contracts, typed `DepartmentScope`, exact-attempt cleanup, and offline external model cache. Phase 7 may add only one-turn grounded answers through the reviewed retrieval authority and isolated model runtime. Do not add public vector search, conversations, history, streaming, reranking, adapters, LlamaIndex, OCR, download, or training behavior.

## Development setup

Runtime artifacts must remain outside the checkout. On macOS, run:

```bash
./scripts/setup_google_drive_storage.sh
cp .env.example .env
```

Set `DEPTSLM_DATA_DIR` in the untracked `.env` file to the path printed by the script. Tests and CI must use fresh temporary directories instead of Google Drive.

## Required validation

Database schema changes require PostgreSQL 16 and Alembic validation. From `apps/api`, set `DATABASE_TEST_URL` to an isolated `postgresql+psycopg://` database, then run:

```bash
python -m alembic upgrade head
python -m pytest -m postgres
```

Never run migration-cycle tests against shared or production data. Non-database tests remain runnable with `python -m pytest -m "not postgres and not qdrant"`. Qdrant integration tests require a disposable Qdrant 1.13.4 service plus `DEPTSLM_TEST_QDRANT_URL`, `DEPTSLM_TEST_QDRANT_API_KEY`, `DEPTSLM_TEST_QDRANT_ISOLATED=1`, and `DEPTSLM_REQUIRE_QDRANT_TESTS=1`; CI must not silently skip either PostgreSQL or Qdrant tests.

For the Compose-managed database, use the image-contained migration path:

```bash
./scripts/compose.sh run --rm api python -m alembic upgrade head
```

The `postgres` hostname works inside Compose only. Host-shell migration tests require a `DATABASE_URL` using `localhost`. Security-sensitive mutation tests must prove transaction-time authorization, effective-administrator protection, and concurrent admin changes against PostgreSQL 16.

Run the smallest relevant checks. The current complete validation set is:

```bash
python -m ruff check apps/api/app apps/api/tests
python -m ruff check services/rag-worker/deptslm_worker services/rag-runtime/deptslm_runtime
python -m ruff format --check apps/api/app apps/api/tests services/rag-worker/deptslm_worker services/rag-runtime/deptslm_runtime
python -m pytest apps/api
pnpm --filter @deptslm/web test
pnpm --filter @deptslm/web typecheck
pnpm --filter @deptslm/web build
bash -n scripts/setup_google_drive_storage.sh scripts/validate_data_dir.sh scripts/compose.sh
sh -n services/rag-worker/entrypoint.sh services/training-worker/entrypoint.sh
```

Use the repository's configured Python environment and Node/pnpm runtime when global tools are unavailable.

## Data and storage safety

Never commit or force-add:

- `.env`, credentials, or tokens
- uploaded or extracted documents
- vector snapshots or generated training datasets
- adapters, model weights, or model caches
- logs, evaluation results, or exports
- real university, department, staff, faculty, or student data

All future department-owned artifacts must use safe paths beneath `DEPTSLM_DATA_DIR` and include a validated `department_id` segment.

Document upload changes must keep raw bodies incremental, avoid multipart and process-temporary storage, revalidate authorization after streaming, and test cleanup for denial, cancellation, storage, and database failures. Tests must create a fresh temporary `uploads` directory; they must never use a developer's Google Drive folder.

Extraction changes must keep parsing out of API handlers, use the installed secret-free parser subprocess, reverify source bytes, preserve claim/lease ownership, and publish only beneath a fresh temporary `extracted_text` root in tests. Metadata APIs must never expose extracted or chunk text, hashes, paths, claim tokens, or worker identity.

Indexing changes must revalidate the exact final Phase 5 allowlist incrementally, keep embedding/model dependencies out of the API and extraction image, use deterministic fake embeddings only in exact test environments, and keep all Qdrant calls department-filtered. Tests use temporary `extracted_text` and `model_cache` directories and an isolated collection; never download the real model in CI.

Grounded-answer changes must keep the API as the only PostgreSQL/Qdrant authority, pass only bounded questions and selected evidence to the internal runtime, validate every model citation against server-issued labels, and persist no question, answer, prompt, evidence text, vector, hash, or path. The real two-model smoke test is explicit and opt-in only.

Structured-feedback changes must remain PostgreSQL-only and content-free. Accept reviewed enums and exact same-run citation labels only; never add comments, notes, question/answer fields, browser persistence, Qdrant/runtime/artifact access, or automatic effects on RAG, evaluation, datasets, or training. Tests must cover all-role owner submission, tenant denial, immutable idempotency, reviewer transitions and version conflicts, PostgreSQL-server-time expiry, explicit authorized purge, safe audits, and frontend free-text/persistence prohibitions.

Evaluation changes must reuse the exact Phase 7 retrieval, prompt, runtime, answer, citation, and final-source authority implementation. Suite content stays in `DEPTSLM_DATA_DIR/eval_results`; PostgreSQL and result artifacts remain content-free. Never add evaluator-only search, prompts, model parameters, feedback imports, LLM judges, public case/artifact endpoints, or automatic production/training effects. Tests must cover suite parsing and source staleness, same-department evaluator roles, cursor binding, claims and cancellation, deterministic metrics and Decimal gates, exact publication, non-persistence, and evaluator image isolation.

## Department and authentication safety

- Derive allowed departments from authenticated membership; do not trust a client-supplied `department_id` by itself.
- Require explicit department scope at every API, query, job, cache, path, log, retrieval, training, and export boundary.
- Missing or ambiguous scope must fail closed.
- Treat uploaded, extracted, and retrieved text as untrusted content.

## Pull requests

Use the pull request template and include:

- motivation and summary
- important files changed
- exact validation commands and results
- storage-policy impact
- department-isolation and security impact
- known limitations and deferred work

Inspect staged changes before committing. Keep each PR limited to one phase or coherent concern, and request review for authorization, persistence, retrieval-safety, dependency, or deployment changes.

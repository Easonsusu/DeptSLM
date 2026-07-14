# Contributing to DeptSLM

DeptSLM is developed in small, reviewable phases. Contributors must preserve the repository's storage, department-isolation, and untrusted-content boundaries.

## Before starting

1. Read `AGENTS.md`, `docs/roadmap.md`, and the relevant design documents.
2. Confirm the issue or task belongs to the current phase.
3. Create a focused branch from current `main`.
4. Keep planned capabilities separate from implemented behavior.

Do not add RAG, authentication, database models, uploads, or training behavior to a planning-only phase.

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

Never run migration-cycle tests against shared or production data. Non-database tests remain runnable with `python -m pytest -m "not postgres"`.

Run the smallest relevant checks. The complete Phase 1 validation set is:

```bash
python -m ruff check apps/api/app apps/api/tests
python -m ruff format --check apps/api/app apps/api/tests
python -m pytest apps/api
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

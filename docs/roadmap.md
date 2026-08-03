# DeptSLM Roadmap

This roadmap separates foundational safety work from product implementation. A later phase may begin only after the prior phase's boundaries, tests, and limitations are documented.

## Phase 0 — Project initialization (completed)

- Establish the monorepo, web and API skeletons, worker placeholders, documentation, and local Compose structure.
- Keep runtime artifacts outside Git through `DEPTSLM_DATA_DIR` and Google Drive setup.
- Define future department isolation, untrusted-document handling, and insufficient-information rules.

## Phase 1 — CI and workflow foundation (completed)

- Verify and harden automated backend, worker, storage, lint, format, frontend, and artifact-policy checks.
- Add contribution guidance, pull request and issue templates, and the project roadmap.
- Define department and authentication boundaries without implementing product behavior.

## Phase 2 — Department and authentication boundary implementation (completed)

- Implement the reviewed authentication context and fail-closed department authorization primitives.
- Add cross-department denial tests before exposing department-owned resources.

## Phase 3 — Department CRUD and membership model (completed)

- Add PostgreSQL department lifecycle, membership management, scoped authorization, migrations, and transactional audit events.

## Phase 4 — Document metadata and upload flow (completed)

- Add department-scoped document metadata and a validated, incrementally streamed upload workflow using external storage.
- Require transaction-time authorization revalidation, serialized quota enforcement, safe audit evidence, and compensating cleanup.
- Keep extraction, OCR, chunking, indexing, download, malware scanning, and production storage deferred.

## Phase 5 — Document extraction and chunking (completed)

- Add a PostgreSQL queue, constrained parser subprocess, source verification, deterministic normalization/chunking, provenance, leases, quota enforcement, and safe failure handling.
- Phase 5 merged its reviewed extraction, chunking, provenance, and lease boundary.

## Phase 6 — Qdrant indexing and retrieval (completed)

- Add pinned offline Qwen3-Embedding integration, PostgreSQL indexing jobs, staged Qdrant publication, exact-attempt cleanup, and mandatory typed `department_id` payload filters.
- Phase 6 merged its reviewed indexing, collection-schema, lease, cleanup, and future-retrieval authority boundaries.

## Phase 7 — RAG chat with citations (completed)

- Add source-grounded Qwen3 answers, citation metadata, prompt-injection defenses, and insufficient-information behavior.
- Phase 7 merged its reviewed one-turn grounded-answer, citation, prompt-injection, and insufficient-information boundaries.

## Phase 8 — Structured RAG feedback review (completed)

- Add immutable department-scoped structured feedback, constrained review transitions, PostgreSQL-server-time retention, explicit purge, and transactional audit metadata.
- Phase 8 merged its reviewed feedback isolation, retention, purge, and review boundaries.

## Phase 9 — Evaluation runner (completed)

- Phase 9 merged its reviewed immutable external evaluation-suite boundary, exact production-policy reuse, deterministic retrieval and answer metrics, explicit Decimal quality gates, and content-free result publication.
- It also merged complete ground-truth final authority, supervised worker and publication lifecycle controls, descriptor-bound artifact integrity, crash recovery, and bounded reconciliation. It adds no LLM judge, feedback-derived data, production changes, training data, or Phase 10 behavior.

## Phase 10 — SFT dataset builder (completed)

- Phase 10 merged reviewed, traceable, department-scoped supervised fine-tuning dataset generation from immutable human-authored external source bundles. It has no training, adapter, or model execution.

## Phase 11 — LLaMA-Factory training job generation (completed)

- Phase 11 merged its reviewed, immutable department-scoped LoRA and QLoRA job-bundle boundary from one approved Phase 10 dataset. It does not invoke LlamaFactory, train a model, create adapters, or place datasets, logs, or weights in Git.

## Phase 12 — LoRA adapter registry (under review)

- Phase 12.0 (completed) defines the adapter-registry threat model, immutable administrator-controlled source boundary, closed artifact and metadata contracts, governance-lineage versus training-provenance limits, and future lifecycle, evaluation, promotion, rollback, reconciliation, and purge design.
- Phase 12.1 (under review): immutable external adapter intake and metadata-only registry foundations; no runtime loading.
- Phase 12.1A (completed): pure standard-library static compatibility validation for the pinned Qwen3-0.6B LoRA/QLoRA configuration, tensor-key and shape contract, and bounded safetensors metadata.
- Phase 12.1B (completed): administrator-only,
  dry-run-by-default source intake accepts exactly `adapter_config.json` and
  `adapter_model.safetensors`, validates them through the Phase 12.1A child,
  streams them into private immutable `adapters/imports` storage, and commits
  only content-free source/attempt authority metadata. It creates no registry
  record, Phase 11 binding, evaluation, approval, promotion, runtime loading,
  reconciliation, or purge.
- Phase 12.1C (completed): binds one exact
  same-department committed Phase 12.1B source to one approved succeeded
  Phase 11 job and its captured Phase 10 authority, then publishes an
  immutable, content-free registry bundle through a leased, descriptor-bound
  worker. It preserves declared external training association while leaving
  training provenance unverified. It does not evaluate, approve, promote,
  load, route, reconcile, purge, or execute training.
- Phase 12.1D (completed): adds only department-scoped,
  metadata-only registry list/detail reads backed by PostgreSQL authority.
  The closed projection exposes lineage, contract, verification, lifecycle,
  and retention metadata; it exposes no artifact bytes, paths, hashes, tensor
  data, identities, or runtime settings, and it performs no mutation or audit.
- Phase 12.1E-A (current; under review): adds a bounded, administrator-only,
  dry-run-by-default reconciliation foundation for source stages, failed or
  abandoned source finals, terminal registry stages, and failed or validation-
  failed registry finals. It separates descriptor-bound inspection from rename,
  persists verified identity and pre-move intent before no-replace tombstone
  binding, refuses unbound tombstone adoption, verifies each committed unlink,
  and confirms cleanup only after every applicable surface is absent across all
  operations. The limit selects complete attempt groups; it never changes purge
  state or releases dependencies.
- Phase 12.1E-B/C (not started): adapter purge and later lifecycle hardening.
- Phase 12.2 (not started): adapter-target evaluation, baseline/candidate evidence, and fixed numeric quality and safety gates; no automatic promotion.
- Phase 12.3 (not started): review, approval, promotion, supersession, rollback, and deployment event history; no silent fallback.
- Phase 12.4 (not started): department-bound runtime routing, immutable request snapshots, fail-closed loading, and explicit rollback-to-base.

## Phase 13 — Security hardening, Docker demo, and final documentation (not started)

- Complete threat modeling, abuse and isolation tests, operational safeguards, a reviewed local Docker demonstration, and final setup and recovery documentation.

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

## Phase 12 — LoRA adapter registry (completed)

- Phase 12.0 (completed) defines the adapter-registry threat model, immutable administrator-controlled source boundary, closed artifact and metadata contracts, governance-lineage versus training-provenance limits, and future lifecycle, evaluation, promotion, rollback, reconciliation, and purge design.
- Phase 12.1 (completed): immutable external adapter intake, registry publication, metadata-only reads, reconciliation, purge, and lifecycle-release foundations; no runtime loading.
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
- Phase 12.1E-A (completed): adds a bounded, administrator-only,
  dry-run-by-default reconciliation foundation for source stages, failed or
  abandoned source finals, terminal registry stages, and failed or validation-
  failed registry finals. It separates descriptor-bound inspection from rename,
  persists verified identity and pre-move intent before no-replace tombstone
  binding, refuses unbound tombstone adoption, verifies each committed unlink,
  and confirms cleanup only after every applicable surface is absent across all
  operations. The limit selects complete attempt groups; it never changes purge
  state or releases dependencies.
- Phase 12.1E-B (completed): adds a separate bounded,
  administrator-only, dry-run-by-default purge authority for one exact
  validated adapter. PostgreSQL independently reserves the source and
  registry attempts, versions, manifests, identities, and attempt numbers
  before any filesystem mutation. Registry bytes are purged first; source
  bytes are eligible only after an independently verified registry purge.
  Final artifacts move through the private `.purge-deleting` namespace with
  descriptor-relative no-follow checks, same-filesystem no-replace rename,
  fsync, exact tombstone identity, per-file in-flight progress, and
  crash-resumable descriptor unlink/rmdir. Purge records only content-free
  metadata and one success audit; it never deletes Phase 10/11 artifacts,
  history, backups, or audit rows. There is no API route, runtime loading,
  deployment change, evaluation, or training behavior.
- Phase 12.1E-C (completed): adds only a separate,
  administrator-only, dry-run-by-default metadata lifecycle release for one
  exact active upstream dependency after independently revalidating an exact
  completed Phase 12.1E-B purge. It proves the two final paths remain absent
  and both exact `.purge-deleting` namespaces are empty through read-only,
  descriptor-relative inspection, then releases only that dependency in one
  short revalidated PostgreSQL transaction with one success audit. It does not
  delete, recreate, move, inspect contents of, or otherwise mutate artifacts,
  Phase 10/11 data, lineage, review history, or purge history; it adds no API,
  evaluation, approval, promotion, loading, routing, or training behavior.
- Phase 12.2 (completed): adds a separate, department-scoped,
  administrator-only paired evaluation of one exact validated adapter against
  the exact Phase 7 baseline. Each case shares one Phase 9-authorized retrieval
  context, production prompt contract, and deterministic seed across both
  lanes. The candidate runtime is isolated and offline-capable; only numeric,
  content-free evidence and fixed Decimal metrics/gates are published beneath
  external `eval_results`. Queue leases, cancellation, reclaim, final
  PostgreSQL authority, and the Phase 12.1E-B purge fence remain mandatory.
  Evaluation does not approve, promote, route, or change production behavior.
- Phase 12.3 (completed): adds separate review and approval authorities,
  department deployment metadata, explicit promotion and rollback operations,
  rollback-retention references and release, immutable deployment event history,
  and exact evaluation/registry/artifact authority fencing. It does not overload
  `Adapter.status`, load or route adapters, or approve or promote automatically.
- Phase 12.4 (completed): captures an immutable content-free
  deployment target at RAG admission, preserves every Phase 7 retrieval and
  final-authority contract, and routes generation for adapter targets only to a
  separate private production runtime. It uses exact Phase 12.3 authority,
  descriptor-verified registry copies, target fingerprints, killable single-
  target children, no silent base fallback, and a running-request E-B purge
  fence. It does not change deployment governance.

## Phase 13 — Security hardening, Docker demo, and final documentation (final scope)

- Complete threat modeling, abuse and isolation tests, operational safeguards, a reviewed local Docker demonstration, and final setup and recovery documentation.
- No Phase 14 is defined by the current roadmap.

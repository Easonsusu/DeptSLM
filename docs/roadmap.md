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

## Phase 5 — Document extraction and chunking (current; not completed)

- Add a PostgreSQL queue, constrained parser subprocess, source verification, deterministic normalization/chunking, provenance, leases, quota enforcement, and safe failure handling.
- Phase 5 remains current until this pull request is reviewed and merged.

## Phase 6 — Qdrant indexing and retrieval

- Add Qwen3-Embedding integration and mandatory `department_id` payload filters for all vector operations.

## Phase 7 — RAG chat with citations

- Add source-grounded Qwen3 answers, citation metadata, prompt-injection defenses, and insufficient-information behavior.

## Phase 8 — Feedback collection

- Add department-scoped feedback, review status, retention, and audit metadata.

## Phase 9 — Evaluation runner

- Add reproducible retrieval and answer evaluations with external result storage and explicit quality gates.

## Phase 10 — SFT dataset builder

- Add reviewed, traceable, department-scoped supervised fine-tuning dataset generation.

## Phase 11 — LLaMA-Factory training job generation

- Generate controlled LoRA or QLoRA job configurations without placing datasets, logs, or weights in Git.

## Phase 12 — LoRA adapter registry

- Add department-bound adapter metadata, evaluation state, approval, promotion, and rollback.

## Phase 13 — Security hardening, Docker demo, and final documentation

- Complete threat modeling, abuse and isolation tests, operational safeguards, a reviewed local Docker demonstration, and final setup and recovery documentation.

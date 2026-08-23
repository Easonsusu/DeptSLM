# DeptSLM Product Specification

## Document status

This document describes the product direction and separates completed Phase 0–12.4 boundaries from the remaining hardening scope. Phase 12.0 through Phase 12.4 are completed. Phase 12.4 records an immutable content-free target snapshot for each public grounded-answer request and routes only the generation lane through a separate private runtime; retrieval and governance remain unchanged. Phase 13 is the final security, Docker-demo, and documentation hardening scope represented by this revision. Unless a capability is explicitly labeled as completed, it is not implemented.

## Product summary

DeptSLM is a university departmental small language model customization platform. It is intended to give each department a governed workspace for turning its own approved documents into a source-grounded assistant and, when useful, training a department-specific LoRA or QLoRA adapter for Qwen3.

The platform combines retrieval with optional model adaptation:

- Qwen3 is the target base SLM.
- Qwen3-Embedding is the target embedding model.
- LlamaIndex will coordinate document ingestion and RAG query workflows.
- Qdrant will provide department-filtered vector search.
- LLaMA-Factory will run future LoRA and QLoRA training workflows.

The central product promise is not simply “chat with files.” It is a department-isolated, reviewable workflow in which answers are grounded in approved sources and the assistant admits when those sources are insufficient.

## Target users

- **Department administrators** configure a department workspace, manage access, approve content, and oversee retention.
- **Faculty and staff** upload approved reference material, ask operational or academic questions, and review cited answers.
- **Knowledge managers or librarians** curate source collections, metadata, versions, and access rules.
- **AI/IT operators** manage models, ingestion, evaluation, training jobs, adapters, infrastructure, and audits.
- **Evaluators and reviewers** define test sets, compare versions, and approve changes before wider release.

Students may become an end-user group in a future deployment, but only after a department defines appropriate content, privacy, and access controls.

## Product principles

1. **Department isolation:** every department-owned record and operation is scoped by `department_id`; missing scope fails closed.
2. **Source grounding:** answers should cite the department sources used to produce them.
3. **Honest uncertainty:** when no usable source is retrieved, the assistant says it does not have enough information.
4. **Untrusted retrieval:** retrieved documents are data, not instructions, and cannot override system policy.
5. **External runtime storage:** source code belongs in GitHub; runtime artifacts belong under `DEPTSLM_DATA_DIR`, outside the repository.
6. **Human control:** content publication, adapter promotion, and other consequential changes should be reviewable and reversible.
7. **Incremental delivery:** RAG and training will be added after the underlying boundaries, contracts, and tests are designed.

## Core planned capabilities

### Department workspaces and access

- Create and administer isolated department workspaces.
- Assign role-based access to department resources.
- Audit important document, retrieval, training, and deployment actions.
- Prevent cross-department reads, writes, retrieval, training, and exports.

### Knowledge ingestion

- Upload supported documents to a department workspace.
- Extract, normalize, chunk, and enrich text with source metadata.
- Review ingestion status and errors.
- Embed chunks with Qwen3-Embedding and index them in Qdrant with mandatory `department_id` filters.
- Reprocess or retire a source without affecting another department.

### Source-grounded assistant

- Accept a question within an authorized department context.
- Retrieve only sources from that department.
- Give an answer with source references when evidence is sufficient.
- Treat retrieved text as untrusted and resist instructions embedded in documents.
- Return a clear insufficient-information response when evidence is absent or inadequate.

### Fine-tuning and adapters

- Build reviewed department-specific training datasets.
- Phase 11 generates immutable LoRA or QLoRA LlamaFactory job bundles; it does not execute training or create adapters.
- Phase 12.1B stores externally produced adapters outside the repository, and Phase 12.1C records a department-scoped, verified governance association to one exact Phase 10 dataset and Phase 11 job plus the reviewed base-model and closed artifact contracts. Phase 12.2 evaluates one exact adapter; Phase 12.3 records explicit human review and deployment decisions. Phase 12.4 routes an approved deployment only after exact admission authority, with no silent fallback, no public selector, and no change to retrieval.
- Evaluation and explicit review approval are required before promotion or runtime use. Adaptation remains optional and must demonstrate value beyond the approved base/RAG behavior.

### Phase 12 governance boundary

Phase 12.0 defines the external adapter threat model, immutable artifact and
governance-lineage contracts, metadata-only registry, evaluation evidence,
review, promotion, rollback, and fail-closed runtime routing. Phase
12.1C adds only the internal migration and administrator-enqueued worker for
immutable registry publication. Phase 12.1D adds only the two department-scoped
metadata GETs, Phase 12.2 adds paired evaluation, Phase 12.3 adds separate
review/deployment authorities, and Phase 12.4 adds a department-bound immutable
generation route. An imported
adapter is not approved, an approved adapter is not promoted, and promotion is
not proof of safety or quality. Cross-department fallback and silent fallback
to the base model are prohibited; rollback-to-base must be explicit. See
[adapter-registry.md](adapter-registry.md) and
[adapter-governance.md](adapter-governance.md).

The first intake is an administrator-controlled CLI, not a browser or public
weight-upload route. It streams only the two payload files into a
server-ID-derived immutable source bundle; DeptSLM generates a closed,
content-free intake manifest and never trusts an external manifest or host path.
Source state is separate from adapter state, and one committed source may be
consumed by only one exact adapter version. A rollback-retention reference may
be removed by a reviewed pre-purge mutation; upstream retention releases only
after the registry and every bound source copy are committed purged. Registry
and source purge remain separate, bounded, descriptor-relative, and
crash-resumable. The registry records verified governance lineage and a
declared external training association, not proven dataset use, trusted
training execution, or certified adapter provenance. Phase 12.1 must use a
reviewed model-free static schema, with package versions and numeric limits
fixed before implementation.

### Evaluation and operations

- Maintain versioned evaluation sets and results.
- Compare retrieval, base-model, prompt, and adapter versions.
- Monitor jobs and operational errors without leaking sensitive document contents into logs.
- Export approved reports and artifacts to the external runtime directory.

## Phase 0 scope

Phase 0 creates the foundation only:

- a Git repository and documented monorepo structure
- a basic Next.js and TypeScript landing page with no business logic
- a FastAPI skeleton with `GET /health` and `GET /version`
- placeholders for PostgreSQL, Qdrant, the RAG worker, and the training worker in Docker Compose
- an environment-variable contract centered on `DEPTSLM_DATA_DIR`
- an idempotent macOS script for creating the required Google Drive runtime folders
- documentation for product intent, architecture, storage, API, and local deployment
- repository-level contribution and safety instructions

Phase 0 does not claim end-to-end service readiness or production behavior.

## MVP scope

The first usable MVP is expected to provide:

- authenticated department membership and basic administrator/member roles
- department creation and configuration
- document upload, extraction, chunking, ingestion status, and deletion
- Qwen3-Embedding integration and department-filtered Qdrant indexing
- LlamaIndex-based retrieval and a Qwen3 answer-generation path
- answers with source metadata and a defined insufficient-information response
- prompt-injection defenses that treat all retrieved content as untrusted
- a basic web interface for document management and department-scoped chat
- job status, audit metadata, and essential error reporting
- evaluation of retrieval and answer grounding on a small, approved test set

Fine-tuning is deliberately not required for the initial RAG MVP. It should be introduced only after the team can measure whether adaptation adds value beyond retrieval and prompt improvements.

## Future complete scope

Subject to security, governance, and evaluation review, the longer-term platform may add:

- self-service department onboarding, quotas, and lifecycle management
- richer roles, SSO, delegated administration, and approval workflows
- connectors to approved university content systems
- OCR, table-aware parsing, multilingual ingestion, and document versioning
- hybrid and reranked search, configurable retrieval policies, and citation verification
- asynchronous, resumable ingestion with re-indexing and retention controls
- dataset authoring, redaction, deduplication, provenance, and approval
- LoRA/QLoRA job scheduling, experiment tracking, adapter registry, promotion, and rollback
- safe routing among base and department adapters
- regression suites, human evaluation, model cards, and cost/latency dashboards
- backup, disaster recovery, observability, rate limiting, and production deployment controls
- policy-aware exports, retention, deletion, and audit reports

These are roadmap candidates, not Phase 0 commitments.

## Explicit non-goals for Phase 0

- Implementing RAG, embeddings, vector indexing, or document parsing
- Downloading or committing Qwen model weights
- Running LLaMA-Factory or producing adapters
- Building a production authentication or authorization system
- Handling real university, personal, confidential, or regulated data
- Treating the placeholder Compose stack as a production deployment

## High-level success criteria

Phase 0 is complete when a new contributor can clone the repository, understand its boundaries, configure external runtime storage, inspect the web/API skeletons, validate the Compose configuration, and see unambiguous rules for future tenant isolation and grounded-answer safety.

Future MVP success should be measured with explicit targets for retrieval quality, grounded-answer quality, citation correctness, insufficient-information accuracy, cross-department isolation, prompt-injection resistance, latency, reliability, and operator effort. Numerical targets require representative data and stakeholder agreement and are therefore not set in Phase 0.

## Roadmap v2 Phase 14.0 status

Roadmap v1 Phase 0–13 is complete. Roadmap v2 begins with a design-only
controlled training execution contract. Phase 11 remains the source of one
approved, immutable five-file LlamaFactory bundle; Phase 12 remains the
explicit intake, evaluation, approval, promotion, rollback, and routing
boundary for externally produced adapters. Phase 14.0 adds no training,
LlamaFactory execution, model download, runtime service, queue, worker, API,
migration, or automatic handoff. A future Phase 14.1/14.2/14.3 implementation
must preserve department authority, offline model preparation, explicit human
governance, and the existing Phase 12.1A static adapter contract.

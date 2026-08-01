# DeptSLM

DeptSLM is a university departmental small language model (SLM) customization platform. It is intended to let each department build an isolated assistant from its own approved documents, retrieval index, evaluation data, and eventually its own LoRA or QLoRA adapter.

> **Phase 9 status:** Completed. The merged department-scoped evaluation runner imports immutable external suites through verified descriptors, reuses the exact Phase 7 production policy in killable leased workers, computes deterministic metrics and explicit Decimal gates, and publishes content-free numeric results only after final all-source authority checks. Phase 8 feedback remains separate from evaluation ground truth.

> **Phase 10 status:** Completed. It added a department-scoped builder for
> human-authored supervised fine-tuning dataset artifacts. Source examples and
> resulting datasets remain private external files; PostgreSQL and public APIs
> expose metadata only. PostgreSQL intentionally stores closed, content-free
> ownership manifests for exact attempt-scoped cleanup, never source content,
> source references, artifact paths, or dataset bytes. It does not train
> models or create adapters. Import first
> completes a short lock-taking authorization transaction, then captures
> authority in a repeatable-read, read-only, lock-free batch scan; final
> publication reauthorizes and repeats the exact selector scan under locks.
> Leased dataset publication gives independent deadlines to staged verification,
> marker transition, rename/durability, post-rename verification, and final
> metadata commit.

> **Phase 11 status:** Completed. It generates reviewed, department-scoped
> LlamaFactory 0.9.5 LoRA or QLoRA job bundles from one approved Phase 10
> dataset. It streams only exact retained descriptor-bound dataset bytes into
> private external bundle storage and publishes content-free metadata. It never
> installs or invokes
> LlamaFactory, loads a tokenizer or model, executes training, writes adapters,
> or implements an adapter registry.

> **Phase 11 hardening:** A job captures a complete, content-free approved
> Phase 10 authority snapshot. Each succeeded job has one validated
> authoritative final bundle owner; purge cleans historical attempt stages
> first, then commits final-deletion authorization before touching that one
> final surface. Each long parent operation has an independent deadline and
> the final transaction renews only before locks. A reservation binds the
> exact owner, closed manifest, and UUID tombstone namespace. After a verified
> no-replace move, cleanup commits the exact tombstone directory, parent, and
> fixed-file identities before unlinking any member. Retries reject substituted
> paths or files; a single purge operation emits at most one success audit.
> Blocked pre-move cleanup leaves the final intact, while post-move interruption
> keeps the reservation active. Review changes are rejected while a reservation
> is active. This is a reviewed boundary, not training execution.

> **Phase 12 status:** Phase 12.0 is completed and Phase 12.1 is under review.
> Phase 12.1A is completed, and this review implements Phase 12.1B's
> administrator-only immutable source intake. It accepts exactly
> `adapter_config.json` and `adapter_model.safetensors`, validates their
> model-free Phase 12.1A contract in an isolated descriptor child, streams the
> bytes into private external storage, and commits only content-free source and
> attempt authority metadata. It does not create an adapter registry record,
> bind a Phase 11 job, evaluate, approve, promote, load, or purge an adapter.
> Phase 12.1C through 12.1E and Phase 12.2 through Phase 12.4 remain
> unimplemented; no runtime routing exists.

The static contract models the completed PEFT 0.18.1 artifact: saved
`inference_mode=true`, `auto_mapping=null`, `peft_version="0.18.1"`, and the
exact safetensors metadata `{"format":"pt"}`. That metadata is closed and
content-free; `__metadata__` is not a tensor, and acceptance does not prove
training provenance.

Source intake is an administrator CLI and dry-run is the default. It accepts
only the two exact external files and generates a closed, content-free
`intake_manifest.json` binding the department, server-generated source and
attempt IDs, safe internal request reference, positive payload sizes, digests,
contract versions, and code revision. An external manifest, archive, directory,
or arbitrary host path is never authority. Static validation is model-free and
the child reads no tensor payload; the parent streams payload bytes only to
hash and copy them. The immutable source bundle is separate from any future
adapter registry and is not approved, evaluated, promoted, runtime-usable, or
bound to a Phase 11 job. Reconciliation and purge are not implemented here.
The validated-byte authority is bound by complete digest passes before and
after child validation, digest-checked descriptor-relative staging, and
post-rename identity verification. Every PostgreSQL transition compares a
complete frozen authority snapshot and both row versions; static contract
errors remain distinct from fixed descriptor/operational errors. Migration
0010 is self-contained and CI validates that head with a private temporary
adapters root; CI downloads no model weights.

Phase 12.1A fixes the compatible package references, closed configuration and
tensor grammar, bounded header/file limits, and model-free validation rules in a
pure standard-library contract. The registry records verified governance
lineage and declared external training association, not proven dataset use or
trusted training execution. See the [Phase 12.1A static adapter contract](docs/adapter-static-contract.md)
and [Phase 12 adapter registry contract](docs/adapter-registry.md).

Each evaluation publication has a server-generated attempt UUID and a positive run attempt number. Final suite/result manifests are closed schemas and removal requires descriptor-verified exact department, resource, ownership UUID, run attempt, code revision, payload digests, and sizes. Staging has a separate private UUID-path ownership contract so a supervised child killed during directory or payload creation can be removed without parsing partial content. The exact descriptor chain is opened once through deletion; a staging marker is housekeeping only, so missing, zero-byte, truncated, or partial marker states remain recoverable. Reconciliation registers a resumable content-free batch before deletion. Unsafe entries terminalize as fixed-code blocked items and cannot starve later valid items; completed batches write one audit only when deletion succeeded. It is dry-run by default, department-scoped, restricted to system or department administrators, and accepts a strict limit of 1 through 1000. PostgreSQL and external storage are not transactionally atomic: an orphaned external result after a crash is never retrieval or evaluation authority. Reconciliation does not delete backups or persistent audit history.

The API manages content-free upload, extraction, and indexing metadata. For an authorized one-turn answer it creates content-free run metadata, retrieves through the fixed department-scoped Qdrant adapter, cross-checks every candidate against PostgreSQL, reads only selected verified chunks, and calls a private model runtime. After generation it reauthorizes every supplied source—including uncited evidence—against exact PostgreSQL and artifact state, while returning and persisting only cited labels. Questions, answers, prompts, retrieved text, and vectors are not persisted. PostgreSQL succeeded state remains retrieval authority.

Phase 8 feedback is immutable structured PostgreSQL metadata attached to the original requester's completed run. Submit and review JSON are stream-bounded to 4,096 and 2,048 bytes before decoding, with exact reviewed identifiers. Identical canonical PUT replay is idempotent; reviewer transitions are versioned and constrained. Feedback reads assemble complete parent/reason/source metadata in one PostgreSQL statement-time snapshot. Expired feedback becomes inaccessible before explicit authorized batch purge, whose narrow settings loader requires only `DATABASE_URL` and no runtime storage mount. Persistent audit rows may outlive purged feedback, backup deletion is not claimed, and local Compose is not a production privacy or retention claim.

Phase 9 evaluation suite questions and accepted answers remain only in immutable department-scoped external suite artifacts. PostgreSQL stores metadata and numeric metrics only; generated answers, prompts, evidence, vectors, and runtime output are not persisted anywhere. The evaluator uses the exact production retrieval, prompt, generation, citation, and final-authority path. Its parent retains a PostgreSQL-server-time lease while bounded children perform suite reading, cases, and final source checks; loss, cancellation, shutdown, or timeout reaps the child and blocks publication. It has no model stack or Hugging Face token and delegates model work to the existing private runtime. Fixed seeds improve repeatability but do not guarantee bit-identical generation across execution environments. No gate changes production automatically.

## Planned stack

- Next.js and TypeScript for the web application
- FastAPI for the HTTP API
- PostgreSQL for application metadata
- Qdrant for vector search
- LlamaIndex for document ingestion and RAG query workflows
- Qwen3 as the target base SLM
- Qwen3-Embedding as the target embedding model
- LLaMA-Factory for LoRA and QLoRA fine-tuning
- Docker Compose for local development

## Repository layout

```text
DeptSLM/
├── apps/
│   ├── api/                  # FastAPI application
│   └── web/                  # Next.js application
├── services/
│   ├── rag-worker/           # Extraction plus isolated Phase 6 indexing paths
│   ├── rag-runtime/          # Private supervised Phase 7 model runtime
│   ├── training-worker/      # Phase 10 dataset builder
│   └── training-job-worker/  # Phase 11 bundle generator only
├── packages/
│   └── shared/               # Future shared contracts and utilities
├── data/
│   ├── eval_sets/            # Small, synthetic, versioned fixtures only
│   └── sample_docs/          # Small, synthetic, versioned fixtures only
├── docs/                     # Product and engineering documentation
├── scripts/                  # Developer setup scripts
├── .env.example
└── docker-compose.yml
```

## Runtime storage is outside this repository

GitHub stores source code and safe, synthetic fixtures only. Uploaded documents, extracted text, vector snapshots, training datasets, adapters, model files, caches, logs, evaluation results, and exports must never be written to or committed in this repository.

Every local runtime component that writes file-based persistent data must use `DEPTSLM_DATA_DIR`. On macOS, this should point to a `DeptSLM` folder in Google Drive. Applications must fail with a clear error when the variable is missing; they must not fall back to a path inside the checkout. Tests and CI must use temporary directories.

The setup script detects both `My Drive` and the localized `我的雲端硬碟` directory used by Traditional Chinese Google Drive installations. It chooses the strongest unambiguous match and stops without writing when multiple locations are equally suitable. Phase 0 Compose service state is also kept beneath this external root in `service_state/`.

See [Storage policy](docs/storage-policy.md) for the complete rules.

## Local setup

Prerequisites for the complete local stack are Git, Docker Desktop with Docker Compose, and a local Google Drive mount on macOS. Running the apps outside containers requires Node.js 20 or newer and Python 3.11 or newer.

1. Clone the repository and enter it:

   ```bash
   git clone https://github.com/Easonsusu/DeptSLM.git
   cd DeptSLM
   ```

2. Create the external runtime directory:

   ```bash
   ./scripts/setup_google_drive_storage.sh
   ```

   The script is safe to run repeatedly. Copy the printed `DEPTSLM_DATA_DIR` value.

3. Create a local environment file and replace the example storage path with the value printed by the script:

   ```bash
   cp .env.example .env
   ```

   Never commit `.env`.

4. Validate and start the local Compose project:

   ```bash
   ./scripts/compose.sh config
   ./scripts/compose.sh run --rm api python -m alembic upgrade head
   ./scripts/compose.sh up --build
   ```

   The wrapper validates the complete external directory layout and sets a guard required by `docker-compose.yml`; invoking `docker compose` directly is intentionally rejected.

5. Check the API skeleton:

   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/version
   ```

   Protected identity checks additionally require the development/test authentication variables documented in [.env.example](.env.example). Compose passes those variables only to the API container; the generated secret remains only in the untracked `.env`. The Compose migration command uses the internal `postgres` hostname. Host-shell Alembic commands must override `DATABASE_URL` with a host-accessible `localhost` URL. HS256 is allowed only with an explicit reviewed local environment and a non-placeholder secret of at least 32 bytes.

   Bootstrap the first local department only after migration:

   ```bash
   ./scripts/compose.sh run --rm api python -m app.admin bootstrap-department \
     --slug computer-science --display-name "Computer Science" \
     --admin-issuer https://local-issuer.invalid --admin-subject opaque-admin
   ```

   The default ports are controlled by `API_PORT` and `WEB_PORT` in `.env`.

Stop the stack with:

```bash
./scripts/compose.sh down
```

Run at most one extraction job or poll continuously with:

```bash
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --once
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --poll
```

Phase 6 requires a long untracked `DEPTSLM_QDRANT_API_KEY`. Prepare the exact pinned model and fixed collection explicitly; normal workers do neither:

```bash
./scripts/compose.sh run --rm model-admin \
  python -m deptslm_worker.model_admin prepare-embedding
./scripts/compose.sh run --rm vector-admin bootstrap
./scripts/compose.sh run --rm indexing-worker \
  python -m deptslm_worker.indexer --once
```

Model assets remain under `DEPTSLM_DATA_DIR/model_cache`. The indexing worker mounts only `extracted_text` and `model_cache` read-only and receives no API authentication secret. See [Vector indexing](docs/vector-indexing.md), [Qdrant boundary](docs/qdrant-boundary.md), and [Embedding model](docs/embedding-model.md).

Phase 7 additionally requires a long untracked `DEPTSLM_RAG_RUNTIME_TOKEN` and the exact generation model. Preparation remains an explicit administrative action; the normal runtime is offline:

```bash
./scripts/compose.sh run --rm model-admin \
  python -m deptslm_worker.model_admin prepare-rag-models
```

The generation contract is `Qwen/Qwen3-0.6B` revision `c1899de289a04d12100db370d81485cdf75e47ca`, non-thinking mode, an exact 40,960-token model context, an 8,192-token operational input cap, and at most 512 new tokens; query embedding is capped at 2,048 tokens. Inputs are tokenized completely and never silently truncated. The internal runtime receives no database or Qdrant credentials and is not published on a host port. Its HTTP process supervises one persistent killable model child with separate startup and operation clocks. Over-token input is recoverable without reload; fatal timeout, cancellation, disconnect, shutdown, protocol, or child failures terminate and reap the process group and permit one bounded background replacement. Readiness is false and requests fail fast during replacement. The child receives neither the runtime bearer token nor other secrets or proxy settings.

Validate or import a reviewed suite and run the evaluator with:

```bash
./scripts/compose.sh run --rm api python -m app.evaluation_admin import-suite \
  --department-id <UUID> --actor-issuer <issuer> --actor-subject <subject> \
  --source-directory <absolute-path>
./scripts/compose.sh run --rm evaluator-worker \
  python -m deptslm_worker.evaluator --once
```

The import command is dry-run unless `--apply` is supplied. Suite inputs are limited to 500 cases and 16 MiB.

## Safety and data isolation

- Future department-owned records, documents, indexes, jobs, adapters, and conversations must be scoped and authorized by `department_id` at every storage and service boundary.
- Retrieved document text is untrusted input. It must be quoted as context and must never be allowed to override system or developer instructions.
- Questions, evidence, generated answers, and citation filenames reject all format controls, combining grapheme joiner, noncharacters, and other unsafe Unicode while preserving variation selectors, ordinary accents, and emoji. A focused lexer accepts only exact ASCII `[S1]` through `[S8]` citations and rejects paired or dangling source-like lookalikes without blocking ordinary bracket prose.
- If retrieval returns no usable source, the assistant must say that it does not have enough information. It must not invent a department-specific answer.
- Secrets, model weights, and runtime artifacts do not belong in Git history.

Contributor rules are in [AGENTS.md](AGENTS.md).

Contribution workflow and validation guidance are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

- [Product specification](docs/product-spec.md)
- [Architecture](docs/architecture.md)
- [Storage policy](docs/storage-policy.md)
- [API](docs/api.md)
- [Deployment](docs/deployment.md)
- [Roadmap](docs/roadmap.md)
- [Department and authentication boundaries](docs/department-auth-boundaries.md)
- [Authentication foundation](docs/authentication-foundation.md)
- [Database model](docs/database-model.md)
- [Department and membership API](docs/department-membership-api.md)
- [Document model](docs/document-model.md)
- [Document upload](docs/document-upload.md)
- [Document extraction](docs/document-extraction.md)
- [Chunk model](docs/chunk-model.md)
- [RAG worker](docs/rag-worker.md)
- [Vector indexing](docs/vector-indexing.md)
- [Qdrant boundary](docs/qdrant-boundary.md)
- [Embedding model](docs/embedding-model.md)
- [Phase 12.1A static adapter contract](docs/adapter-static-contract.md)
- [Phase 12.1B adapter source intake](docs/adapter-source-intake.md)
- [Phase 12 adapter registry contract](docs/adapter-registry.md)
- [Grounded RAG answering](docs/rag-answering.md)
- [Prompt-injection boundary](docs/prompt-injection-boundary.md)
- [Citation model](docs/citation-model.md)
- [Internal RAG runtime](docs/rag-runtime.md)
- [Structured RAG feedback](docs/rag-feedback.md)
- [Feedback review](docs/feedback-review.md)
- [Feedback retention and purge](docs/feedback-retention.md)
- [Evaluation suites](docs/evaluation-suites.md)
- [Evaluation runner](docs/evaluation-runner.md)
- [Evaluation metrics](docs/evaluation-metrics.md)
- [Evaluation quality gates](docs/evaluation-quality-gates.md)
- [Evaluation artifacts](docs/evaluation-artifacts.md)

## Current non-goals

Phase 9 does not implement LLM judging, semantic grading, public raw results, a frontend dashboard, feedback-derived cases, automatic threshold or RAG changes, training datasets, SFT, adapters, model promotion, cross-department benchmarking, production OAuth/OIDC/SSO, or production deployment.

Phase 10 does not derive examples from feedback or evaluation suites, generate examples with a model, establish semantic entailment, guarantee two-person approval, train a model, invoke LLaMA-Factory, or create/promote adapters. Phase 12.0 remains contract and threat-model work. Phase 12.1A is complete, and Phase 12.1B adds only administrator-controlled immutable source intake with no registry, evaluation, approval, promotion, rollback, or runtime routing. Google Drive storage is an external development runtime location, not a production object store or backup.

## License

No license has been selected in Phase 0. Until one is added, normal copyright restrictions apply.

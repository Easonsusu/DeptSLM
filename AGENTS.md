# AGENTS.md

These repository-specific rules apply to Codex and every other automated or human contributor working on DeptSLM. More specific instructions may add constraints, but they must not weaken the data-isolation, storage, or safety rules below.

## 1. Project identity

DeptSLM is a university departmental SLM customization platform. Its purpose is to help departments create assistants from department-approved knowledge and, in future phases, department-specific LoRA or QLoRA adapters. Preserve a small, understandable implementation and do not present planned behavior as implemented.

## 2. Tech stack

Use the planned stack consistently:

- Next.js with TypeScript in `apps/web`
- FastAPI in `apps/api`
- PostgreSQL for application metadata
- Qdrant for vector search
- LlamaIndex for future ingestion and RAG workflows
- Qwen3 and Qwen3-Embedding as target model families
- LLaMA-Factory for future LoRA and QLoRA work
- `services/rag-worker` and `services/training-worker` for long-running jobs
- Docker Compose for local orchestration

Do not add an overlapping framework or heavy dependency without a documented need. Do not implement RAG or fine-tuning before its reviewed phase.

## 3. Storage policy

The Git repository contains source code, configuration templates, documentation, and small synthetic fixtures only. Never commit or place runtime artifacts in the checkout, including:

- uploaded documents or extracted text
- vector database data or snapshots
- generated training datasets
- LoRA or QLoRA adapters
- model weights or model caches
- logs, evaluation outputs, or exported reports
- `.env` files, credentials, tokens, or other secrets

All file-based persistent runtime paths must derive from `DEPTSLM_DATA_DIR`. If it is missing or invalid in a local runtime, fail clearly and immediately. Never silently fall back to the repository, the current working directory, or an implicit relative path. Tests and CI must use a fresh temporary directory and clean it up.

## 4. Google Drive runtime folder requirement

On the user's Mac, `DEPTSLM_DATA_DIR` must identify the external `DeptSLM` folder under the selected Google Drive mount, normally:

```text
~/Library/CloudStorage/GoogleDrive-*/My Drive/DeptSLM
```

The required artifact subdirectories are `uploads`, `extracted_text`, `vector_snapshots`, `training_datasets`, `adapters`, `model_cache`, `eval_results`, `logs`, and `exports`; local Compose service state is kept under `service_state`. Use `scripts/setup_google_drive_storage.sh` to create them idempotently. The script supports localized personal-drive directory names. Do not delete or overwrite existing Drive content. Do not hard-code one user's absolute path in source code or committed configuration.

## 5. GitHub safety rules

- Target repository: `Easonsusu/DeptSLM`; default branch: `main`.
- Keep the repository public unless the environment or user requires private visibility.
- Inspect staged changes before every commit.
- Never commit secrets, `.env`, model weights, runtime artifacts, generated caches, or user/department data.
- Never rewrite shared history, force-push, delete branches, or change repository visibility without explicit approval.
- Do not bypass `.gitignore` with forced adds.
- Prefer small, descriptive commits and avoid unrelated generated files.

## 6. Pull request rules

- Keep each PR focused on one phase or coherent change.
- State the motivation, scope, test evidence, storage impact, and known limitations.
- Call out schema, API, environment-variable, or security-boundary changes explicitly.
- Update the relevant docs when behavior or setup changes.
- Do not claim a planned feature works until it has an implementation and a test.
- Require review for tenant-boundary, retrieval-safety, persistence, authentication, dependency, and deployment changes.
- Keep phase PRs focused; no RAG or fine-tuning implementation before its reviewed phase.
- Phase 3 database changes use SQLAlchemy and Alembic. Do not call `metadata.create_all` at runtime, add unscoped department-owned repository methods, or hard-delete departments, memberships, identities, or audit events.
- Phase 4 document uploads use raw incremental request streaming and canonical external paths. Do not introduce multipart buffering, process-temporary files, client-derived paths, hard deletion, extraction, OCR, indexing, or download behavior in this phase.
- Phase 5 extraction runs only in the RAG worker through the installed constrained parser subprocess. Never parse untrusted documents in API request handlers, use original filenames as paths, or expose extracted/chunk text through metadata APIs.
- Every extraction and chunk query requires `DepartmentScope`; every worker job carries non-null `department_id` and `document_id`. Parser subprocesses receive no secrets, database credentials, client paths, or user environment.
- Extracted files and chunk content stay beneath `DEPTSLM_DATA_DIR/extracted_text` and never enter Git. Qdrant, embeddings, LlamaIndex, models, OCR, and malware-scanning work do not belong in Phase 5.
- Phase 5 parsers receive only a read-only verified claim-scoped source snapshot, fixed output/result descriptors, and separate scratch. Never pass the live canonical source or a publishable directory descriptor. Final output is exactly `normalized.txt`, `chunks.jsonl`, and `manifest.json` moved into a fresh directory.
- Expired extraction leases are non-revivable. Claim-owned mutations require PostgreSQL-server-time proof that the matching worker/token lease is strictly in the future. Reclaim cleanup may remove only the exact previous claim-token scope and must never remove unknown final directories.
- Phase 6 Qdrant operations require a typed `DepartmentScope`; collection names and filters are fixed internally and never client-controlled. Direct Qdrant client calls outside `deptslm_worker.qdrant_adapter` are forbidden.
- Chunk text and vectors never enter PostgreSQL, and chunk text never enters Qdrant payload. Normal workers never download models; model IDs and immutable revisions must be explicitly reviewed and validated from external `model_cache` storage.
- Unpublished points and indexing attempts without succeeded PostgreSQL authority are never trusted. Exact-attempt cleanup must include department, indexing, and vector-attempt filters. Phase 6 exposes no public search, chunk-text, or RAG behavior.
- A Qdrant collection must pass the exact dense-only vector and payload-index contract before any point operation. Never clean, repair, delete, or recreate a mismatched or unknown collection.
- Every claim-owned Qdrant mutation requires current PostgreSQL-server-time ownership of the exact scope, worker, claim token, vector attempt, lease, and fixed contract. Exact deletion must verify both published and unpublished zero counts; reclaim repeats prior-attempt cleanup before activation.
- Embedding request writes must be bounded, nonblocking, deadline-controlled, heartbeat-aware, and interruptible by shutdown or claim loss. Never spool request text or vectors to disk.
- Phase 8 feedback is structured metadata only. Never add free-text feedback or reviewer notes without a new reviewed phase, and never store questions, answers, prompts, evidence, excerpts, or model output in feedback tables.
- Feedback cannot alter retrieval, prompts, generation, evaluation, datasets, or training automatically. Feedback and review routes require exact `DepartmentScope` authorization; `system_admin` has no cross-department bypass.
- Feedback source targets must reference exact persisted citations from the same department and run. Expiration and visibility use PostgreSQL server time, and purge must remain explicit, authorized, department-scoped, and bounded.
- Feedback code must not access Qdrant, artifact storage, extracted content, the RAG runtime, model code, or external runtime directories.
- Feedback submit and review bodies must be byte-bounded while streaming before JSON decoding. Purge must load only its PostgreSQL URL, service-level list/purge bounds must not rely on transport validation, and feedback reads must assemble a complete response under one PostgreSQL visibility decision so concurrent expiry or purge cannot expose partial children.
- Phase 9 evaluation must reuse the exact production retrieval, prompt, generation, answer-validation, citation, and final-authority policies. Never add an evaluator-only Qdrant adapter, raw-search bypass, prompt, model parameter set, or citation validator.
- Evaluation questions and accepted answers remain only in immutable department-scoped external suite artifacts. Questions, accepted answers, generated answers, prompts, evidence, vectors, and raw runtime output never enter PostgreSQL; generated answers are not written to external results.
- Evaluation artifacts and operations require exact `DepartmentScope`. Feedback cannot become evaluation data automatically, gates cannot alter production automatically, and no LLM judge may be added without a future reviewed phase.
- Evaluator workers receive only PostgreSQL, Qdrant, internal-runtime, exact evaluation settings, external `extracted_text` read-only, and `eval_results` read-write. They never receive model weights, model-cache mounts, Hugging Face tokens, uploads, training data, adapters, or exports.
- Evaluation suite/result files must be read and verified through descriptor-relative no-follow handles. Hashing and parsing use one descriptor lifetime; reject symlinks, hard links, path replacement, unknown entries, and stale staged/final digests. Final PostgreSQL artifact hashes must come from an exact post-rename verification.
- Evaluation workers retain PostgreSQL-server-time claim ownership while any suite scan, production-policy case, or final authority verification is active. Such operations run in killable child process groups with bounded deadlines and heartbeats. Claim loss, cancellation, shutdown, timeout, or heartbeat database failure must terminate and reap the child, prevent publication, and never create a completion-success audit.
- Phase 10 SFT source examples are immutable human-authored external artifacts only. Never derive them from feedback, evaluation suites, RAG output, model output, or browser storage. PostgreSQL, public APIs, audits, logs, and CLI output must never contain instructions, responses, source chunk IDs, paths, source manifests, or dataset bytes. PostgreSQL may retain only closed, content-free ownership manifests needed for exact attempt-scoped cleanup.
- The Phase 10 dataset builder has PostgreSQL and `training_datasets` access only. It must never contain or receive model, tokenizer, Hugging Face, Qdrant, RAG-runtime, upload, extraction, evaluation-result, adapter, export, cloud-credential, proxy, or application-auth-secret dependencies/configuration/mounts.
- Phase 11 consumes only one exact succeeded, approved, unpurged Phase 10 dataset in the same `DepartmentScope`. It creates immutable external LlamaFactory job bundles but never installs or invokes LlamaFactory, loads a tokenizer/model, executes training, writes logs/checkpoints/adapters, or implements a scheduler or registry.
- The Phase 11 bundle worker receives only PostgreSQL metadata and `training_datasets`; it must not receive model caches, adapters, Qdrant, RAG runtime settings, uploads, extraction data, evaluation data, Hugging Face tokens, proxies, cloud credentials, or application-auth secrets. Its fixed child receives only exact retained manifest/train/validation/provenance and stage descriptors with closed content-free metadata; it must never reopen a source pathname or materialize a complete dataset.
- Phase 11 artifacts are exactly `manifest.json`, `training.yaml`, `dataset_info.json`, `train.jsonl`, and `validation.jsonl`. Descriptor-relative, private, no-follow publication and final PostgreSQL authority remain mandatory. Metadata, API responses, audits, logs, and the browser must never retain dataset records, configuration bytes, paths, hashes, model outputs, or adapter data.
- Phase 11 enqueue captures every reviewed, content-free Phase 10 authority field and both initial and final verification must compare the complete snapshot. Purge records a durable exact-job reservation before filesystem mutation, reauthorizes and transitions it to `deletion_authorized` before deletion, and fences every review/archive mutation while active. A crash-resumable reservation may be retried only for the same exact job, retention anchor, and attempt surfaces.
- Dataset publication requires descriptor-verified private exact artifacts, fixed group-isolated splitting, PostgreSQL-server-time claim ownership, source authority revalidation, and a successful final transaction. Contentful source parsing, selector creation, and construction use only fixed exec children with closed schemas, exact secret-free environments, `close_fds`, bounded framed IPC, and explicit source/stage descriptors; they receive no database socket, configuration object, model, Qdrant, runtime, or unrelated descriptor. The parent must never parse the source bundle. Source import must commit its short lock-taking authorization/identifier check before a repeatable-read, read-only, lock-free authority capture in at-most-512-selector batches; durable registration reauthorizes afterward, and final import validation reauthorizes and uses the same canonical batches with deterministic locks. It may stream only child-created UUID selectors in bounded database batches, write a private content-free provenance-ID mapping through the stage descriptor, and retain an unlinked selector descriptor for final authority revalidation. Every long parent selector, authority, hashing, publication, verification, or cleanup operation requires monotonic deadline, server-time heartbeat, cancellation, shutdown, and claim-loss checkpoints. Staged prepublication verification, marker transition, rename/durability, post-rename verification, final transaction, and stale stage/final cleanup must each receive independent guards; never represent them as one publication deadline. Final hashes are captured outside locks, while retained final directory/file descriptors are identity-rechecked inside the short success transaction. Phase 11 configurations use LlamaFactory `enable_liger_kernel: false`, `neat_packing: false`, and `bnb` only for reviewed QLoRA bitsandbytes settings. A succeeded build is not usable by a later phase unless it is explicitly approved; Phase 10 does not implement training, adapters, or consumption.
- Result staging and publication require a server-generated publication attempt UUID and exact positive run attempt number in a closed, content-free manifest. Each build claim must durably register a metadata-only attempt before filesystem mutation; reclaim preserves every earlier attempt and its manifest ownership for exact cleanup. The final success transaction must never rename or write external artifacts. Final cleanup requires complete descriptor-verified manifests, exact payload digests and sizes, and exact metadata ownership. An authorized final deletion reserves the exact succeeded attempt, closed manifest, and UUID tombstone namespace; it verifies the final before a same-filesystem no-replace move to private `.deleting` storage and fsyncs both parents. It must then commit the exact tombstone directory, parent, and five fixed-file identities before any unlink. Every retry compares those identities, and a missing member is accepted only for the persisted in-flight unlink step; substituted, parked, or same-UID replacement paths remain fenced. A post-move failure remains active and resumable; a pre-move mismatch leaves the final intact. One durable purge operation increments each purged job once and appends at most one success audit only after all active reservations close. Staging cleanup is separate: an exact private UUID path and durable metadata own incomplete content; one no-follow descriptor chain remains open from validation through deletion, requires private current-service-user ownership, and may remove partial payloads plus missing, zero-byte, truncated, or partial marker states only within that exact scope. Markers are not ownership authority. Unsafe staging paths terminalize as fixed-code blocked reconciliation items and must not starve later valid items. Reconciliation item identity and cleanup confirmation include the exact resource attempt; cleanup for one attempt must never finalize a sibling. Reconciliation registers every applicable stage and manifest-proven final surface; cleanup confirmation is withheld until all current surfaces complete, while a future authorized operation records a fresh retry item for a formerly blocked resource. It finalizes one resumable deletion-success audit without deleting backups or audit history.
- Phase 12.0 through Phase 12.4 are complete. Phase 12.1A through Phase 12.1E-C preserve the static adapter contract, immutable source/registry publication, metadata-only reads, reconciliation, purge, and lifecycle-release boundaries. Phase 12.2 adds only a separate administrator-enqueued paired evaluator that reuses the exact Phase 7/9 policy for one validated adapter and publishes content-free numeric evidence; it does not approve, promote, load, or route an adapter. Phase 12.3 adds explicit review, deployment, promotion, rollback, and retention metadata. Phase 12.4 captures one immutable content-free deployment target per public RAG request, preserves Phase 7 query embedding/retrieval/final authority, and routes adapter generation only through a separate private runtime after exact validation. External adapters and evaluation results remain outside Git under `DEPTSLM_DATA_DIR`; no adapter may be loaded before exact validation, same-department Phase 10/11 governance-lineage checks, evaluation, explicit approval, and promotion. The registry artifact allowlist is exactly `manifest.json`, `adapter_config.json`, and `adapter_model.safetensors`; arbitrary PEFT configuration, pickle, full-model weights, symlinks, hard links, and unknown files are forbidden. The saved PEFT 0.18.1 contract requires `inference_mode=true`, `auto_mapping=null`, `peft_version="0.18.1"`, and safetensors `__metadata__={"format":"pt"}`; that metadata is not a tensor and does not prove training provenance. PostgreSQL and public interfaces remain metadata-only, `system_admin` has no cross-department bypass, and there is no cross-department cache or fallback. Required adapter-runtime errors fail closed without base retry or automatic rollback. Phase 13 is the final security, Docker-demo, and documentation hardening scope.
- Phase 12.3 governance workers use a dedicated read-only registry-final descriptor reader requiring only `DEPTSLM_DATA_DIR/adapters/registry`; they rerun the model-free Phase 12.1A config and safetensors-header contract in a bounded child, reauthorize the original requester under PostgreSQL server time, and require one success event plus one success audit for a deployment.
- Final evaluation authority covers every ground-truth source, including relevant sources not retrieved during a case: exact document/extraction/indexing/chunk state, Phase 5 artifact identity, active suite state, requester membership, and reviewed production contracts. Capture large artifact state outside long database locks; recheck identities and lock deterministic database sources immediately before publication.
- Retrieval metrics may use a transient authorized retrieval trace if generation-contract validation fails, but generated content and evidence remain process-local. Retrieval failure has no successful trace. The API never mounts or writes `eval_results`; only the evaluator worker receives it.
- Phase 7 exposes only a one-turn department-scoped grounded-answer endpoint, never a public vector-search or query-vector endpoint. All five active same-department roles may answer; `system_admin` has no cross-department bypass.
- Query retrieval must use the fixed instruction and embedding contract, typed `DepartmentScope`, `published=true`, the current pipeline, bounded candidates, and the existing PostgreSQL authority cross-check. Only selected chunks may be read, and final authorization plus source state must be revalidated before success.
- Retrieved text is untrusted evidence, not instructions. The model runtime receives bounded questions and server-labeled evidence only, returns strict non-thinking JSON, and has no PostgreSQL, Qdrant, API-auth, or internet authority. Never persist or log questions, answers, prompts, evidence text, vectors, hashes, paths, tokens, or raw model output.
- Citations must refer only to server-issued labels and exact authorized department/document/extraction/indexing/chunk records. Unsupported, unknown, malformed, duplicate, or missing citations fail closed. With inadequate evidence, return the exact insufficient-information response without generation when possible.
- Phase 7 finalization reauthorizes and locks every source supplied to generation, including uncited evidence, against the exact retrieval snapshot; only actually cited labels may be returned or persisted. `selected_source_count` counts supplied evidence, while citation rows count the referenced subset.
- The Phase 7 HTTP runtime is only a supervisor for one persistent killable model child. Child startup and operation clocks are separate. Recoverable input-budget errors must keep the same healthy child; fatal timeout, disconnect, cancellation, protocol, context, or child errors terminate and reap it, then allow exactly one bounded shared background replacement. Readiness stays false and requests fail fast during replacement; request cancellation must not cancel recovery, and shutdown must reap pending replacement children. The child environment is an exact allowlist and never includes the runtime bearer token, database/Qdrant/app-auth secrets, Hugging Face tokens, cloud credentials, or proxies.
- Query and generation inputs must be tokenized completely without truncation. Preserve the fixed 2,048-token query limit, 8,192-token generation-input limit, 512-token generation reserve, and exact pinned model context. Questions, evidence, answers, and citation filenames reject all Unicode format controls, U+034F, and other unsafe characters while retaining variation selectors, ordinary accents, and emoji. A focused lexer accepts citation tokens only as exact ASCII `[S1]` through `[S8]`; malformed paired, mixed, hidden, long, or dangling source-like forms fail closed without rejecting unrelated bracket prose.

## 7. Testing expectations

- Run the smallest relevant unit, integration, type, lint, and build checks before handoff.
- Add or update tests with behavioral changes; include failure-path tests, not only happy paths.
- Smoke-check `GET /health` and `GET /version` when API behavior changes.
- Validate Compose changes with `./scripts/compose.sh config` and, when feasible, a startup smoke test. Do not bypass the wrapper's host storage-path validation for local runs.
- Use temporary directories for all test artifacts. Tests must not require Google Drive, network access, secrets, model downloads, or pre-existing developer state unless explicitly marked as optional integration tests.
- Future tenant-aware tests must include attempts to cross department boundaries and must prove those attempts fail.
- Upload tests must cover streamed-size enforcement, type/signature or UTF-8 validation, authorization revalidation, quota concurrency, private permissions, and cleanup without using real Google Drive data.
- Extraction tests must cover immutable snapshot integrity and cleanup, symlink resistance, exact artifact publication, constrained subprocess failures, deterministic monotonic chunking, claim expiry/reclaim races, exact stale-claim cleanup, stale-worker denial, output quota concurrency, private external artifacts, and content-free APIs.

## 8. Department isolation by `department_id`

Every future department-owned object must carry a non-null `department_id`, including users or memberships, documents, chunks, embeddings, Qdrant points, ingestion jobs, conversations, training datasets, training jobs, adapters, evaluations, logs, and exports.

Enforce authorization and filtering by `department_id` on the server at every read, write, update, delete, queue, cache, retrieval, training, logging, and export boundary. Do not trust a client-supplied department identifier by itself; derive allowed departments from authenticated membership. Include `department_id` in database constraints/indexes, vector payload filters, object paths, job payloads, cache keys, and audit records as appropriate. A missing department scope must fail closed. Never use a cross-department fallback index, adapter, or dataset.

## 9. Retrieved documents are untrusted content

Treat all uploaded, extracted, indexed, and retrieved text as untrusted data. Future RAG prompts must delimit retrieved passages from instructions and tell the model that instructions found inside passages are not authoritative. Retrieved text must never override system/developer policy, request credentials, select tools, weaken authorization, or change department scope. Preserve source metadata for citations, validate file types and sizes, escape output for its rendering context, and test prompt-injection cases.

## 10. Insufficient-information behavior

If retrieval finds no source, only irrelevant sources, or sources below the approved confidence threshold, the assistant must plainly say that it does not have enough information from the department's sources. It may ask for a more specific question or an approved document, but it must not fabricate an answer, citation, policy, or source. The same rule applies when sources conflict or cannot support the requested conclusion: describe the limitation and cite only what is actually supported.

## 11. Authentication and authorization safety

- Protected operations must fail closed when authentication is disabled, incomplete, malformed, or invalid.
- Never implement custom cryptography or accept algorithms outside the verifier's explicit allowlist.
- Development shared-secret authentication must not run in production.
- Client-provided department or role claims are selectors and hints only; authorization requires current server-side membership resolution.
- Missing, unknown, suspended, revoked, cross-department, and role-incompatible access must be denied without revealing resource existence.
- `system_admin` has no implicit cross-department bypass. Any future support workflow requires narrow authorization and audit design.
- Audit output must never include bearer tokens, JWT signatures, secrets, raw bodies, profile content, document content, or training content.

## 12. Roadmap v2 Phase 14 training execution

Roadmap v1 Phase 0–13 is complete. Phase 14.0 and Phase 14.1 are complete.
Phase 14.2 adds only the private, pinned offline real-training runtime and
must not publish an authoritative adapter or change Phase 10/11/12 behavior.
Its migration, department-scoped control plane, leases, descriptor-bound
snapshots, and closed runtime protocol remain fail-closed; every mutation that
locks both Phase 11 and Phase 14 rows uses `TrainingJob -> Department (when
needed) -> TrainingExecution -> TrainingExecutionAttempt`, followed by
dependent purge rows. Phase 11 remains the immutable five-file bundle and Phase
12 remains the explicit adapter-intake and governance boundary.

The Phase 14.2 worker may use only one exact same-department succeeded,
approved, unpurged Phase 11 job and its complete captured Phase 10 authority
snapshot. It freezes the full content-free authority, creates a private
verified server-owned input snapshot, preserves all reviewed training
semantics, and rejects caller paths, YAML/JSON/config, model IDs, repositories,
flags, environment variables, shell fragments, remote loaders, and arbitrary
callbacks. The runtime uses the prepared local `Qwen/Qwen3-0.6B` revision
`c1899de289a04d12100db370d81485cdf75e47ca`; execution is offline and has no
Hugging Face token. LoRA and QLoRA NF4 support fail closed on unreviewed
hardware; no silent CPU, precision, quantization, or device fallback is
allowed.

The Phase 14.2 control plane receives PostgreSQL and external training-run
storage only. Its private runtime receives no PostgreSQL, Qdrant, API-auth,
membership, RAG, evaluation, adapter, cloud, or Hugging Face credentials; it
has no public port, Docker socket, host networking, or normal internet egress.
Fixed argv, sanitized environment, dedicated process groups,
shutdown/cancellation/claim-loss termination, deadlines, bounded
output/log/disk/process resources, and complete child-tree reaping are
mandatory. Its serialized request is content-free and contains no paths or
file descriptors; descriptors transfer only through authenticated SCM_RIGHTS
capabilities. Filesystem presence and zero exit are never authority. A real
success may retain private non-authoritative candidate output and keeps the
exact Phase 11 authority fenced until Phase 14.3. Phase 14.3 and Phase 15 have
not started.

Phase 12.1C hardening requires exact composite foreign keys for the source,
Phase 11 attempt, and Phase 10 attempt snapshots. An evolving version is
checked for the adapter, registry attempt, every upstream attempt, source, and
retention dependency. The registry parent retains all five Phase 11 descriptors
and verifies hashes, sizes, manifest fields, and current PostgreSQL authority
before and after the child; the child receives only the Phase 11 manifest
descriptor and closed metadata. Claims are reclaimed only through one exact
prior attempt, with terminal claim-loss handling and no registry-stage/final
deletion helper. Phase 10 and Phase 11 final-file deletion reauthorizes the
exact resource and active adapter dependency immediately before filesystem
mutation. PostgreSQL remains the final authority; external filesystem
publication is not transactionally atomic.

- Phase 12.1E-A adapter reconciliation is a separate manual, dry-run-by-default,
  department-scoped maintenance operation. It may inspect and remove only
  `source_stage`, failed/abandoned non-authoritative `source_final`, terminal
  `registry_stage`, and failed/validation-failed `registry_final` surfaces. It
  never changes source or adapter purge state, releases an upstream dependency,
  touches Phase 10/11 artifacts, or exposes a public route. Durable PostgreSQL
  operation/item rows own exact resource/attempt/version snapshots. The
  descriptor-bound store verifies private UID/mode/no-follow identity. It
  persists a closed observed identity and plan before a separate move-intent
  transaction; only then may the exact original be reopened for a no-replace
  `.deleting` tombstone. Tombstone identity is committed before unlink, every
  unlink and parent entry is verified, and progress is crash-resumable. Partial
  markers and payloads
  are never parsed or logged; incomplete markers are recoverable under exact
  metadata/path authority, while complete finals still require closed manifests
  and exact digests. The maintenance Compose profile receives only PostgreSQL
  and the external adapters root; API, model, Qdrant, dataset, and training-job
  mounts are forbidden. PostgreSQL and filesystem operations remain
  non-atomic, and in-flight requests cannot be fenced retroactively.
- Phase 12.1E-B adapter purge is a separate manual, dry-run-by-default,
  department-scoped maintenance operation. It independently reserves the exact
  validated registry and source attempts before filesystem mutation, deletes
  registry final bytes before source final bytes, and uses the private
  `.purge-deleting` namespace with no-follow descriptors, no-replace moves,
  fsync, exact tombstone identities, per-file in-flight progress, and durable
  directory-unlink intent. Initial rename requires an empty exact namespace;
  a canonical private unknown sibling around a durable move intent before or
  after rename is preserved as a retryable operator-resolved conflict, while
  expected-item substitution and unsafe state are terminal. Unbound
  post-rename recovery requires an exact singleton item namespace. Both exact
  namespaces are rechecked empty before `purged` and its success audit. Only
  exact same-department administrators may apply it;
  `system_admin` has no cross-department bypass. It never deletes Phase 10/11
  artifacts, lineage, review/deployment history, audit rows, backups, or other
  retained copies, and it adds no public route, model, Qdrant, RAG, evaluation,
  training, approval, promotion, or runtime loading behavior.
- Phase 12.1E-C adapter lifecycle release is a separate manual,
  dry-run-by-default, department-scoped metadata operation. It may release only
  the one exact active `AdapterUpstreamDependency` of an adapter/source already
  `purged` by one completed unblocked E-B operation with exact source/registry
  reservations, completed items, retained manifests, and success audit. It
  must reject active E-A/E-B mutation authority and reappeared or unsafe final
  storage. It reads only the exact two final paths and exact `.purge-deleting`
  namespaces through private no-follow descriptors; it never parses, hashes,
  moves, recreates, deletes, or otherwise mutates artifacts. The final short
  transaction reauthorizes every row and changes only that dependency to
  `released`, its version, and the adapter version, then appends one release
  audit. It never changes Phase 10/11 artifacts, source/adapter lifecycle,
  E-B history, review/deployment history, or runtime behavior, and it adds no
  public route.

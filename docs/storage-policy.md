# Storage Policy

## Policy summary

DeptSLM keeps source code in GitHub and runtime artifacts outside the repository. In local macOS development, external runtime storage is a `DeptSLM` folder in Google Drive. The required environment variable `DEPTSLM_DATA_DIR` is the only root from which file-based persistent runtime paths may be derived.

The policy is fail-closed: if `DEPTSLM_DATA_DIR` is missing, empty, invalid, or not writable for a component that needs to write artifacts, that component must stop with a clear error. It must not fall back to the repository, the current directory, `/tmp`, a home-directory cache, or any other implicit persistent location. Tests and CI are the exception only in that they explicitly set `DEPTSLM_DATA_DIR` to a newly created temporary directory.

## What belongs in GitHub

- application and service source code
- dependency manifests and lockfiles
- Docker and development configuration templates
- documentation and scripts
- database migrations, once introduced
- small, synthetic, non-sensitive fixtures that are intentionally reviewed and versioned

The repository's `data/sample_docs` and `data/eval_sets` directories are reserved only for small synthetic fixtures. They are not runtime storage and must not contain real university, department, student, employee, research, confidential, licensed, or regulated content.

## Required external directory

On the user's Mac, the expected location is:

```text
~/Library/CloudStorage/GoogleDrive-*/My Drive/DeptSLM
```

Google Drive localizes the personal-drive folder; for example, Traditional Chinese installations use `我的雲端硬碟` instead of `My Drive`. `scripts/setup_google_drive_storage.sh` detects either form, creates the directory idempotently, and prints the exact value to use for `DEPTSLM_DATA_DIR`. It scores multiple candidates, prints the unambiguous selection, and refuses to write when the best candidates are tied.

The required tree is:

```text
DeptSLM/
├── uploads/
├── extracted_text/
├── vector_snapshots/
├── training_datasets/
├── adapters/
├── model_cache/
├── eval_results/
├── logs/
├── exports/
└── service_state/           # Local Compose state, not a portable artifact
    ├── postgres/
    └── qdrant/
```

The setup script may create missing directories, but it must never delete or overwrite existing content.

## Artifact placement

| Artifact | External subdirectory | Notes |
| --- | --- | --- |
| Uploaded source files | `uploads/` | Phase 4 paths are isolated by `department_id` and document UUID. |
| Extracted or normalized text | `extracted_text/` | Phase 5 uses department/document/extraction UUIDs; text and chunk JSONL are sensitive and untrusted. |
| Vector database snapshots | `vector_snapshots/` | Live Qdrant persistence is a separate deployment concern; it must also remain outside the repo. |
| Generated training datasets | `training_datasets/` | Store provenance and department ownership. |
| LoRA and QLoRA adapters | `adapters/` | Never mix or fall back across departments. |
| Downloaded model files and caches | `model_cache/` | Includes weights, tokenizers, and derived caches. |
| Evaluation outputs | `eval_results/` | Synthetic fixtures alone may live in the repo. |
| Runtime logs | `logs/` | Do not log document text, secrets, or unnecessary personal data. |
| Generated reports and bundles | `exports/` | Review access and content before sharing. |
| Local service state | `service_state/` | Compose-only PostgreSQL and Qdrant persistence; not a backup or portable snapshot. |

Phase 4 uses `<root>/uploads/<department_id>/.staging/<upload_id>.part` while streaming and `<root>/uploads/<department_id>/<document_id>/source` after finalization. Filenames are metadata only and never become path components. The `uploads` root must preexist as a real writable directory. Storage uses descriptor-relative no-follow operations, exclusive files, `0700` directories, `0600` sources, and same-filesystem atomic rename. Normal handled failures compensate, while crash-orphan discovery and physical retention remain deferred.

Phase 5 stages beneath `<root>/extracted_text/<department_id>/<document_id>/.staging/<extraction_id>/<claim_token>/` and publishes to a fresh exclusive `<root>/extracted_text/<department_id>/<document_id>/<extraction_id>/`. The claim contains a private verified source snapshot, parent-created outputs, and a separate scratch directory during processing. Before publication, the snapshot and scratch are removed, unexpected entries are rejected, and quota is computed from the exact reviewed final allowlist: `normalized.txt`, `chunks.jsonl`, and `manifest.json`. Only those three files move into the final directory; the claim directory is never renamed as the result. The root must preexist as a real writable non-symlink directory. Publication uses descriptor-relative no-follow operations, exclusive `0600` files, `0700` directories, identity/link checks, and never overwrites a final result. Expired-job recovery removes only the exact prior claim token's staging. The worker mounts uploads read-only and extracted text read-write.

Phase 6 model preparation stages and caches only beneath `model_cache`, then publishes one real-file directory named for the immutable reviewed revision with a complete integrity manifest. Ordinary indexing mounts `model_cache` and `extracted_text` read-only, operates offline, and writes vectors only to Qdrant service state. Qdrant payload is content-free; live state remains beneath external `service_state/qdrant` and portable snapshots beneath `vector_snapshots`. No model, vector, chunk artifact, Hugging Face cache, or Qdrant data may enter the checkout or process temporary storage.

Phase 7 prepares the exact embedding and generation models through the same explicit external `model_cache` boundary. The private runtime mounts only that subdirectory read-only, and the API mounts `extracted_text` read-only while retaining its upload write boundary. Questions, answers, prompts, selected evidence, query vectors, and raw model output remain transient memory only and must not be written to the repository, temporary directories, home caches, logs, exports, or database fields.

Phase 8 adds no file artifact and no runtime directory. Structured feedback, reviewed reason identifiers, exact citation targets, workflow state, expiry, and safe audit metadata remain in PostgreSQL. Feedback tables and logs contain no questions, answers, prompts, evidence, excerpts, comments, filenames, paths, vectors, or model output. Purge removes eligible database rows explicitly and does not touch uploads, extraction artifacts, Qdrant, models, Google Drive content, or any runtime directory.

## Never commit

The following must never be committed, even to a private branch:

- uploaded documents
- extracted, parsed, normalized, or chunked document text
- Qdrant data, vector indexes, or vector snapshots
- generated training datasets
- LoRA or QLoRA adapters
- model weights, including `.safetensors`, `.bin`, `.gguf`, `.pt`, and `.pth`
- model downloads and caches
- runtime logs
- evaluation runs or outputs
- generated exports or reports
- `.env` files, secrets, credentials, API keys, tokens, or certificates
- real department or user data of any kind

`.gitignore` is a safety net, not authorization to write runtime files into the checkout. A path being ignored does not make it an acceptable runtime location. Before committing, inspect both staged and untracked files and remove any artifact from the worktree without adding it to history.

If sensitive content or a secret is committed, stop distribution, notify the repository owner, revoke or rotate affected credentials, and follow an approved history-remediation process. Merely deleting it in a later commit is insufficient.

## Application contract

Any future component that reads or writes artifacts must:

1. Read `DEPTSLM_DATA_DIR` explicitly at startup or at the start of the relevant operation.
2. Resolve and validate the root, with a clear error naming the missing or unusable variable.
3. Build paths only beneath the approved subdirectories.
4. Include and validate `department_id` for department-owned artifacts.
5. Prevent `..`, symlink, or absolute-path escapes from the approved root.
6. Use least-privilege file permissions appropriate to the platform.
7. Avoid writing source contents, secrets, or sensitive identifiers to logs.
8. Define cleanup, retention, and deletion behavior before handling real data.

The Phase 5 parser receives the read-only immutable source-snapshot descriptor, fixed output/result descriptors, and a descriptor-based scratch alias, not the live source, a publishable directory descriptor, external host path, database/auth secret, filename, or user environment. PostgreSQL stores extraction/chunk metadata only; metadata APIs expose no content or paths.

The Phase 6 embedding child receives only a reviewed model directory argument plus bounded sequence/text requests. It receives no database, Qdrant, authentication, department, document, extraction, user, or filename values. PostgreSQL stores indexing metadata but no text/vectors; Qdrant stores vectors with a content-free payload.

The Phase 7 runtime receives only a bounded question and server-labeled selected evidence through an authenticated internal endpoint. It receives no database, Qdrant, API JWT, user identity, department ID, path, filename, or storage descriptor. PostgreSQL stores content-free run/citation metadata only.

The Phase 8 feedback service receives authenticated identity and exact department/run/citation selectors through the API, then queries PostgreSQL only. It does not receive or access artifact descriptors, document bodies, model inputs/outputs, Qdrant settings, RAG runtime settings, or `DEPTSLM_DATA_DIR` paths.

Do not hard-code a developer's absolute Google Drive path in source code, Docker files, tests, or committed environment templates. `.env.example` should contain a placeholder; each developer keeps the real value in an untracked `.env`.

## Docker Compose

Local Compose configuration passes `DEPTSLM_DATA_DIR` explicitly to services that need it and binds only approved external subpaths. Use `scripts/compose.sh` for local Compose commands: it rejects missing, relative, root, nonexistent, non-writable, source-overlapping, and incomplete host paths before Docker runs. The wrapper also sets a required Compose guard, so a direct `docker compose` invocation fails instead of bypassing validation. Bind mounts disable automatic host-path creation.

PostgreSQL and live Qdrant state use `service_state/` bind mounts in the local Compose stack. Before real data is used, review whether synchronized Google Drive storage is suitable, ensure volumes cannot resolve inside the checkout, and document backup, synchronization, corruption, retention, and recovery behavior. Portable Qdrant snapshots intended for retention belong in `vector_snapshots/`.

## Tests and CI

Tests and CI must not depend on Google Drive. Each run should create a fresh temporary directory, set `DEPTSLM_DATA_DIR` to its absolute path, create only the required test subdirectories, and remove it at the end. Test fixtures must be synthetic and non-sensitive. Tests should also verify that:

- startup fails when `DEPTSLM_DATA_DIR` is absent where required;
- paths cannot escape the configured root;
- one department cannot read another department's artifacts;
- no test writes runtime artifacts into the repository.
- interrupted, invalid, unauthorized, over-quota, storage-failed, and database-failed uploads leave no staged source.
- extraction failures, timeouts, claim loss, and shutdown remove the exact source snapshot and scratch staging;
- final extraction directories contain only the three reviewed artifacts, and extra staging bytes cannot evade quota accounting;
- expired claims cannot regain ownership and reclaim cleanup cannot cross claim-token scope.
- model preparation and normal indexing never write model or vector artifacts into the checkout or home cache;
- Qdrant tests use the pinned isolated service and exact department/attempt filters;
- failed, stale, shutdown, and reclaimed indexing attempts clean only their exact vector attempt and never become trusted.
- grounded-answer tests use temporary verified extraction artifacts and fake offline models; no question, answer, prompt, evidence, vector, or raw model output remains in the checkout.
- feedback tests use isolated PostgreSQL state only and verify that no free text, feedback content artifact, Qdrant/runtime access, or browser persistence is introduced.
- evaluation tests create temporary `extracted_text` and `eval_results` roots, use synthetic suites and a fake runtime, and prove that questions, accepted/generated answers, prompts, evidence, vectors, and raw output never enter PostgreSQL or result artifacts.

Phase 9 final suite files are exactly `manifest.json` and `cases.jsonl`; final run files are exactly `manifest.json`, `summary.json`, and `case_results.jsonl`. Suite content remains private and has no public download API. Run artifacts are numeric and content-free. UUID-only department/resource paths, private permissions, no-follow checks, exclusive staging, digest verification, exact-attempt cleanup, and atomic rename are required. External publication and PostgreSQL state are compensating rather than transactionally atomic.

## Google Drive limitations

Google Drive provides convenient local synchronization, not a production object store, database, queue, locking service, or backup policy. Concurrent writers and large model artifacts may create sync conflicts, partial uploads, quota pressure, or slow startup. Production storage will require a separately reviewed design; this policy does not claim that the local Google Drive layout is production ready.

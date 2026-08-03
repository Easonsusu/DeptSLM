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

Phase 9 final suite files are exactly `manifest.json` and `cases.jsonl`; final run files are exactly `manifest.json`, `summary.json`, and `case_results.jsonl`. Suite content remains private and has no public download API. Run artifacts are numeric and content-free. UUID-only department/resource paths, private permissions, no-follow checks, hard-link rejection, exact descriptor identity checks, exclusive staging, digest verification, exact-attempt cleanup, and atomic rename are required. Run ownership includes the positive run attempt number, publication UUID, suite, department, and code revision. Hashing/parsing use one descriptor lifetime; staged files are reverified before rename and final files are rehashed after rename before their PostgreSQL digests are committed. Explicit reconciliation is dry-run by default, has a strict service bound of 1 through 1000 items, records a durable content-free batch before deletion, and may remove only manifest-proven stale staging or final artifacts. It is not backup deletion, retention enforcement, or audit-history deletion. External publication and PostgreSQL state are compensating rather than transactionally atomic.

## Phase 10 SFT datasets

Phase 10 source bundles and final dataset artifacts live only under `DEPTSLM_DATA_DIR/training_datasets`, never in the checkout. Source bundles are exactly `manifest.json` and `examples.jsonl`; final builds are exactly `manifest.json`, `train.jsonl`, `validation.jsonl`, and `provenance.jsonl`. Directories are UUID-derived and private; regular files are private, no-follow, and hard-link resistant. Leased dataset publication retains the exact stage directory and file descriptors across separately bounded staged verification, marker removal, rename/durability, and post-rename digest verification; only the final short metadata transaction follows. No phase reopens a previously authorized path or hashes payload files while final PostgreSQL locks are held. The API never mounts `training_datasets`; only the isolated dataset-builder worker may mount it read-write.

## Phase 11 training-job bundles

Phase 11 writes only private job bundles below `DEPTSLM_DATA_DIR/training_datasets/jobs/<department UUID>/<training job UUID>`. A final bundle contains exactly `manifest.json`, `training.yaml`, `dataset_info.json`, `train.jsonl`, and `validation.jsonl`; staging adds only the private marker. The API never mounts this directory. The isolated bundle worker verifies and copies an approved Phase 10 dataset through retained descriptors; it does not execute the configuration or mount `model_cache`, `adapters`, logs, or model outputs. The final directory is one job-level surface with one validated succeeded-attempt owner; historical attempts own only their exact stage directories. Purge deletes stages first, then may delete that descriptor-verified final only after PostgreSQL commits final-deletion authorization. The authorization reservation binds the owner attempt, closed content-free manifest, and UUID tombstone namespace below private `.deleting/jobs`; the final is fully verified before an atomic no-replace move and both parent directories are fsynced. Before any member is removed, a `tombstone_bound` reservation persists the exact private directory, parent, and fixed-file identities. Retries require each identity to match; only the one durably in-flight unlink may be absent. Tombstone deletion never parses partial bytes, and a substituted, parked, or partial unbound tombstone remains actively fenced. A pre-move failure leaves the final directory intact. Generated job bundles, like all runtime data, are never committed to Git.

## Phase 12 adapter source, registry, and reconciliation (under review)

The proposed private external registry layout is:

```text
adapters/
├── imports/<department UUID>/<source bundle UUID>/
│   ├── intake_manifest.json
│   ├── adapter_config.json
│   └── adapter_model.safetensors
├── registry/<department UUID>/<adapter UUID>/
│   ├── manifest.json
│   ├── adapter_config.json
│   └── adapter_model.safetensors
├── .staging/imports/<department UUID>/<source bundle UUID>/<import attempt UUID>/
├── .staging/registry/<department UUID>/<adapter UUID>/<publication UUID>/
├── .deleting/source_stage/<department UUID>/<source bundle UUID>/<operation item UUID>/
├── .deleting/source_final/<department UUID>/<source bundle UUID>/<operation item UUID>/
├── .deleting/registry_stage/<department UUID>/<adapter UUID>/<operation item UUID>/
└── .deleting/registry_final/<department UUID>/<adapter UUID>/<operation item UUID>/
```

Phase 12.1C now creates the private registry surface through its isolated
worker. The final allowlist is exactly `manifest.json`, `adapter_config.json`, and
`adapter_model.safetensors`. No pickle, `.bin`, `.pt`, `.pth`, GGUF, base-model
weights, tokenizer, optimizer, scheduler, trainer state, checkpoint, log,
script, arbitrary README, cache, temporary file, symlink, hard link, or unknown
entry is permitted. Components derive only from `DEPTSLM_DATA_DIR`; the
`adapters` root must preexist. The implemented Phase 12.1C worker requires UUID-only
server-owned paths, real private `0700` directories, private `0600` files,
current service UID, exclusive creation, no overwrite, same-filesystem atomic
publication, file and directory fsync, post-rename rehash, and retained
descriptor identity through the final PostgreSQL commit. Final file and
directory mtime/ctime nanoseconds are part of that retained authority.
Descriptor-relative no-follow operations must reject path replacement and links;
an incomplete marker is not ownership authority; exact PostgreSQL metadata and
the private UUID stage path are the cleanup boundary.

The source-intake allowlist is exactly `intake_manifest.json`,
`adapter_config.json`, and `adapter_model.safetensors`. An administrator CLI
streams only the two externally supplied payload files into a private UUID-
derived `.staging/imports/<department>/<source bundle>/<import attempt>/`
directory. DeptSLM generates a closed, content-free `intake_manifest.json`
binding department ID, source-bundle ID, import publication-attempt ID, positive
attempt number, safe internal uploader/request references, payload digests and
positive sizes, intake contract version, and code revision. Original host paths,
filenames, bytes, tensor values, arbitrary operator text, external manifests,
and credentials never enter PostgreSQL, APIs, audits, or logs. An external
manifest, README, archive, directory tree, or unknown entry is rejected. The
committed import bundle is immutable and read-only to the registry worker. No
user-supplied host path is reopened from PostgreSQL metadata. Publication
requires an exact marker, retained descriptors, complete allowlist verification,
same-filesystem no-replace rename, parent fsync, post-rename rehash,
entry-to-descriptor binding, file/directory mtime/ctime capture, and a final
PostgreSQL authority commit that repeats those checks without hashing.

Phase 12.1B creates only the private `adapters/imports` and
`.staging/imports` source-intake surfaces through the administrator CLI. Phase
12.1C additionally mounts imports and Phase 11 bundles read-only and registry
staging/final paths read-write for its isolated worker. The CLI is dry-run by
default, accepts exactly the two external files, and streams them through
retained descriptors; it never reopens a persisted host path. The API and
browser have no adapter storage mount and no weight-upload route.

Phase 12.1C updates setup and Compose only to create and mount the private
registry worker surfaces. There is no fallback to the checkout, current directory,
`/tmp`, home, cache, logs, exports, or `model_cache`. Host paths, adapter bytes,
configuration bytes, tensor values, and sensitive content never enter
PostgreSQL, APIs, audits, or logs. PostgreSQL and external storage are
non-atomic; a database commit does not prove external publication or deletion.

Phase 12.1B implements import-source state, separate from adapter state. Phase
12.1C adds `claimed` and `consumed` source transitions, `adapters`, registry
attempts, and an active upstream retention dependency. The registry worker
records declared external training association and verified governance and
artifact compatibility, but `training_provenance_verified` remains false. Failed
attempts remain bound to their exact source; the source and registry are never
runtime, evaluation, promotion, or rollback authority.

Future purge has separate source and registry operations. A rollback-retention
reference may be removed through a reviewed pre-purge mutation; upstream Phase
10/11 retention dependencies release only after no active deployment or
rollback reference remains, all registry bytes and every bound source copy are
confirmed purged through committed authorities, and the adapter state is
`purged`. Registry-only deletion cannot mark an adapter purged while a source
copy remains. A consumed source may be purged before the registry after registry
publication is independently verified and no intake retry/reconciliation needs
the source; the adapter may remain active because the registry is authority.

Source reconciliation and purge are dry-run by default, strictly bounded,
administrator-only, department-scoped, durable before mutation,
descriptor-relative/no-follow, exact-attempt owned, tombstone-bound, and
crash-resumable. They emit one operation-level success audit only after all
items complete. They never delete a source still required by an active intake or
retry, never delete registry artifacts through source cleanup, and never delete
Phase 10/11 artifacts. Metadata, governance lineage, evaluations, reviews,
deployment events, and audit history remain; backup or Google Drive history
deletion is not claimed.

The registry manifest records verified governance lineage, a declared external
training association, and verified artifact compatibility, not proven dataset
use, trusted training execution, or certified adapter provenance. Phase 12.1A
now fixes the model-free static schema: PEFT `0.18.1`, Transformers `4.55.0`,
safetensors format `0.7.0`, the closed configuration/tensor grammar, and
`1,048,576`-byte header and `44,040,192`-byte file bounds. It loads no model or
tokenizer and does not implement evaluation, approval, promotion, runtime
loading, reconciliation, or purge.

A non-purged adapter record creates retention dependencies on the exact Phase 10
dataset artifact and Phase 11 authoritative job bundle. Upstream Phase 10 and
Phase 11 purge must fail closed while those dependencies exist. Adapter purge
removes only private adapter bytes after active-deployment and explicit
rollback-retention fences, then marks artifact state purged while retaining
metadata, lineage, evaluations, reviews, deployment events, and audit history.
Historical metadata references do not retain bytes; active deployment and
explicit rollback-retention references do. No purge claims deletion from
backups or Google Drive history.

Instructions, target responses, source chunk identifiers, paths, and manifests are contentful external data. PostgreSQL keeps no such content and public APIs do not disclose it. Google Drive remains external development storage, not a production object store or backup.

### Phase 12.1C authority hardening

Registry enqueue stores the complete content-free source, Phase 11, and Phase
10 attempt snapshot, including the Phase 11 five-file digests and sizes. The
parent retains descriptor-relative handles for all five Phase 11 files and
rechecks their identities before and after the fixed child; only the manifest
descriptor crosses the child boundary. Every lifecycle transition compares
evolving versions for the adapter, registry attempt, upstream attempts, source,
and active retention dependency. Reclaim marks exactly one prior attempt and
creates a fresh attempt without adopting old surfaces. Phase 10 and Phase 11
final-file deletion reauthorizes the exact resource and active adapter fence
immediately before mutation. Phase 12.1E-A adds a manual `maintenance` profile
for bounded reconciliation of exactly four non-authoritative surfaces: source
stages, failed/abandoned source finals, terminal registry stages, and
failed/validation-failed registry finals. Durable operation/item rows own the
exact department, resource, publication attempt, attempt number, expected
versions, and surface. The descriptor-bound store opens the private root and
UUID path without following links, verifies UID/mode/identity, and keeps the
chain authoritative through a no-replace move into `.deleting`. Entries are
unlinked only through the exact tombstone descriptor and the parent entry is
rechecked before `rmdir`.

Partial markers and payloads are not parsed or logged; missing, zero-byte,
truncated, or interrupted markers remain recoverable when exact metadata and
private path authority match. Final surfaces still require the complete closed
manifest and every exact digest and size. Symlinks, hard links, substituted
parents, wrong UID/mode, foreign scope, unknown entries, and non-directories
terminalize as blocked fixed-code items. A blocked item cannot starve later
valid items. Reconciliation never transitions an adapter/source to purge,
releases a dependency, deletes Phase 10/11 artifacts, or claims deletion from
backups or audit history. Filesystem publication and PostgreSQL state remain
non-atomic, and an already in-flight filesystem request cannot be fenced by
PostgreSQL.

## Google Drive limitations

Google Drive provides convenient local synchronization, not a production object store, database, queue, locking service, or backup policy. Concurrent writers and large model artifacts may create sync conflicts, partial uploads, quota pressure, or slow startup. Production storage will require a separately reviewed design; this policy does not claim that the local Google Drive layout is suitable for production deployment.

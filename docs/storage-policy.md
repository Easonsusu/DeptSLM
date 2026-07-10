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
| Uploaded source files | `uploads/` | Future paths must be isolated by `department_id`. |
| Extracted or normalized text | `extracted_text/` | Treat as sensitive and untrusted. |
| Vector database snapshots | `vector_snapshots/` | Live Qdrant persistence is a separate deployment concern; it must also remain outside the repo. |
| Generated training datasets | `training_datasets/` | Store provenance and department ownership. |
| LoRA and QLoRA adapters | `adapters/` | Never mix or fall back across departments. |
| Downloaded model files and caches | `model_cache/` | Includes weights, tokenizers, and derived caches. |
| Evaluation outputs | `eval_results/` | Synthetic fixtures alone may live in the repo. |
| Runtime logs | `logs/` | Do not log document text, secrets, or unnecessary personal data. |
| Generated reports and bundles | `exports/` | Review access and content before sharing. |
| Local service state | `service_state/` | Compose-only PostgreSQL and Qdrant persistence; not a backup or portable snapshot. |

Future path conventions should include a validated, non-user-controlled department segment, for example `<root>/uploads/<department_id>/...`. Code must use safe path joining and reject traversal outside the resolved `DEPTSLM_DATA_DIR` root.

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

Do not hard-code a developer's absolute Google Drive path in source code, Docker files, tests, or committed environment templates. `.env.example` should contain a placeholder; each developer keeps the real value in an untracked `.env`.

## Docker Compose

Local Compose configuration passes `DEPTSLM_DATA_DIR` explicitly to services that need it and binds only approved external subpaths. Use `scripts/compose.sh` for local Compose commands: it rejects missing, relative, root, nonexistent, non-writable, source-overlapping, and incomplete host paths before Docker runs. The wrapper also sets a required Compose guard, so a direct `docker compose` invocation fails instead of bypassing validation. Bind mounts disable automatic host-path creation.

PostgreSQL and live Qdrant state use `service_state/` bind mounts in the Phase 0 placeholder. Before real data is used, review whether synchronized Google Drive storage is suitable, ensure volumes cannot resolve inside the checkout, and document backup, synchronization, corruption, retention, and recovery behavior. Portable Qdrant snapshots intended for retention belong in `vector_snapshots/`.

## Tests and CI

Tests and CI must not depend on Google Drive. Each run should create a fresh temporary directory, set `DEPTSLM_DATA_DIR` to its absolute path, create only the required test subdirectories, and remove it at the end. Test fixtures must be synthetic and non-sensitive. Tests should also verify that:

- startup fails when `DEPTSLM_DATA_DIR` is absent where required;
- paths cannot escape the configured root;
- one department cannot read another department's artifacts;
- no test writes runtime artifacts into the repository.

## Google Drive limitations

Google Drive provides convenient local synchronization, not a production object store, database, queue, locking service, or backup policy. Concurrent writers and large model artifacts may create sync conflicts, partial uploads, quota pressure, or slow startup. Production storage will require a separately reviewed design; this policy does not claim that the local Google Drive layout is production ready.

# Phase 12.1E-A adapter artifact reconciliation

Phase 12.1E-A is a maintenance-only foundation. It does not purge an adapter
or source, release an upstream dependency, evaluate or approve an adapter,
load a model, or change runtime routing. The command is local, explicitly
administrator-authorized, department-scoped, bounded to 1--1000 items, and
dry-run by default:

```text
python -m app.admin reconcile-adapter-artifacts \
  --department-id <department-uuid> \
  --actor-issuer <issuer> \
  --actor-subject <subject> \
  [--limit 100] [--apply]
```

Only `system_admin` and `department_admin` membership in the selected
department may run it. A system administrator has no cross-department bypass.
The service-level age bound is 300--86,400 seconds and is checked again by
the database-backed operation; output contains counts and fixed surface names,
never IDs, paths, bytes, hashes, manifest contents, or error details.

## Exact scope

Each operation registers content-free PostgreSQL rows before an apply pass.
Each item binds one exact department, resource, publication attempt, attempt
number, and expected resource/attempt versions. The only eligible surfaces are:

| Surface | Eligibility |
| --- | --- |
| `source_stage` | exact incomplete source-attempt stage, including partial or missing marker states |
| `source_final` | only a failed or abandoned non-authoritative source with a complete manifest |
| `registry_stage` | exact terminal registry-attempt stage |
| `registry_final` | only failed or validation-failed adapters with a complete manifest |

Committed, claimed, consumed, purge-pending, or purged sources; validated or
purge-pending/purged adapters; authoritative finals; Phase 10 datasets; Phase
11 bundles; and every upstream dependency are excluded. No transition to a
purge state occurs and no dependency is released.

## Descriptor and tombstone contract

The store opens the exact `adapters` root, department, resource, and stage or
final chain with `O_NOFOLLOW`, private `0700` directories, current service UID,
and identity checks. It keeps the chain authoritative through inspection and
the descriptor-relative no-replace move into `.deleting/<surface>/<department>/<resource>/<item>`.
Entries are removed only through that exact tombstone descriptor. Parent entry
identity is checked immediately before both the move and final `rmdir`.

Partial stages are owned by durable metadata plus the exact UUID path. Marker,
manifest, and payload contents are never parsed or logged during partial-stage
cleanup; zero-byte, truncated, missing, or interrupted markers are recoverable.
Final surfaces require the complete fixed allowlist, closed manifest, exact
SHA-256 digests, sizes, permissions, and identity. Symlinks, hard links,
unknown entries, substituted parents, wrong UID or mode, foreign scope, and
non-directories fail closed as fixed blocked item codes.

The item records tombstone identity and a deletion plan before unlink. Each
member unlink is fenced by its persisted identity and an in-flight marker;
retries accept a missing member only after that exact unlink was durably
recorded. A post-move crash remains resumable. A blocked item is terminalized
without preventing later valid items in the same bounded batch. One completed
operation appends at most one content-free success audit after every applicable
item is complete; it never claims deletion from backups or historical audit.

## Isolation and deployment

`services/adapter-maintenance` is a manual Compose `maintenance` profile. It
runs as the unprivileged service user with a read-only root filesystem and
dropped capabilities, and its only writable bind is the external
`DEPTSLM_DATA_DIR/adapters` root. It receives PostgreSQL authority and no
Qdrant, model, tokenizer, PEFT, Hugging Face, upload, extraction, evaluation,
dataset, training-job, export, or application-auth-secret settings. The API
image and service have no adapter storage mount. Runtime artifacts remain
outside Git under `DEPTSLM_DATA_DIR`.

PostgreSQL authority and external filesystem publication are compensating
controls, not a distributed transaction. An in-flight filesystem operation
cannot be fenced atomically by PostgreSQL; exact descriptors, tombstones, and
subsequent authority checks keep unknown or orphaned bytes untrusted.

Phase 12.1E-B/C, evaluation, approval, promotion, loading, runtime routing,
and later Phase 12/13 work remain separate future phases.

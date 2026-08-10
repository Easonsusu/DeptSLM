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

Every applied operation is a durable retry generation. Historical `blocked`
items are immutable evidence and are never reopened or mutated. After an
administrator repairs the reviewed external condition, a later authorized
operation may register a fresh item for the same exact resource, attempt, and
surface. At most one generation is active for a physical surface. For every
surface, valid untried work is ordered before blocked retries across both
source and registry lanes. Committed, succeeded, active, protected, and
sibling-authority filters are applied in bulk after each bounded keyset
window, so a persistent source retry cannot consume every operation while an
untried registry attempt waits. Among blocked siblings, the scheduler uses
exact retry counts and the latest blocked generation to rotate deterministically;
a repaired newer generation can therefore progress without being starved by an
older mismatch. Stage retries use the same ordering and never depend on a final
manifest being present. Each family first reads a deterministic keyset window
of at most `min(limit * 8, 1000)` attempt rows. The per-status queries use the
full `(department_id, status, created_at, id)` indexes and fixed quotas whose
sum is that bound; they do not rank an unbounded eligible relation with
correlated history expressions. A separate content-free PostgreSQL cursor
records the last inspected `(created_at, id)` boundary for each department,
family, and lifecycle status. Apply mode advances each status cursor even when
every row in its window is structurally ineligible, so repeated operations
cannot rescan the same blocked prefix forever or jump over an uninspected row
from another status; dry-run mode never creates or updates cursor rows. When a
status suffix is exhausted, that bounded keyset stream wraps deterministically
to the beginning and persists its new bounded boundary; it does not append the
prior cursor row, so an adjacent older row cannot remain outside the circular
scan.
Only that window is materialized for structural checks and detailed fairness.
Sibling authority uses bounded grouped aggregate results for the resources in
the window rather than materializing every historical sibling row or issuing
N+1 queries. History aggregation receives at most the window keys per surface.
The final merged selection
locks only the distinct attempt/resource rows (at most the requested limit)
with `FOR UPDATE SKIP LOCKED`, and no more than that many attempts are
registered in an operation. A blocked-only generation is
`completed_with_blocks` and emits no deletion-success audit. A mixed generation
emits at most one content-free success audit only after an exact attempt
commits `cleanup_confirmed_at`; a completed item with another blocked applicable
surface is not sufficient. A later retry emits an audit only for a newly
confirmed resource not already covered by an earlier mixed-operation audit.

## Descriptor and tombstone contract

The store opens the exact `adapters` root, department, resource, and stage or
final chain with `O_NOFOLLOW`, private `0700` directories, current service UID,
and identity checks. Inspection is a read-only descriptor operation: it returns
only a closed identity and deletion plan. PostgreSQL persists that observation
as `registered -> verified`, then commits `move_authorized_at` and the exact
item-scoped tombstone namespace before the worker reopens the original chain.
The move compares every path, descriptor, digest, size, mode, UID, link count,
`mtime_ns`, and `ctime_ns`, performs a no-replace rename, fsyncs both parents,
and persists the resulting tombstone identity as `tombstone_bound` before any
unlink. Entries are removed only through the committed tombstone descriptor;
parent entry identity is checked immediately before both the move and final
`rmdir`.

Partial stages are owned by durable metadata plus the exact UUID path. Marker,
manifest, and payload contents are never parsed or logged during partial-stage
cleanup; zero-byte, truncated, missing, or interrupted markers are recoverable.
Final surfaces require the complete fixed allowlist, closed manifest, exact
SHA-256 digests, sizes, permissions, and identity. Symlinks, hard links,
unknown entries, substituted parents, wrong UID or mode, foreign scope, and
non-directories fail closed as fixed blocked item codes.

The item records the observed identity and deletion plan before rename, then
records tombstone identity before unlink. An unbound item-scoped tombstone is
never adopted or parsed: it blocks with `artifact_tombstone_conflict` and keeps
both surfaces untouched. A post-rename/pre-commit crash is resumable only when
the durable move intent exists, the original is absent, and the tombstone
matches the previously committed observation and plan. Each member unlink is
fenced by its persisted identity and an in-flight marker; retries accept a
missing member only after that exact unlink was durably recorded. A blocked
item is terminalized without preventing later valid items in the same bounded
batch. The limit selects complete attempt groups, while `eligible_count` is the
actual number of registered surfaces. Cleanup confirmation scans every
operation and applicable surface for the exact attempt and is set once only
after all originals and tombstones are absent. One completed operation appends
at most one content-free success audit; it never claims deletion from backups
or historical audit.

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

Migration `0013_phase12_adapter_reconciliation_cursor` adds the independent
cursor table and the complete attempt keyset indexes. The cursor identity is
`(department_id, family, lifecycle_status)`: each independently quota-limited
status stream advances and wraps on its own, so a later key in one status can
never jump over an uninspected key in another status. PR #19 has a different
`0013` migration on its branch; it must be rebased and renumbered to `0014`
after this hotfix merges, before the branches are combined.

Phase 12.1E-B/C, evaluation, approval, promotion, loading, runtime routing,
and later Phase 12/13 work remain separate future phases.

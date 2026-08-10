# Phase 12.1E-B adapter artifact purge

Phase 12.1E-B is a maintenance-only, administrator-authorized purge boundary
for one exact validated adapter. It is under review and does not add an HTTP
route, deployment state, evaluation, runtime loading, or training behavior.

## Authority and authorization

The `purge-adapter-artifacts` command is dry-run by default; `--apply` is
required for mutation. It accepts one department and adapter UUID, a bounded
operation/item limit, and a server-resolved administrator identity. A
department administrator or same-department system administrator may act only
through current membership. `system_admin` has no cross-department bypass.
PostgreSQL locks and records an independent operation, source/registry
reservations, and exact item rows before any filesystem change. Each row keeps
the department, adapter/source IDs, attempt IDs, publication IDs, attempt
numbers, versions, closed manifest, expected status, and content-free observed
or tombstone identities. Replays resume the same operation and never create a
second active reservation.

The authority snapshot must still show the validated adapter, consumed source,
committed source attempt, succeeded registry attempt, exact governance lineage,
no active claim, and no conflicting Phase 12.1E-A reconciliation item. The
final success transaction reauthorizes all of those fields and appends at most
one content-free `adapter.purge` success audit. PostgreSQL is the authority;
filesystem publication and deletion are not transactionally atomic.

## Deletion order and storage boundary

Registry bytes are verified and deleted before source bytes. Source deletion is
blocked unless the independently registered registry item is completed. The
purge store uses a separate private namespace:

```text
adapters/.purge-deleting/source_final/<department_id>/<source_bundle_id>/<item_id>/
adapters/.purge-deleting/registry_final/<department_id>/<adapter_id>/<item_id>/
```

The source and registry finals remain in their canonical external locations
until a complete closed manifest, exact file digests and sizes, private
permissions, current-service ownership, UUID-derived path, and PostgreSQL
authority have been verified. The verified observation, exact deletion plan,
and item-scoped tombstone namespace are committed in a short
`deletion_authorized` transaction before filesystem mutation. The move is
then same-filesystem and no-replace, followed by parent and tombstone-directory
fsync. A separate short transaction commits the exact `tombstone_bound`
identity before any unlink. The initial rename requires an empty exact resource
namespace, and an existing or unknown tombstone is never adopted. Once the
move intent is durable, a canonical private unknown sibling found before the
initial rename is preserved and causes a retryable operator-resolved conflict;
the same item can resume only after reviewed external removal of that sibling.
For unbound post-rename recovery, the exact resource namespace must contain
only the one durable item UUID. The same retryable rule applies to a canonical
private unknown sibling after rename; replacement of the expected item itself,
or malformed or unsafe namespace state, remains a terminal authority mismatch.

All pathname components are opened descriptor-relatively with no-follow
semantics. The original directory and each fixed file are identity-checked
before the move. The tombstone directory is identity-checked again before each
descriptor-relative unlink and before `rmdir`; a missing member is accepted
only when its exact in-flight unlink intent was durably recorded. A directory
unlink intent is recorded before the first `rmdir`, and a retry may accept the
missing directory only after that intent. Substituted, symlinked, foreign,
permissive, hard-linked, or non-directory paths remain blocked without
deletion.

## Crash recovery and retention

The move-intent commit is the retry authority after a process crash: before
rename, a valid unknown sibling keeps that exact durable item active without
moving either directory; after rename, recovery may use only the exact
committed observation and singleton tombstone namespace. A retry must reject a
substituted original or expected tombstone. PostgreSQL and filesystem operations
remain non-atomic, so an already in-flight rename cannot be fenced
retroactively; the committed move intent and post-move identity checks keep
such state bounded and untrusted. Each file deletion records `in_flight_entry` and `next_entry_index`; each
directory deletion records `directory_unlink_started_at`. A crash before an
unlink leaves an active reservation that can retry the exact identity. A crash
after an unlink or directory removal is accepted only for the matching durable
step. Partial state is never parsed or logged. The operation remains terminally
blocked when the stored identity, manifest, or authority no longer matches;
the exact unknown-sibling namespace conflict above remains resumable only with
the same durable item after external reviewed removal.

Immediately before the final PostgreSQL transition, both exact source and
registry purge namespaces must be empty. A remaining unknown tombstone is not
deleted, quarantined, or adopted, and prevents the `purged` lifecycle change
and success audit until an external reviewed recovery removes only that state.

Purge removes only the exact source and registry artifact bytes. It never
deletes Phase 10 datasets, Phase 11 training-job bundles, PostgreSQL lineage,
reviews, deployment history, audit rows, backups, Google Drive history, or
other retained copies. A blocked item does not prevent later independent items
from being reconciled, and a blocked operation never changes an adapter to
`purged`. The maintenance process has only PostgreSQL metadata and the
external adapters mount; it receives no model, Qdrant, RAG, dataset, upload,
or application runtime stack.

## Non-goals

This phase does not approve, evaluate, promote, load, route, reconcile
non-authoritative E-A surfaces, release upstream dependencies, or implement
Phase 12.1E-C, Phase 12.2, Phase 12.3, Phase 12.4, or Phase 13 behavior.

# Phase 12.1E-C adapter lifecycle release

Phase 12.1E-C is the completed reviewed, narrow lifecycle boundary after a
completed Phase 12.1E-B adapter-byte purge. It does not perform another purge,
reconcile artifacts, read artifact contents, or implement evaluation, approval,
promotion, deployment, routing, loading, rollback, training, or Phase 12.2.

## Command and authorization

The local maintenance command is dry-run by default:

```text
python -m app.admin release-adapter-upstream-dependency \
  --department-id <uuid> \
  --adapter-id <uuid> \
  --expected-adapter-version <positive-integer> \
  --expected-source-version <positive-integer> \
  --expected-dependency-version <positive-integer> \
  --actor-issuer <opaque-issuer> \
  --actor-subject <opaque-subject> [--apply]
```

Only active same-department `system_admin` and `department_admin` memberships
may run it. `system_admin` has no cross-department bypass. The command accepts
no filesystem path, manifest, digest, operation, attempt, source, dataset,
job, or dependency selector; all authority is derived from the exact adapter
row and current server-side membership.

Dry-run validates and returns content-free metadata only. Apply mode is the
only mode that writes state. There is no HTTP mutation route.

## Required authority

For the exact adapter and its claimed source, E-C requires:

- adapter and source are both `purged`, have their purge timestamps, retain
  the exact same department/source/claim lineage, and the adapter has no
  worker, claim token, or lease;
- verified governance and artifact flags remain true;
- caller-supplied adapter and source versions match exactly;
- exactly one department-scoped upstream dependency matches the adapter's
  captured Phase 11 job and Phase 10 dataset lineage, has the supplied version,
  and has a consistent `active` or `released` lifecycle;
- the dependency's exact `TrainingJob` and `SftDatasetBuild` still share the
  department and immutable adapter lineage. E-C does not require either
  upstream artifact to be purge-eligible, archived, or otherwise modified;
- exactly one matching E-B operation is `completed`, has two eligible and two
  completed items, zero blocked items, a completion timestamp, and one
  transactional `adapter.purge` success audit;
- that operation retains exact source and registry attempt IDs, publication
  UUIDs, attempt numbers, content-free snapshots, two completed reservations,
  two completed items, and their closed manifest ownership; and
- no active E-B operation exists for the adapter and no active E-A item can
  mutate either exact source or registry final surface.

Missing, duplicate, inconsistent, blocked, or incomplete authority fails
closed. Historical failed or blocked E-B operations do not prevent a single
unambiguous successful operation from proving release authority.

## Read-only storage proof

E-C uses `AdapterPurgeArtifactStore` only for descriptor-relative no-follow
inspection of these exact paths:

```text
adapters/registry/<department_id>/<adapter_id>
adapters/imports/<department_id>/<source_bundle_id>
adapters/.purge-deleting/registry_final/<department_id>/<adapter_id>/
adapters/.purge-deleting/source_final/<department_id>/<source_bundle_id>/
```

Both final directories must be absent and both exact E-B resource namespaces
must be empty. A reappeared final, expected or unknown tombstone, malformed
entry, symlink, wrong ownership/mode, substituted descriptor chain, or missing
storage root fails closed. E-C never parses, hashes, opens payload content,
renames, unlinks, creates, repairs, or quarantines an artifact.

The external filesystem and PostgreSQL are not transactionally atomic. E-C
performs the proof before apply and rechecks it inside the final short database
transaction. A same-user writer can still race a filesystem observation, so no
storage observation is treated as an atomic fence or as permission to delete.

## Transaction lock order

E-C apply uses one canonical PostgreSQL lock order shared with the adjacent
maintenance workflows. The transaction first acquires the exact department
authorization fence, then performs a bounded active E-B operation probe. It
then locks the adapter and source, their authoritative source and registry
attempts, the exact Phase 11 training job and Phase 10 dataset build, and the
single upstream dependency. Only after those rows are stable does it select at
most two matching successful E-B operations and lock the exact two
reservations, two items, one success audit, and a bounded active E-A probe.

The E-B finalization path acquires the same department fence before its active
operation, reservations/items, adapter/source, attempts, and upstream rows;
Phase 12.1C enqueue is authorization-first as well. Dry-run E-C performs the
same bounded proof without `FOR UPDATE` locks. Historical failed or blocked
operations are never materialized or locked. This ordering prevents a
`resource rows -> department authorization` cycle while preserving the
department-scoped fail-closed boundary.

## Apply and idempotency

Apply reauthorizes membership, adapter/source/dependency lineage, E-B operation,
reservations, items, upstream rows, E-A/E-B activity, supplied versions, and
read-only storage proof while holding the reviewed short transaction locks. It
changes only the exact dependency:

- `active` becomes `released`;
- `released_at` uses PostgreSQL server time;
- that dependency version increments once;
- the adapter version increments once; and
- one `adapter.upstream_dependency.release` success audit is appended with
  resource type `adapter`.

It does not change adapter/source/job/dataset statuses or timestamps, E-B
operation/reservation/item history, Phase 10 or Phase 11 artifacts, review or
deployment history, backups, Google Drive history, or audit history.

If the same exact dependency is already consistently released and all authority
still validates with the current post-release adapter, source, and dependency
versions, E-C returns an idempotent content-free no-op. It does not increment a
version or add another audit. Contradictory released state fails closed.
Releasing one adapter's dependency does not release another adapter's dependency
that shares a Phase 10 dataset or Phase 11 job; existing upstream selection
fences remain active until every separate dependency is released.

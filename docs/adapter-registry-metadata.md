# Phase 12.1D adapter metadata reads

Phase 12.1D adds a read-only, department-scoped view of the immutable adapter
registry. It is a completed PostgreSQL control-plane projection; it does
not load, stream, mutate, reconcile, purge, evaluate, approve, promote, or
route an adapter.

## Endpoints

```text
GET /departments/{department_id}/adapters
GET /departments/{department_id}/adapters/{adapter_id}
```

The list endpoint accepts only integer `limit` values from 1 through 100 and a
non-negative integer `offset`. Results are ordered by `created_at DESC, id ASC`.
The detail endpoint returns a safe `404` when the adapter is absent from the
authorized department. Both routes resolve membership on the server in the
request transaction; a URL department identifier is never an authorization
credential.

Same-department `system_admin`, `department_admin`, and `instructor` roles may
read. Students, viewers, inactive or expired memberships, archived
departments, and cross-department selectors are denied. `system_admin` has no
cross-department bypass.

## Closed projection

The response contains only PostgreSQL metadata:

- adapter and department UUIDs, lifecycle status, safe error code, timestamps,
  and optimistic version;
- exact Phase 11 job and Phase 10 dataset lineage identifiers, versions,
  profile/model/license metadata, and LlamaFactory version;
- reviewed contract-version fields;
- declared external-training, governance-lineage, artifact-compatibility,
  and training-provenance booleans; and
- source/dependency status and server-recorded retention timestamps.

It never contains adapter bytes, `adapter_config.json`, tensors, tensor names or
shapes, hashes, byte sizes, filenames, host paths, storage descriptors,
requester or worker identities, claim tokens, secrets, Qdrant settings, model
runtime settings, prompts, examples, or model output. Pydantic schemas reject
additional response fields, and reads do not append audit events or change
database versions.

## Authority and retention

Every returned adapter must have an exact same-department source association
and an exact upstream dependency for its Phase 11 job and Phase 10 dataset. A
non-purged adapter requires an active dependency; a purged historical adapter
may show a released dependency with its release timestamp. Missing, duplicate,
foreign, or lifecycle-inconsistent associations fail closed as unavailable
metadata rather than leaking partial state.

PostgreSQL is the sole read authority. The routes do not inspect external
registry storage, and they do not claim that bytes, backups, Google Drive
history, or audit history have been deleted. Phase 12.1E-A reconciliation is a
separate explicit, bounded, department-scoped maintenance operation and does
not alter this read projection.

## Non-goals

There is no upload, download, manifest/configuration endpoint, tensor endpoint,
mutation endpoint, evaluation endpoint, approval or promotion endpoint,
deployment pointer, runtime adapter loading, silent base-model fallback,
  cross-department cache, or Phase 12.1E-B/C behavior in this phase.

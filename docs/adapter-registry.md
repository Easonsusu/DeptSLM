# Phase 12 adapter registry contract

This document is the reviewed Phase 12.0 design boundary. It describes future
metadata, artifact, evaluation, review, deployment, and runtime contracts; it
does not implement any of them.

## Status

- Phase 11 is completed. Its reviewed output is an immutable, department-scoped
  LlamaFactory job bundle; it does not execute training or create an adapter.
- Phase 12 is under review.
- Phase 12.0 defines contracts and a threat model only.
- DeptSLM has not imported, validated, evaluated, approved, promoted, loaded, or
  purged an adapter.
- Phase 12.1, 12.2, 12.3, and 12.4 remain unimplemented.
- Phase 13 has not started.

No statement in this document is a claim that an adapter registry, adapter
evaluation, deployment pointer, runtime loading, or rollback exists today.

## Phase 12 objective

Phase 12 is intended to provide controlled intake of externally produced LoRA
or QLoRA adapter artifacts and an immutable, department-scoped adapter
registry. The future registry must preserve exact lineage to a Phase 10 dataset
and a Phase 11 training-job bundle, retain adapter evaluation evidence, require
explicit review and approval, support controlled department promotion, and
support rollback to a previous adapter or explicitly to the base model. A later
reviewed subphase may add fail-closed runtime routing.

This boundary does not make external training trustworthy. An operator's claim
about how an adapter was produced is an input to validate, not evidence that
the artifact is safe, compatible, or useful.

## Subphase plan

### Phase 12.0 — contract and threat model (current; under review)

- Correct project status and document the threat model.
- Define closed adapter artifact and metadata contracts.
- Design API, storage, lifecycle, evaluation, promotion, rollback,
  reconciliation, and purge boundaries.
- Make no implementation, migration, dependency, service, or runtime change.

### Phase 12.1 — immutable adapter intake (not started)

- Validate an external adapter through the closed artifact contract.
- Publish it to private external storage and register metadata only.
- Add reconciliation and purge foundations.
- Do not load an adapter at runtime.

### Phase 12.2 — adapter-target evaluation (not started)

- Produce exact baseline and candidate evidence for one adapter version.
- Reuse reviewed production-policy evaluation behavior where applicable.
- Apply fixed numeric quality and safety gates.
- Never promote automatically.

### Phase 12.3 — review, promotion, and rollback (not started)

- Add explicit review and approval, department promotion, supersession,
  rollback, rollback-to-base, and immutable deployment-event history.
- Never silently fall back between departments or from a failed adapter load.

### Phase 12.4 — runtime routing (not started)

- Route a department request through one immutable deployment snapshot.
- Load only an exactly validated and approved department adapter.
- Fail closed on load or contract errors.
- Support explicit rollback-to-base; do not use an implicit fallback.

### Phase 13 (not started)

Phase 13 remains the separate security-hardening, Docker demonstration, and
final-documentation phase.

## Trust and threat model

Every externally produced adapter is untrusted input. Future intake must defend
against:

- cross-department adapter substitution;
- an adapter bound to the wrong Phase 11 job, publication attempt, or dataset;
- the wrong base model or immutable revision;
- arbitrary or unsafe PEFT configuration;
- a full-model checkpoint disguised as an adapter;
- pickle, executable, or other unsafe formats;
- unexpected files, symbolic links, hard links, path replacement, and same-UID
  replacement;
- exclusive-creation races, partial writes, truncated files, and publication
  crashes;
- oversized tensors/files and malformed safetensors headers;
- invalid tensor names, shapes, ranks, or dtypes;
- database/filesystem non-atomicity;
- stale evaluation evidence, evidence reused across adapters or departments,
  and approval or promotion races;
- runtime cache-key collisions, adapter loading failure, silent base-model
  fallback, rollback races, retention races, and purge races; and
- Google Drive synchronization conflicts, partial synchronization, or quota
  pressure.

Filenames and paths are never client authority. An external operator attestation
is not proof. An imported adapter is not an approved adapter; an approved
adapter is not automatically promoted; and a promoted adapter is not proof of
safety or quality. Google Drive is development storage, not an object store,
locking service, or backup guarantee.

## Required lineage

Every future adapter version must bind to one exact same-department Phase 11
training job. Its content-free registry metadata must preserve:

- adapter ID and department ID;
- Phase 11 training job ID and job version;
- Phase 11 publication attempt ID;
- Phase 11 profile ID and manifest digest;
- Phase 10 dataset build ID, dataset version, and manifest digest;
- base model ID, immutable revision, and license metadata;
- the Phase 11 LlamaFactory version;
- adapter contract versions;
- intake publication attempt ID and positive attempt number; and
- code revision.

The reviewed base contract is:

| Field | Value |
| --- | --- |
| Base model ID | `Qwen/Qwen3-0.6B` |
| Base model revision | `c1899de289a04d12100db370d81485cdf75e47ca` |
| License metadata | `Apache-2.0` |
| Phase 11 LlamaFactory version | `0.9.5` |

Phase 12.1 must fail closed if any lineage field changes between initial
authorization, validation, publication, or final commit.

## Planned adapter artifact contract

The proposed final artifact directory contains exactly these three files:

```text
manifest.json
adapter_config.json
adapter_model.safetensors
```

The contract explicitly prohibits pickle, `adapter_model.bin`, PyTorch `.pt` or
`.pth`, GGUF, base-model weights, tokenizer files, optimizer/scheduler/trainer
state, checkpoints, training logs, shell or Python scripts, arbitrary README
files, caches, temporary files, symbolic links, hard links, and unknown entries.

Phase 12.1 must pin and review the compatible versions of PEFT, safetensors,
and the transformers interfaces required for validation. Phase 12.0 does not
invent package versions. Phase 12.1 must first define a closed
`adapter_config.json` key and value contract; arbitrary PEFT configuration is
not accepted.

Validation must check the exact reviewed base-model lineage, safetensors header,
tensor names, shapes, ranks, dtypes, aggregate sizes, and adapter type. A file
that contains full-model weights or an otherwise valid but unreviewed format is
not an adapter under this contract.

## Planned storage layout

All paths are future external runtime paths below `DEPTSLM_DATA_DIR`:

```text
adapters/
  imports/
    <department_id>/
      <source_bundle_id>/
        intake_manifest.json
        adapter_config.json
        adapter_model.safetensors
  registry/
    <department_id>/
      <adapter_id>/
        manifest.json
        adapter_config.json
        adapter_model.safetensors
  .staging/
    registry/
      <department_id>/
        <adapter_id>/
          <publication_attempt_id>/
  .deleting/
    registry/
      <department_id>/
        <adapter_id>/
          <purge_operation_id>/
```

The future implementation must require a pre-existing `adapters` root, UUID-
only server-owned path components, real directories, private `0700`
directories, private `0600` files, current service UID ownership, no-follow
descriptor-relative access, no symlinks, no hard links, exclusive creation,
and no overwrite. Publication must be same-filesystem atomic, fsync files and
directories, and rehash after rename. Retained descriptor identity must be
rechecked through the final PostgreSQL commit.

There is no fallback to the checkout, current directory, `/tmp`, a home or
cache directory, `logs`, `exports`, or `model_cache`. Host paths must never
enter PostgreSQL, API responses, audits, or logs. PostgreSQL and external
storage are non-atomic; a successful database transaction cannot claim that an
external rename or fsync happened atomically with it.

## Planned immutable source-intake boundary

The source of an adapter is a private, immutable, department-scoped import
bundle. The planned source layout is:

```text
adapters/
  imports/
    <department_id>/
      <source_bundle_id>/
        intake_manifest.json
        adapter_config.json
        adapter_model.safetensors
  .staging/
    imports/
      <department_id>/
        <source_bundle_id>/
          <import_attempt_id>/
```

This layout is planned only and is not created in Phase 12.0. An
administrator-controlled CLI will stream only the two externally supplied
payload files, `adapter_config.json` and `adapter_model.safetensors`, into a
private server-ID-derived stage. DeptSLM then generates `intake_manifest.json`
after basic descriptor verification. An externally supplied `manifest.json`,
`intake_manifest.json`, README, archive, directory tree, or unknown file is
rejected. External manifests and operator attestations are untrusted input and
never become authority.

The import source bundle is immutable and is verified through retained
descriptor-relative, no-follow handles. No source host path is stored in
PostgreSQL, audit events, logs, public APIs, or a registry manifest. A queued
adapter-registry worker may consume only an already committed immutable import
source bundle; it must never reopen a user-supplied arbitrary host path from
PostgreSQL metadata.

The external source allowlist is exactly:

```text
intake_manifest.json
adapter_config.json
adapter_model.safetensors
```

The final registry allowlist is separately exactly:

```text
manifest.json
adapter_config.json
adapter_model.safetensors
```

The final `manifest.json` is generated by DeptSLM after complete validation. It
binds the exact department, source bundle, adapter, intake attempt, Phase 10
lineage, Phase 11 lineage, base model, contracts, payload digests, and positive
sizes. It is never copied from the external source package and is reverified
after publication before PostgreSQL records success.

## Planned intake boundary

Phase 12.1 should begin with an administrator-controlled CLI and an isolated
adapter-registry worker. It must not begin with a browser or public weight-upload
API.

The future worker receives only:

- PostgreSQL;
- read-only adapter imports;
- read-only Phase 11 training-job bundle storage; and
- read-write adapter registry storage.

It must not receive Qdrant, RAG-runtime credentials, API JWT secrets, uploads,
extracted text, evaluation suites or results unless a later reviewed subphase
introduces them, `model_cache`, base-model weights, Hugging Face tokens, cloud
credentials, proxy settings, a Docker socket, or unrelated storage roots. The
API and browser receive no adapter storage mount and no weight-upload route.

Contentful validation belongs in a fixed exec child with exact read-only source
descriptors, exact private staging descriptors, closed content-free metadata, a
secret-free environment, `close_fds`, bounded framed content-free IPC, and
cancellation, shutdown, deadline, and lease supervision. Adapter bytes must not
cross IPC; the parent must never parse or materialize the complete adapter.

## Planned PostgreSQL boundary

These are conceptual entities only; names and decomposition may change during
Phase 12.1 review:

- `adapters`
- `adapter_import_attempts`
- `adapter_evaluation_evidence`
- `adapter_reviews`
- `department_adapter_deployments`
- `adapter_deployment_events`
- `adapter_reconciliation_operations`
- `adapter_reconciliation_items`
- `adapter_purge_operations`
- `adapter_purge_items`
- `adapter_purge_reservations`

PostgreSQL may store only content-free metadata: UUIDs, department scope,
lifecycle states, lineage, contract versions, digests, positive byte sizes,
reviewed tensor counts or aggregate shape metadata, evaluation IDs, numeric
gate state, review state, the active deployment pointer, timestamps, optimistic
versions, and closed ownership manifests.

It must not store adapter bytes, `adapter_config.json` bytes, tensor bytes or
values, host paths, training logs, prompts, answers, dataset records, raw
evaluation outputs, exception text, credentials, or model output.

## Planned intake eligibility and authority snapshot

Phase 12.1 may accept an adapter only from one exact same-department Phase 11
job that is `succeeded`, has `review_status=approved`, is not purged, is at the
exact expected version, is owned by one exact succeeded publication attempt,
and is backed by the exact descriptor-verified Phase 11 final manifest. The job
must still bind to the reviewed base model and revision, profile, LlamaFactory
version, and Phase 10 dataset snapshot.

Initial intake must capture a complete immutable, content-free snapshot of
every reviewed Phase 10 and Phase 11 authority field. Any authority change
before the final PostgreSQL commit fails closed. An import from a pending,
rejected, archived, purged, failed, cancelled, or foreign Phase 11 job is
unavailable.

## Planned lifecycle and invariants

The reviewed conceptual lifecycle is:

```text
importing -> validated -> evaluation_pending -> review_pending
          -> approved -> promoted -> superseded -> archived -> purged
```

`evaluation_failed` and `rejected` are terminal or retryable states as defined
by the later reviewed implementation. The implementation may separate artifact,
evaluation, review, and deployment state instead of using one overloaded status
field.

Required invariants are:

- imported is not approved;
- approved is not promoted;
- one department has at most one active deployment;
- cross-department promotion is impossible;
- a deployment points to one exact immutable adapter version;
- promotion creates an immutable event;
- rollback creates another immutable event and never rewrites history;
- rollback-to-base explicitly targets the base model;
- an adapter artifact cannot be deleted while an active deployment or explicit
  rollback-retention reference requires it;
- historical metadata references do not by themselves require artifact bytes;
- a purged artifact cannot be active, promoted, loaded, or selected for rollback;
- a department deployment pointer never references a purged artifact; and
- all lifecycle mutations use same-department authorization and optimistic
  version checks.

## Planned retention references and purge semantics

A non-purged adapter registry record creates retention dependencies on its exact
Phase 11 training job and authoritative job bundle and its exact Phase 10
dataset build and authoritative dataset artifact. Future Phase 10 and Phase 11
purge eligibility must fail closed while a dependency exists. Adapter purge
never deletes either upstream artifact; Phase 10 and Phase 11 maintenance must
independently check these dependencies before deleting upstream artifacts.

The following references are distinct:

1. **Immutable historical reference.** Metadata-only deployment and audit
   history remains after adapter artifact purge. It does not by itself require
   adapter bytes to remain.
2. **Active deployment reference.** The department currently routes to this
   exact adapter version. It blocks adapter artifact purge.
3. **Rollback-retention reference.** An explicitly retained, still-approved,
   same-department adapter version remains eligible as a rollback target. It
   blocks artifact purge until removed through a reviewed mutation.

Rollback eligibility requires the same department, exact immutable adapter
version, unpurged and currently verified artifact, approved review, evaluation
evidence still valid under the reviewed policy, exact base-model revision, and
no active purge or conflicting deployment operation. A historical event that
references a purged adapter remains readable as metadata but cannot be a
rollback target.

Removing a rollback-retention dependency requires no active deployment, no
in-progress evaluation, review, promotion, rollback, reconciliation, or purge
operation, no explicit rollback-retention requirement, and completed adapter
artifact purge. PostgreSQL lineage and immutable audit/deployment history may
remain afterward.

Future adapter purge may remove private adapter artifact bytes after every
required fence and retention check and mark the artifact state `purged`. It
retains the metadata row, lineage, evaluations, reviews, deployment events,
timestamps, and audits. Metadata-row deletion is not required adapter purge
behavior.

## Planned evaluation contract

Phase 12.2 must reuse the reviewed Phase 9 production-policy evaluation behavior
where applicable. Evidence must bind the department, adapter and adapter
version, base-model revision, evaluation suite, baseline run, candidate run,
retrieval and generation contracts, numeric metrics, fixed Decimal gates, and
code revision.

No LLM judge may be added without a new reviewed phase. Feedback cannot become
evaluation data automatically, and evaluation cannot promote an adapter
automatically. Stale, foreign, incomplete, cancelled, or mismatched evidence
cannot approve an adapter. Evidence for one department or adapter version
cannot approve another. Generated answers, prompts, evidence, and vectors stay
outside PostgreSQL and public APIs.

## Planned review, promotion, and rollback

Mutation roles are limited to same-department `system_admin` and
`department_admin`. Read access may include same-department `system_admin`,
`department_admin`, and `instructor`. `system_admin` has no cross-department
bypass.

Promotion must require an exact validated artifact, an exact approved Phase 11
job, complete required Phase 12 evaluation evidence, every fixed quality and
safety gate, an approved adapter review, current same-department authorization,
an expected version match, no active purge or conflicting deployment operation,
and final artifact identity revalidation.

Eventually, runtime routing must use one immutable deployment snapshot for the
complete request, reject cross-department or base-revision mismatches, return a
safe `503` on a required adapter-load failure, and never silently fall back to
the base model. The base model is used only when no adapter is explicitly
deployed or after explicit rollback-to-base. Cache keys must include department,
adapter ID, adapter version, and base revision.

## Planned metadata-only API boundary

The following endpoint shapes are conceptual and **not implemented in Phase
12.0**:

- `GET /departments/{department_id}/adapters`
- `GET /departments/{department_id}/adapters/{adapter_id}`
- `POST /departments/{department_id}/adapters/{adapter_id}/evaluations`
- `PATCH /departments/{department_id}/adapters/{adapter_id}/review`
- `POST /departments/{department_id}/adapters/{adapter_id}/promote`
- `POST /departments/{department_id}/adapters/rollback`
- `GET /departments/{department_id}/adapter-deployment`

If implemented, these routes expose safe metadata only. There must be no adapter
weight upload or download, raw manifest or configuration download, host path,
tensor metadata disclosure, arbitrary model selector, or arbitrary adapter
selector endpoint. A URL `department_id` remains a selector; server-side
membership resolution is the authorization boundary.

## Reconciliation, retention, and purge

Future Phase 12 implementation must provide administrator-only commands that are
dry-run by default and have strict bounded limits. It must register a durable
operation and item before filesystem mutation, preserve exact attempt ownership,
clean stages first, retain one authoritative final surface, fence review and
deployment, support crash-resumable deletion, bind exact tombstone identities
before unlink, and produce one operation-level exactly-once success audit.

Metadata, lineage, evaluation, deployment, and audit history are retained. A
purge must not delete Phase 10 datasets or Phase 11 training-job bundles, and it
must not claim deletion from backups, Google Drive history, or other retained
copies. PostgreSQL and external storage remain non-atomic throughout recovery.

## Acceptance criteria for Phase 12

Phase 12 as a whole is complete only when tests prove that:

- source intake accepts only the two payload files, creates a server-generated
  intake manifest, and consumes only a committed immutable source bundle;
- intake is eligible only for the exact approved, succeeded, unpurged Phase 11
  job and its complete immutable Phase 10/11 authority snapshot;
- external adapter artifacts are validated through a closed immutable contract;
- exact Phase 10 and Phase 11 lineage is preserved;
- publication is private, descriptor-bound, immutable, and recoverable;
- invalid, foreign, malformed, substituted, oversized, or full-model artifacts
  fail closed;
- evaluation evidence cannot cross departments, adapters, or versions;
- unapproved adapters cannot be promoted;
- each department has at most one active deployment;
- upstream Phase 10/11 purge is fenced by adapter retention dependencies;
- historical metadata references survive artifact purge without retaining bytes,
  while active and rollback-retention references fence deletion;
- promotion and rollback are transactional at the metadata boundary, versioned,
  and auditable without claiming PostgreSQL/filesystem atomicity;
- runtime routing cannot cross departments and never silently falls back;
- PostgreSQL, APIs, audits, logs, and browser state contain no adapter bytes or
  sensitive content;
- reconciliation and purge are crash-resumable;
- normal CI downloads no real models or adapters; and
- Phase 13 remains unstarted.

Until these criteria are met, an adapter is not available for runtime use.

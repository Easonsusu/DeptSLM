# Phase 12 adapter registry contract

This document is the reviewed Phase 12 governance boundary. It describes the
completed contract work, the reviewed Phase 12.1B source-intake implementation,
the completed Phase 12.1C immutable registry-publication implementation, the
completed Phase 12.1D metadata-only registry read boundary, the completed
Phase 12.1E-A reconciliation foundation, and the completed Phase 12.1E-B
purge authority, and the completed Phase 12.1E-C lifecycle-release scope.
Phase 12.2 and Phase 12.3 are completed, and Phase 12.4 is the current
reviewed adapter-runtime routing scope.
It also records future metadata, evaluation, review, deployment, and runtime contracts. The
separate Phase 12.1A static compatibility contract is documented in
[adapter-static-contract.md](adapter-static-contract.md).

## Status

- Phase 11 is completed. Its reviewed output is an immutable, department-scoped
  LlamaFactory job bundle; it does not execute training or create an adapter.
- Phase 12.0 through Phase 12.3 are completed; Phase 12.4 is under review.
- Phase 12.0 is completed: it defines contracts and the threat model.
- Phase 12.1 is completed. Phase 12.1A, Phase 12.1B, and Phase 12.1C are
  completed. Phase 12.1C binds one exact committed source to one approved succeeded Phase 11
  job and captured Phase 10 authority, then publishes an immutable private
  registry bundle through a leased descriptor-bound worker. It records only
  content-free authority and never evaluates, approves, promotes, loads,
  routes, reconciles, or purges an adapter.
- Phase 12.1D is completed and adds only department-scoped
  PostgreSQL metadata list/detail reads through closed projections. It performs
  no mutation, artifact access, or audit.
- Phase 12.1E-A is completed: it is a separate administrator-only,
  dry-run-by-default operation for four non-authoritative surfaces.
- Phase 12.1E-B is completed. It independently reserves
  exact source/registry attempts and removes registry bytes before source bytes
  through `.purge-deleting` descriptor-bound tombstones.
- Phase 12.1E-C is completed. It can release only one exact
  active upstream dependency after read-only proof that the corresponding E-B
  purge completed without blocks, both final paths remain absent, and both
  exact purge namespaces are empty. It does not touch artifact bytes.
- Phase 12.2 is completed. It adds only paired, content-free adapter-target
  evaluation and does not approve, promote, load, or route.
- Phase 12.3 is completed. It adds separate review, approval, deployment,
  promotion, rollback, retention, and immutable event authorities without
  changing `Adapter.status` or loading an adapter.
- Phase 12.4 is the current reviewed scope. It captures immutable content-free
  deployment snapshots for public RAG requests and routes only adapter
  generation through a separate private runtime; retrieval and evaluation
  runtime semantics remain unchanged.
- Phase 13 has not started.

The Phase 12.1D, Phase 12.2, and Phase 12.3 metadata routes remain the only
public adapter metadata surfaces today. Phase 12.4 adds no adapter selector or
mutation route; the existing grounded-answer endpoint resolves deployment
authority server-side. These surfaces return closed
PostgreSQL projections; evaluation enqueue, cancellation, listing, and detail
operations never expose registry files or evaluation content. There is still
no adapter upload/download or public runtime route. The Phase 12.1C internal
worker publishes immutable registry files and content-free PostgreSQL authority
only; the Phase 12.4 production runtime verifies a private copy before PEFT.

## Phase 12 objective

Phase 12 is intended to provide controlled intake of externally produced LoRA
or QLoRA adapter artifacts and an immutable, department-scoped adapter
registry. The future registry must preserve a verified governance association to
one Phase 10 dataset and one Phase 11 training-job bundle, retain adapter
evaluation evidence, require explicit review and approval, support controlled
department promotion, and support rollback to a previous adapter or explicitly
to the base model. A later reviewed subphase may add fail-closed runtime
routing. This association does not prove dataset use or trusted external
training execution.

This boundary does not make external training trustworthy. An operator's claim
about how an adapter was produced is an input to validate, not evidence that
the artifact is safe, compatible, or useful.

## Subphase plan

### Phase 12.0 — contract and threat model (completed)

- Correct project status and document the threat model.
- Define closed adapter artifact and metadata contracts.
- Design API, storage, lifecycle, evaluation, promotion, rollback,
  reconciliation, and purge boundaries.
- Phase 12.0 made no migration, dependency, service, or runtime change.

### Phase 12.1 — immutable adapter intake (completed)

#### Phase 12.1A — static compatibility contract (implemented in this review)

- Freeze the reviewed Qwen3-0.6B architecture, PEFT configuration, tensor-key
  grammar, exact shapes, dtypes, sizes, and bounded safetensors metadata parser.
- Keep validation model-free, dependency-free, content-free, and separate from
  intake, storage, publication, registry, reconciliation, and purge.

#### Phase 12.1B — immutable source intake (completed)

- Administrator CLI only, dry-run by default, with exactly
  `adapter_config.json` and `adapter_model.safetensors`.
- Validate through the Phase 12.1A model-free child, stream bytes through
  retained descriptors, generate `intake_manifest.json`, and publish one
  immutable private import bundle.
- Commit only department-scoped source and attempt authority metadata. No
  adapter registry row, Phase 11 binding, evaluation, review, approval,
  promotion, runtime loading, reconciliation, or purge is included.

#### Phase 12.1C — immutable registry publication (completed)

- Bind one same-department committed source to one approved succeeded Phase 11
  bundle and the exact Phase 10 authority snapshot captured by that job.
- Claim and lease PostgreSQL metadata with `SKIP LOCKED`; require live claim
  checks before every filesystem or child-owned mutation.
- Build and publish only the exact private `manifest.json`,
  `adapter_config.json`, and `adapter_model.safetensors` registry files.
- Keep declared external training association explicit while leaving training
  provenance unverified. Record source consumption, upstream retention, and a
  transactional success audit only after complete descriptor verification.
- The worker never loads a model or invokes LlamaFactory, and no additional public API,
  evaluation, approval, promotion, runtime routing, reconciliation, or purge
  is added.

#### Phase 12.1D — metadata-only registry reads (completed)

- `GET /departments/{department_id}/adapters` lists department-scoped adapter
  metadata with integer `limit` 1–100, non-negative integer `offset`, and
  deterministic `created_at DESC, id ASC` ordering.
- `GET /departments/{department_id}/adapters/{adapter_id}` reads one exact
  department-scoped adapter or returns a safe `404`.
- Same-department `system_admin`, `department_admin`, and `instructor` roles
  may read. `student`, `viewer`, inactive memberships, expired memberships,
  archived departments, and cross-department selectors are denied.
- Each response is a closed content-free projection of PostgreSQL authority:
  lineage, fixed contracts, verification booleans, lifecycle timestamps,
  source/dependency retention state, and optimistic version. It contains no
  artifact bytes, paths, hashes, tensor names or values, identities, secrets,
  or runtime settings. Reads append no audit and perform no mutation.
- The source association and active/released upstream dependency must match the
  exact adapter and department. Inconsistent or missing authority fails closed
  without revealing resource details. PostgreSQL is the only read authority.

#### Phase 12.1E-A (completed)

- Reconcile only incomplete source stages, failed or abandoned non-authoritative
  source finals, terminal registry stages, and failed or validation-failed
  registry finals through the separate administrator-only maintenance command.
- Use durable operation/item authority, exact PostgreSQL status/version checks,
  descriptor-relative no-follow storage, read-only inspection followed by a
  committed move intent, no-replace tombstone binding, and crash-resumable
  bounded cleanup. Unbound item-scoped tombstones are blocked rather than
  adopted, and cleanup confirmation spans every operation for the exact attempt.
- Never reconcile authoritative finals, purge states, upstream dependencies,
  Phase 10/11 artifacts, or adapter runtime state.

#### Phase 12.1E-B (completed)

- `purge-adapter-artifacts` is a bounded, administrator-only command that is
  dry-run by default and mutates only with `--apply`.
- PostgreSQL registers one operation, one source reservation, one registry
  reservation, and exact item rows before mutation. The rows retain IDs,
  attempts, numbers, versions, closed manifests, expected statuses, and
  content-free filesystem identities. The final transaction reauthorizes the
  complete snapshot and writes at most one success audit.
- Registry final bytes are independently verified and purged first. Source
  final bytes remain blocked until registry deletion completes. The private
  `.purge-deleting` namespace, no-follow descriptors, no-replace same-filesystem
  move, fsync, exact tombstone identities, per-file in-flight progress, and
  directory unlink intent make deletion crash-resumable without adopting an
  unknown tombstone.
- Purge never deletes Phase 10/11 artifacts, lineage, deployment/review
  history, audit rows, backups, or other retained copies. PostgreSQL and
  external storage remain non-atomic. There is no public purge route and no
  runtime adapter loading.

#### Phase 12.1E-C (completed)

- `release-adapter-upstream-dependency` is an administrator-only command that
  is dry-run by default. It accepts only the exact department and adapter
  selectors, expected adapter/source/dependency versions, actor identity, and
  explicit `--apply`; it accepts no path, manifest, digest, attempt, operation,
  source, dataset, job, or dependency selector.
- It proves the adapter and its claimed source are `purged`, the exact one
  upstream dependency is active, and a unique completed E-B operation retained
  the exact source/registry attempts, reservations, items, manifests, and
  success audit with no blocks. It also rejects active E-A or E-B mutation
  authority for either exact final surface.
- It opens the adapters root only for descriptor-relative, no-follow,
  content-free absence checks of the exact source and registry final paths and
  their exact `.purge-deleting` resource namespaces. Reappeared finals,
  expected or unknown tombstones, malformed entries, symlinks, and unsafe
  storage fail closed without cleanup.
- Apply mode reauthorizes every row and the read-only storage proof under a
  short transaction, changes only `adapter_upstream_dependencies` from
  `active` to `released`, increments only that dependency and adapter version,
  and writes one `adapter.upstream_dependency.release` audit. It never changes
  source, adapter, job, dataset, or E-B history lifecycle timestamps/statuses.

### Phase 12.2 — adapter-target evaluation (completed)

Phase 12.2 evaluates one exact validated adapter against the exact Phase 7
baseline using the reviewed Phase 9 production retrieval, prompt, generation,
citation, metric, and Decimal gate policies. For each suite case the worker
prepares one department-authorized retrieval context and deterministic seed,
then sends that same transient context to the baseline and isolated candidate
lanes. The candidate runtime is pinned to the reviewed base model revision and
adapter stack, runs offline, and has no base-model fallback.

The worker stores only closed run, attempt, case, and aggregate numeric
metadata. External result storage contains exactly `manifest.json`,
`summary.json`, and `case_results.jsonl`; it never contains questions, accepted
answers, prompts, retrieved evidence, generated answers, vectors, paths, or
adapter bytes. PostgreSQL server-time leases, cancellation/reclaim, final
authority checks, and the active E-B purge fence remain required. Evaluation
does not change production retrieval, prompts, adapter state, approval,
promotion, or runtime routing.

- Produce exact baseline and candidate evidence for one adapter version.
- Reuse reviewed production-policy evaluation behavior where applicable.
- Apply fixed numeric quality and safety gates.
- Never promote automatically.

### Phase 12.3 — review, promotion, and rollback (completed)

Phase 12.3 adds the control-plane governance layer between completed
evaluation and future runtime routing. Review, approval, deployment, rollback,
and retention are separate department-scoped PostgreSQL authorities; the
artifact lifecycle in `Adapter.status` remains unchanged. The reviewed scope
provides explicit metadata-only operations for:

- starting, approving, rejecting, and archiving a review bound to one exact
  Phase 12.2 run, registry attempt, suite, dependency, and result authority;
- promoting an approved adapter, rolling back to a retained adapter, or
  explicitly rolling back to the base model;
- durable server-time worker leases, reclaim, exact registry-final descriptor
  verification, and operation-scoped safe error codes;
- supersession/deployment history, rollback-retention references, explicit
  retention release, and immutable content-free deployment events.

No review or evaluation automatically approves or promotes an adapter. No
Phase 12.3 operation loads an adapter or routes a production request; those
behaviors remain Phase 12.4. The governance worker receives only PostgreSQL
and a read-only registry-final mount, while Phase 12.1E purge and lifecycle
release remain fenced against active governance operations.

### Phase 12.4 — runtime routing (current; under review)

- Route only generation for a department request through one immutable
  deployment snapshot; query embedding and retrieval remain Phase 7.
- Load only an exactly validated and approved department adapter in the
  separate private production runtime.
- Fail closed on load or contract errors, with no base retry or fallback.
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

## Required governance lineage (not training provenance)

Every future adapter version must bind to one exact same-department Phase 11
training job as its verified governance lineage. Its content-free registry
metadata must preserve:

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

Phase 12.1 must fail closed if any governance-lineage field changes between initial
authorization, validation, publication, or final commit.

This association is not trusted training provenance. Phase 12 can verify the
permitted Phase 11 job, the reviewed contract, and adapter compatibility, but it
cannot prove that an untrusted external environment used the exact job bundle or
Phase 10 dataset, executed the declared LlamaFactory configuration or steps, or
produced the submitted weights without modification. The registry manifest
records a **verified governance lineage**, a **declared external training
association**, and **verified artifact compatibility** only. Operator
attestations remain untrusted metadata. Stronger provenance would require a
separately reviewed trusted-execution, signing, or remote-attestation design.
Evaluation and approval remain mandatory, but they do not turn this association
into proven training provenance.

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

Phase 12.1A pins and documents the compatible PEFT, safetensors, and
Transformers reference versions. It defines one closed `adapter_config.json`
key/value contract; arbitrary PEFT configuration is not accepted.

Validation must check the exact reviewed base-model contract, safetensors header,
tensor names, shapes, ranks, dtypes, aggregate sizes, and adapter type. A file
that contains full-model weights or an otherwise valid but unreviewed format is
not an adapter under this contract.

### Model-free static adapter validation

The Phase 12.1B validation child and Phase 12.1C registry worker have no
`model_cache`, base-model weights, tokenizer,
Transformers model object, or Hugging Face access. Phase 12.1A therefore fixes
the reviewed, content-free, source-controlled static adapter schema: PEFT
`0.18.1`, Transformers `4.55.0`, safetensors format `0.7.0`, the saved
`adapter_config.json` values including `inference_mode=true`,
`auto_mapping=null`, and `peft_version="0.18.1"`, the exact safetensors
`{"format":"pt"}` metadata object, seven target modules, exact tensor-key
grammar, rank and shape relationships, F16/BF16/F32 dtypes, a 1 MiB header
bound, 392 tensors, and a 44,040,192-byte file bound. `__metadata__` is the
only accepted safetensors metadata and is not a tensor. See
[adapter-static-contract.md](adapter-static-contract.md).

The schema requires no model or tokenizer loading, network access, or model
weights. It rejects unknown target modules, keys, shapes, ranks, and dtypes and
fails closed when compatibility cannot be proven statically. This static
contract does not create a worker or make an external adapter trusted.

## External storage layout

Phase 12.1B uses the private import paths below. Phase 12.1C additionally
creates the private registry and registry-staging paths; deletion surfaces
remain future design:

```text
adapters/
  imports/
    <department_id>/
      <source_bundle_id>/
        intake_manifest.json
        adapter_config.json
        adapter_model.safetensors
  registry/                 # Phase 12.1C final publication
    <department_id>/
      <adapter_id>/
        manifest.json
        adapter_config.json
        adapter_model.safetensors
  .staging/
    imports/
      <department_id>/
        <source_bundle_id>/
          <import_attempt_id>/
    registry/
      <department_id>/
        <adapter_id>/
          <publication_attempt_id>/
  .deleting/
    imports/
      <department_id>/
        <source_bundle_id>/
          <purge_operation_id>/
    registry/
      <department_id>/
        <adapter_id>/
          <purge_operation_id>/
```

The implemented 12.1B/12.1C workers require a pre-existing `adapters` root, UUID-
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

## Immutable source-intake boundary

The source of an adapter is a private, immutable, department-scoped import
bundle. The source layout is:

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

Phase 12.1B's administrator-controlled CLI streams only the two externally supplied
payload files, `adapter_config.json` and `adapter_model.safetensors`, into a
private server-ID-derived stage. DeptSLM then generates `intake_manifest.json`
after basic descriptor verification. An externally supplied `manifest.json`,
`intake_manifest.json`, README, archive, directory tree, or unknown file is
rejected. External manifests and operator attestations are untrusted input and
never become authority.

The import source bundle is immutable and is verified through retained
descriptor-relative, no-follow handles. Its server-generated,
closed/content-free `intake_manifest.json` binds the department ID, source-bundle
ID, import publication-attempt ID, positive import-attempt number, safe internal
uploader/request references, the positive sizes and SHA-256 digests of
`adapter_config.json` and `adapter_model.safetensors`, the intake contract
version, and the code revision. It never stores or exposes original host paths,
original filenames, adapter bytes, configuration bytes, tensor values, arbitrary
operator text, external manifests, or credentials. No source host path is stored
in PostgreSQL, audit events, logs, public APIs, or a registry manifest. A queued
adapter-registry worker may consume only an already committed immutable import
source bundle; it must never reopen a user-supplied arbitrary host path from
PostgreSQL metadata.

Committed publication requires private attempt-scoped staging, retained
no-follow descriptors, complete allowlist and identity verification,
same-filesystem no-replace rename, parent fsync, post-rename rehash, and a final
PostgreSQL authority commit. External manifests or attestations remain untrusted
and never authorize ownership.

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
governance lineage, declared Phase 11 training association, base model,
contracts, payload digests, and positive sizes. It is never copied from the
external source package and is reverified after publication before PostgreSQL
records success.

## Planned import-source lifecycle

Import-source state is separate from adapter state. The conceptual states are:

```text
staging -> committed -> claimed -> consumed
    |          |          |
    +------> rejected   abandoned
                         |
                  purge_pending -> purged
```

The exact implementation may represent terminal outcomes with separate
operation fields, but it must preserve these meanings:

- one committed source bundle may be consumed by at most one exact adapter
  version; it cannot be reused to create sibling adapters;
- claiming binds the exact source bundle, adapter ID, intake attempt, department,
  and Phase 11 authority snapshot;
- a failed attempt never transfers the source bundle to another adapter; retry
  remains bound to the same adapter and exact source bundle;
- successful registry publication and PostgreSQL authority transition the source
  to `consumed`; and
- the immutable registry, not the import source, becomes adapter authority after
  that success. The source is never runtime, evaluation, promotion, or rollback
  authority.

`rejected` covers a source that fails closed validation, `abandoned` covers an
interrupted or explicitly abandoned attempt before commitment, and
`purge_pending`/`purged` are reserved for an authorized source-byte purge. A
source still required by an active intake, retry, or reconciliation operation
cannot be purged.

## Phase 12.1B intake and Phase 12.1C publication boundary

Phase 12.1B provides an administrator-controlled CLI and an isolated static
validation child. Phase 12.1C adds the separate leased registry worker; neither
phase provides a browser or public weight-upload API.

The Phase 12.1C worker receives only:

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

The Phase 12.1B and 12.1C implementations bind exact validated-byte authority across
the pre-child and post-child digest passes, descriptor-relative copy, post-
rename publication verification, and the final PostgreSQL commit. Every
transition compares a complete frozen authority snapshot and both row versions;
static contract errors are distinct from the fixed descriptor/operational
codes. Migration `0010_phase12_adapter_sources` is self-contained and freezes
the source SQL contract literals. Migration `0011_phase12_adapter_registry`
adds the adapter, attempt, dependency, and source-claim schema without
importing application code. CI validates the head and private temporary
adapter roots before running the full suite.

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

### Separate rollback-reference and upstream-release transitions

Removing a rollback-retention reference is a reviewed mutation that may occur
before byte purge. It requires the exact adapter in the authorized department,
the adapter not to be the active deployment, no active promotion, rollback,
evaluation, review, reconciliation, or purge operation, exact expected-reference
and adapter-version matches, and current same-department `system_admin` or
`department_admin` authorization. It records an immutable mutation audit and a
deployment-governance event. The removal makes the adapter ineligible as an
explicit rollback target; it does not purge bytes, erase deployment history, or
release the Phase 10/11 upstream dependencies.

Upstream Phase 10 and Phase 11 retention dependencies may be released only
after no active deployment or rollback-retention reference remains, no intake,
evaluation, review, promotion, rollback, reconciliation, or purge operation is
active, every registry artifact byte is confirmed purged through committed
registry-purge authority, every bound import-source byte is confirmed purged
through committed source-purge authority, and the adapter artifact state is
committed as `purged`. Historical metadata, lineage, evaluations, reviews,
deployment events, timestamps, and audits remain.

### Complete adapter-byte purge semantics

Registry purge removes the authoritative registry artifact; source purge removes
the exact bound immutable import source. A registry-only deletion must not mark
an adapter `purged` while any bound import copy remains. An adapter may enter
`purged` only after the registry final is absent through committed purge
authority, every bound source bundle is absent through committed source-purge
authority, no active deployment or rollback-retention reference exists, and no
conflicting operation remains.

A consumed source may be purged before the registry artifact when registry
publication and PostgreSQL success are complete, no intake retry or
reconciliation requires it, the final registry has been independently verified,
and source-purge authority is committed. The adapter may remain active after
that source purge because the immutable registry is runtime authority. Purge
retains the metadata row and history; it does not delete Phase 10 or Phase 11
artifacts and does not claim deletion from backups or synchronized history.

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

Phase 12.4 runtime routing uses one immutable deployment snapshot for the
complete request, rejects cross-department or base-revision mismatches, returns
a safe `503` on a required adapter-load failure, and never silently falls back
to the base model. The base model is used only when no deployment exists or
after explicit rollback-to-base. Cache keys include department, adapter ID,
adapter version, registry publication, digest/size authority, and base revision;
target changes retire the old child process.

## Metadata-only API boundary

The following endpoint shapes are implemented only where noted and otherwise
remain conceptual:

- `GET /departments/{department_id}/adapters` (Phase 12.1D metadata only)
- `GET /departments/{department_id}/adapters/{adapter_id}` (Phase 12.1D metadata only)
- `POST /departments/{department_id}/adapters/{adapter_id}/evaluations` (Phase 12.2 paired evaluation)
- `GET /departments/{department_id}/adapters/{adapter_id}/evaluations` (Phase 12.2 paired evaluation)
- `GET /departments/{department_id}/adapters/{adapter_id}/evaluations/{evaluation_id}` (Phase 12.2 paired evaluation)
- `POST /departments/{department_id}/adapters/{adapter_id}/evaluations/{evaluation_id}/cancel` (Phase 12.2 paired evaluation)
- `PATCH /departments/{department_id}/adapters/{adapter_id}/review`
- `POST /departments/{department_id}/adapters/{adapter_id}/promote`
- `POST /departments/{department_id}/adapters/rollback`
- `GET /departments/{department_id}/adapter-deployment`

If implemented, the remaining routes expose safe metadata only. There must be no adapter
weight upload or download, raw manifest or configuration download, host path,
tensor metadata disclosure, arbitrary model selector, or arbitrary adapter
selector endpoint. A URL `department_id` remains a selector; server-side
membership resolution is the authorization boundary.

## Reconciliation, retention, and purge

Phase 12.1E-B provides the administrator-only dry-run-by-default purge command
with strict bounded limits. It registers a durable operation, reservations, and
items before filesystem mutation, preserves exact attempt ownership, deletes
the registry final before the source final, supports crash-resumable deletion,
binds exact tombstone identities before unlink, and produces one
operation-level exactly-once success audit. Phase 12.1E-A remains the separate
non-authoritative reconciliation foundation described below; Phase 12.1E-C is
the completed lifecycle-release scope and does not change reconciliation
ownership or artifact cleanup.

Metadata, lineage, evaluation, deployment, and audit history are retained. A
purge must not delete Phase 10 datasets or Phase 11 training-job bundles, and it
must not claim deletion from backups, Google Drive history, or other retained
copies. PostgreSQL and external storage remain non-atomic throughout recovery.

Import-source reconciliation is a separate surface from registry-artifact
reconciliation. Phase 12.1E-A uses the following external layout:

```text
adapters/.deleting/source_stage/<department_id>/<source_bundle_id>/<operation_item_id>/
adapters/.deleting/source_final/<department_id>/<source_bundle_id>/<operation_item_id>/
adapters/.deleting/registry_stage/<department_id>/<adapter_id>/<operation_item_id>/
adapters/.deleting/registry_final/<department_id>/<adapter_id>/<operation_item_id>/
```

The Phase 12.1E-A command is dry-run by default. Before apply mutation it
durably registers the operation and exact source-bundle/attempt or adapter/
registry-attempt item, inspects the original through descriptor-relative
no-follow handles, persists the verified identity and pre-rename move intent,
then reopens and compares the exact original before a no-replace tombstone move
and parent fsync. It commits exact tombstone identities before unlink, never
adopts an unbound tombstone, and remains crash-resumable. It emits one
operation-level success audit only after every applicable surface and tombstone
is absent for the exact attempt across all operations. Phase 12.1E-B uses the
separate `.purge-deleting` namespace and independent final-byte purge authority
documented above; Phase 12.1E-C is separate metadata-only lifecycle release.

Source cleanup must not delete a source still required by an active intake,
retry, or reconciliation operation; must never delete registry artifacts through
a source-cleanup operation; and must never delete Phase 10 or Phase 11
artifacts. A failed or incomplete marker is not ownership authority: only exact
metadata plus the private UUID-derived source path and descriptor identity can
authorize cleanup. Unsafe or foreign paths remain blocked without deletion.

## Acceptance criteria for Phase 12

Phase 12 as a whole is complete only when tests prove that:

- source intake accepts only the two payload files, creates a server-generated
  intake manifest, and consumes only a committed immutable source bundle;
- intake is eligible only for the exact approved, succeeded, unpurged Phase 11
  job and its complete immutable Phase 10/11 authority snapshot;
- external adapter artifacts are validated through a closed immutable contract;
- exact Phase 10 and Phase 11 governance lineage is preserved without claiming
  trusted training provenance;
- publication is private, descriptor-bound, immutable, and recoverable;
- invalid, foreign, malformed, substituted, oversized, or full-model artifacts
  fail closed;
- evaluation evidence cannot cross departments, adapters, or versions;
- unapproved adapters cannot be promoted;
- each department has at most one active deployment;
- upstream Phase 10/11 purge is fenced by adapter retention dependencies;
- rollback-retention references can be removed through a reviewed pre-purge
  mutation, while upstream dependencies release only after registry and source
  bytes are both committed purged;
- import-source lifecycle, exact one-source/one-adapter consumption, and
  source-only reconciliation/purge are separate from adapter lifecycle;
- historical metadata references survive artifact purge without retaining bytes,
  while active and rollback-retention references fence deletion;
- a registry-only deletion cannot mark an adapter purged while any bound import
  source remains;
- the manifest records verified governance lineage and declared external
  training association, not proven training provenance;
- model-free static validation fails closed without loading a model or tokenizer
  using the reviewed Phase 12.1A package references and numeric limits;
- promotion and rollback are transactional at the metadata boundary, versioned,
  and auditable without claiming PostgreSQL/filesystem atomicity;
- runtime routing cannot cross departments and never silently falls back;
- PostgreSQL, APIs, audits, logs, and browser state contain no adapter bytes or
  sensitive content;
- reconciliation and purge are crash-resumable;
- normal CI downloads no real models or adapters; and
- Phase 13 remains unstarted.

Until these criteria are met, an adapter is not available for runtime use.

## Phase 12.1C hardening notes

Migration `0011_phase12_adapter_registry` binds the reverse source claim and
the exact source, Phase 11, and Phase 10 publication attempts with restrictive
composite foreign keys. `AdapterUpstreamDependency` points to the exact
adapter/job/dataset snapshot. SQL and ORM checks freeze all model, package,
artifact, profile, tensor, hash, size, and governance contracts; the two
dataset declarations are copied only from the approved Phase 11 job.

The parent verifies every retained Phase 11 file and its manifest before and
after child execution. Only `manifest.json` is sent to the child. Intake
manifest bytes are hash- and size-bound, including the exact imported-by
identity. The child compares actual configuration and safetensors summaries,
then copies model bytes opaquely without deserializing values.

Claim operations carry an evolving version snapshot for every authority row and
throttle heartbeats to a lease fraction. Reclaim requires one exact prior active
attempt, preserves its manifest as historical metadata, and never adopts its
stage or final. Phase 10 and Phase 11 deletion checks lock and recheck active
dependencies before filesystem mutation. Phase 12.1C intentionally has no
registry-stage/final deletion helper; stale registry surfaces wait for Phase
12.1E.

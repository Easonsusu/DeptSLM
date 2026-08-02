# Phase 12.1C immutable adapter registry publication

Phase 12.1C is the reviewed, department-scoped publication boundary for one
external LoRA or QLoRA adapter. It is implemented on the existing branch under
review; Phase 12.1D and later adapter lifecycle work has not started. This
phase does not evaluate, approve, promote, load, route, reconcile, purge, or
execute training.

## Authority and eligibility

An administrator may dry-run or enqueue one exact source bundle and one exact
Phase 11 job for the same `DepartmentScope`. Apply mode requires:

- a committed, unpurged Phase 12.1B source with no existing adapter claim;
- a succeeded, approved, unarchived, unpurged Phase 11 job;
- the exact succeeded Phase 11 publication attempt and its immutable Phase 10
  dataset authority;
- matching caller-supplied row versions and a server-generated adapter ID;
- explicit confirmation of the declared external training association; and
- a fixed lowercase 40-hex code revision supplied by the worker configuration.

The transaction captures the complete content-free source, Phase 11, and
Phase 10 governance snapshot. It claims the source, creates one adapter row,
one registry-attempt row, and one active upstream-retention dependency. The
source remains `claimed` until the final publication transaction changes it to
`consumed`.

The local administrator command is `python -m app.admin enqueue-adapter-registry`.
It is dry-run by default and accepts only the department, actor, exact source/job
IDs, expected versions, explicit `--confirm-declared-training-association`, and
optional `--apply`; model, adapter, path, manifest, and attestation inputs are not
accepted.

## Worker and child boundary

The one-shot or polling worker has PostgreSQL access, read-only source imports,
read-only Phase 11 bundles, and read-write registry staging/final storage. It
has no model cache, Qdrant, RAG runtime, upload, extraction, evaluation, export,
credential, proxy, or application-auth mounts. The image contains no model,
tokenizer, LlamaFactory, Qdrant, or Hugging Face dependency and performs no
network download.

The parent opens and retains exact private descriptors for both immutable input
bundles. A fixed child receives only the source configuration, source model,
source intake-manifest, and Phase 11 `manifest.json` descriptors, bounded sizes,
UUIDs, the closed content-free source/governance snapshots, and a secret-free
allowlist environment. It validates the Phase 12.1A configuration and
safetensors header without deserializing tensors, verifies the source and Phase
11 manifest digests against the captured snapshots, writes a canonical
configuration, and copies the opaque model bytes in bounded chunks. The other
four Phase 11 descriptors remain parent-side evidence. Adapter bytes never
cross the IPC frame or enter PostgreSQL, logs, or temporary files.

The parent supervises framed IPC with nonblocking writes and an independent
monotonic deadline for each bounded operation,
heartbeats, shutdown/claim-loss checks, child-group termination, and reaping.
No request or vector is spooled to disk.

## Immutable registry artifact

The exact private final directory is:

```text
DEPTSLM_DATA_DIR/
  adapters/registry/<department_id>/<adapter_id>/
    manifest.json
    adapter_config.json
    adapter_model.safetensors
```

Staging is a private UUID path under `adapters/.staging/registry`; its marker
is housekeeping only and is never ownership authority. Publication uses
descriptor-relative no-follow operations, private `0700` directories and
`0600` files, exclusive creation, fsync, and same-filesystem no-replace rename.
The final directory is rechecked by descriptor identity, its closed manifest is
parsed, and all three file digests and sizes are recomputed before PostgreSQL
publication. An unknown or substituted path is never deleted or overwritten.

The manifest is closed and content-free. It includes only UUIDs, contract and
code revisions, positive sizes, SHA-256 values, fixed tensor aggregates,
governance snapshots, and the booleans `declared_external_training_association`
and `training_provenance_verified=false`. It contains no tensor values, dataset
records, prompts, examples, model output, user identity, path, credential, or
runtime setting.

## Final authority and failure behavior

After final descriptor verification, a short PostgreSQL transaction revalidates
the live claim, exact source ownership, active dependency, and complete result
snapshot. It records the registry digests, marks the adapter `validated`, marks
the registry attempt `succeeded`, consumes the source, and appends one
transactional `adapter.registry.publish` success audit. A failure, claim loss,
shutdown, timeout, database error, or incomplete artifact never creates a
completion-success audit or a validated adapter.

PostgreSQL and external storage are not transactionally atomic. If a process
dies after a filesystem rename but before the success commit, the bytes are not
runtime or evaluation authority; later reconciliation/purge handling is a
separate reviewed phase. No public adapter API or frontend surface is added.

## Authority hardening

The worker opens the exact source and Phase 11 descriptor chains once and keeps
them retained through every checkpoint. Before the child starts, the parent
hashes and size-checks all five Phase 11 files (`manifest.json`,
`training.yaml`, `dataset_info.json`, `train.jsonl`, and `validation.jsonl`)
against the current `TrainingJob` row and the closed Phase 11 manifest. Only the
Phase 11 `manifest.json` descriptor is passed to the child; the other four files
remain parent-side evidence. The intake manifest is bound by its exact digest,
byte size, imported-by identity, UUIDs, contract values, and retained descriptor
identity.

The child captures actual model-free configuration and safetensors summaries
(dtype, tensor count, total elements, payload bytes, PEFT and format) and
compares them with the immutable source snapshot. It reads tensor data only as
bounded opaque bytes while copying; no tensor values, model, tokenizer, or
adapter runtime is loaded.

Every lease renewal and lifecycle transition compares an evolving adapter,
attempt, source-attempt, Phase 11 attempt, Phase 10 attempt, and dependency
version snapshot. Heartbeats renew no more often than one third of the lease.
Expired claims cannot mutate or publish. Reclaim marks exactly one matching
previous attempt as `reclaimed`, allocates a fresh attempt, and never adopts an
old stage or final. Phase 10 and Phase 11 retention fences recheck active
registry dependencies immediately before deletion. Registry-stage deletion is
deliberately not implemented; stale registry stages and finals remain untouched
for a future reconciliation phase.

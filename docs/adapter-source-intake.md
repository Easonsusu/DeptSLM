# Phase 12.1B adapter source intake

Phase 12.1B is under review. It adds one administrator-only local CLI for
validating and privately storing an externally produced adapter source. It does
not create an adapter registry record, bind a Phase 11 job, evaluate, approve,
promote, load, reconcile, or purge an adapter. Phase 12.1C through 12.4 and
Phase 13 remain unstarted.

## Command boundary

The command is dry-run by default:

```text
python -m app.admin import-adapter-source \
  --department-id <department-uuid> \
  --actor-issuer <issuer> \
  --actor-subject <subject> \
  --adapter-config <path-to-adapter_config.json> \
  --adapter-model <path-to-adapter_model.safetensors> \
  [--apply]
```

Only the two exact payload files are accepted. The actor must be an active
same-department `system_admin` or `department_admin`; `system_admin` has no
cross-department bypass. Dry-run opens the source descriptors, runs the fixed
model-free Phase 12.1A validator, and streams bounded bytes for digest and size
verification without creating database rows, IDs, directories, or files.

Dry-run output is limited to validation status, the fixed base-model display ID,
dtype, tensor count, and aggregate tensor bytes. Applied output contains only
the generated source UUID, department UUID, and committed status. Paths,
digests, configuration, tensor names, credentials, and exception details are
never printed or persisted in PostgreSQL, audits, or logs.

## Validation and storage

The child receives only the two already-open descriptors, their established
positive sizes, and the fixed contract identifiers. It uses bounded framed IPC,
an exact secret-free environment, `close_fds`, `pass_fds`, a new process group,
and a deadline with deterministic termination/reaping. It parses the complete
bounded JSON configuration and only the safetensors prefix plus declared
header; tensor payload bytes never cross IPC and are never deserialized.

The parent streams each retained descriptor in bounded chunks to hash and copy
it into an exclusive `0600` private stage. It generates the canonical,
sorted-key, compact, LF-terminated `intake_manifest.json`; external manifests
and attestations are never trusted. The final allowlist is exactly:

```text
adapters/imports/<department_id>/<source_bundle_id>/
  intake_manifest.json
  adapter_config.json
  adapter_model.safetensors
```

Incomplete work is staged below the exact UUID-derived `.staging/imports`
surface. Directories are `0700`, files are `0600`, all operations are
descriptor-relative and no-follow, publication is same-filesystem no-replace,
and written files and parent directories are fsynced. A fixed marker is only
incomplete-stage housekeeping; metadata, the UUID path, and retained
descriptor identity are the ownership boundary. Missing or incomplete marker
bytes do not make an otherwise exact private stage authoritative or trusted.

## PostgreSQL authority and crash boundaries

Migration `0010_phase12_adapter_sources` creates only
`adapter_import_sources` and `adapter_import_attempts`. The source and attempt
rows are registered before filesystem mutation, transition through the reviewed
staging/validation/publication states, and commit only content-free contracts,
digests, sizes, tensor summary, UUID ownership, and server timestamps. A
successful final transaction reauthorizes the exact department, locks both
rows, checks the retained post-rename directory and file identities, marks the
source `committed`, points it to the exact attempt, and appends one safe
mutation-success audit.

PostgreSQL and external storage are not atomic. A failure after registration or
publication leaves exact metadata and an exact stage/final surface for the
future reconciliation subphase; uncertain final bytes are never deleted by
exception handling. A committed source is not approved, evaluated, promoted,
runtime-usable, or proof of external training provenance.

## Explicit non-goals

This phase adds no API route, browser upload, adapter download, worker service,
registry table, Phase 11 binding or retention dependency, evaluation, review,
approval, promotion, deployment, runtime loading, purge, or reconciliation.
It adds no PEFT, Transformers, PyTorch, safetensors, or LlamaFactory dependency,
does not download a model, and does not load or materialize tensor values.

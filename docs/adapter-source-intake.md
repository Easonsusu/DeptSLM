# Phase 12.1B adapter source intake

Phase 12.1B is completed. It adds one administrator-only local CLI for
validating and privately storing an externally produced adapter source. It does
not itself create an adapter registry record, evaluate, approve, promote, load,
reconcile, or purge an adapter. Phase 12.1C now consumes this exact committed
source only through its separate reviewed worker, which binds it to one
approved succeeded Phase 11 job and captured Phase 10 authority before
publishing the private registry artifact. Phase 12.1D now adds only the
metadata-only PostgreSQL registry read boundary. Phase 12.1E-A now provides
separate artifact reconciliation only; it does not purge this source or release
dependencies. Phase 12.1E-B/C through 12.4 and Phase 13 remain unstarted.

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
incomplete-stage housekeeping and is never stored in PostgreSQL, the manifest,
audits, or the published authority. It is nevertheless required to be the
exact private `0600` one-link service-owned marker before publication; missing,
substituted, symlinked, hard-linked, wrongly permissioned, or extra-entry
stages fail closed without rename. Metadata, the UUID path, and retained
descriptor identity remain the ownership boundary for incomplete-stage
cleanup.

The exact validated-byte authority is two complete digest passes around child
validation, with positive size, regular-file, owner, mode, link-count, and
descriptor identity checks. Each stage copy verifies the destination digest and
size against that authority, rechecks the retained source descriptor, and
performs a final source digest pass. Before rename, the exact marker and the
three-file allowlist are checked; the marker is unlinked and the stage is
fsynced. After rename, each retained descriptor is fully hashed once, and every
final directory entry is checked descriptor-relatively against that descriptor.
The retained file authority includes device, inode, mode, UID, link count, size,
mtime nanoseconds, ctime nanoseconds, and digest; the final directory authority
also includes its identity, permissions, link count, mtime, and ctime. The final
PostgreSQL transaction repeats the entry allowlist, retained-descriptor, and
file/directory timestamp checks without hashing. A same-inode, same-size
post-hash mutation therefore fails as `adapter_source_authority_changed` and
cannot publish a committed source or success audit.

## PostgreSQL authority and crash boundaries

Migration `0010_phase12_adapter_sources` creates the source tables. Migration
`0011_phase12_adapter_registry` adds the source claim columns and registry
authority tables; it is documented separately in
[adapter-registry-publication.md](adapter-registry-publication.md). The source and
`adapter_import_sources` and `adapter_import_attempts`. The source and attempt
rows are registered before filesystem mutation, transition through the reviewed
staging/validation/publication states, and commit only content-free contracts,
digests, sizes, tensor summary, UUID ownership, and server timestamps. A
successful final transaction reauthorizes the exact department, locks both
rows, checks the retained post-rename directory entries and file descriptors,
including mtime/ctime authority, marks the source `committed`, points it to the
exact attempt, and appends one safe mutation-success audit. Any entry addition,
removal, rename, exchange, substitution, hard link, or timestamp drift leaves
the source and attempt uncommitted; uncertain final bytes are retained for
future reviewed reconciliation.

Every transition captures a frozen authority snapshot containing the complete
department/source/attempt IDs, versions, statuses, actor, code revision,
contract/model/package values, digest and size fields, tensor summary, and
ownership manifest. Later transitions reauthorize, lock the same rows in a
deterministic order, compare the complete expected snapshot, increment both
versions exactly once, and return the next snapshot. Static contract failures
remain the fixed Phase 12.1A codes; descriptor and operational failures use
the separate `adapter_input_invalid`, `adapter_input_unsafe`, and
`adapter_source_changed` boundary.

PostgreSQL and external storage are not atomic. A failure after registration or
publication leaves exact metadata and an exact stage/final surface for the
future reconciliation subphase; uncertain final bytes are never deleted by
exception handling. A committed source is not approved, evaluated, promoted,
runtime-usable, or proof of external training provenance.

## Explicit non-goals

This phase adds no API route, browser upload, adapter download, registry worker,
Phase 11 binding or retention dependency, evaluation, review, approval,
promotion, deployment, runtime loading, purge, or artifact reconciliation. The
separate Phase 12.1E-A command does not change the source-intake CLI contract.
It adds no PEFT, Transformers, PyTorch, safetensors, or LlamaFactory dependency,
does not download a model, and does not load or materialize tensor values.
Migrations `0010_phase12_adapter_sources`, `0011_phase12_adapter_registry`, and
`0012_phase12_adapter_reconciliation` freeze their reviewed SQL literals and do not
import mutable application contract code. CI upgrades to the latter head,
creates a private temporary adapters root, and runs the complete PostgreSQL,
Qdrant, worker, storage, frontend, artifact-policy, and infrastructure checks;
normal CI downloads no model weights.

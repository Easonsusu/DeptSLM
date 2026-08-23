# Phase 14 training execution contract

> Phase 11 currently generates an immutable reviewable LlamaFactory job bundle.
> It does not execute training.
>
> Phase 12 can intake and govern an externally produced adapter. That lineage
> currently proves governance association, not trusted training execution.
>
> Phase 14 introduces a future reviewed supervised execution boundary.
>
> Phase 14.0 itself still performs no training.

This document is the authoritative design contract for Roadmap v2 Phase 14.
It defines the authority, threat model, isolation, storage, lifecycle, and
review gates that must exist before any real training is allowed. It adds no
database schema, service, endpoint, worker, dependency, Compose service, or
runtime behavior.

## 1. Phase 11 is the only input authority

A future execution may originate only from one exact, same-department Phase 11
`TrainingJob` that is:

- `succeeded`, explicitly approved, and not purged;
- the authoritative final publication owner of exactly one succeeded attempt;
- backed by the exact closed five-file manifest and its retained file identity,
  digest, and size evidence;
- bound to the complete Phase 10 authority snapshot already captured by Phase
  11; and
- bound to one reviewed training profile, base-model identity and revision, and
  LlamaFactory contract version.

The exact Phase 11 input surface remains:

```text
manifest.json
training.yaml
dataset_info.json
train.jsonl
validation.jsonl
```

The execution authority freezes every content-free Phase 11 authority field
before training begins, including department, job and publication-attempt
identities, versions, review state, Phase 10 snapshot, profile, model, and
contract fields. It never authorizes from a client path, arbitrary YAML or JSON,
browser content, caller-supplied model/repository, CLI flags, environment
variables, failed/queued/running or stale jobs, purged jobs, or a Phase 10
dataset without an approved Phase 11 job.

## 2. Preserved training semantics

The reviewed semantic contract remains:

- LlamaFactory `0.9.5`;
- base model `Qwen/Qwen3-0.6B`;
- immutable revision `c1899de289a04d12100db370d81485cdf75e47ca`;
- profiles `phase11-qwen3-0.6b-lora-v1` and
  `phase11-qwen3-0.6b-qlora-nf4-v1`;
- SFT, `qwen3_nothink`, thinking disabled, `cutoff_len` 8192, deterministic
  seeds, `packing=false`, `neat_packing=false`,
  `enable_liger_kernel=false`, `use_unsloth=false`, and
  `trust_remote_code=false`.

QLoRA retains only the reviewed NF4/bitsandbytes settings. Phase 14.0 does not
invent torch, Transformers, PEFT, Accelerate, TRL, datasets, or bitsandbytes
versions. Phase 14.2 must establish a separate reviewed training dependency
lock; the Phase 12 runtime stack is not assumed compatible with LlamaFactory.

### Semantic configuration versus execution configuration

Phase 11 is a portable review bundle, not a blindly executable runtime file.
Before execution, a future control plane may rematerialize a closed
execution-only configuration from the verified semantic configuration. Only a
pre-approved substitution set may differ, such as server-owned local model,
dataset, output, log, cache, report-disablement, and temporary directories.
The exact set must be fixed before Phase 14.2.

Learning rate, epochs, LoRA rank or targets, quantization, deepspeed, resume
state, callbacks, scripts, remote reporting, remote dataset/model loaders,
`push_to_hub`, arbitrary flags, shell fragments, and all other training
semantics must remain identical or be rejected. No shell command is assembled
from job data.

## 3. Two future execution boundaries

```text
PostgreSQL + exact Phase 11 authority
              |
              v
  training-execution worker / control plane
              |  closed, content-free request
              v
  private training runtime / data plane
              |  closed result envelope
              v
  verified output -> explicit Phase 12 intake (future 14.3)
```

### Control plane

The future worker owns PostgreSQL authority, exact Phase 11 authorization,
execution lifecycle, leases, cancellation, retention fences, descriptor
verification, bounded input-snapshot preparation, runtime requests, and final
authority registration. Its credentials are PostgreSQL and, only if needed, a
private training-runtime token.

It must not receive Qdrant credentials, API signing secrets, base RAG runtime
tokens, adapter-runtime or adapter-evaluation tokens, or Hugging Face tokens.

### Data plane

The private runtime executes one exact prepared attempt, supervises one fixed
training child, writes only private scratch/log/output areas, and returns a
closed result envelope. It receives no PostgreSQL URL, Qdrant URL/key, API or
membership configuration, adapter registry/deployment authority, RAG,
evaluation, or production adapter runtime tokens.

The future runtime has no public port, Docker socket, host networking, or
normal internet egress; a read-only root filesystem where compatible,
`cap_drop=ALL`, `no-new-privileges`, bounded private tmpfs/process count, a
server-owned runtime token, an exact read-only model-cache mount, and one
server-created attempt area are required. Phase 14.0 does not add this service
to Compose.

## 4. Private input snapshot

Before the runtime is started, a future control-plane worker will freeze and
verify the exact Phase 11 final, copy only the five reviewed files into a
private execution-attempt input snapshot, and verify that copy. The runtime
sees only this server-created snapshot, not the Phase 11 tree or an arbitrary
pathname. The snapshot is non-authoritative until its matching PostgreSQL
execution attempt exists.

The runtime request cannot contain absolute paths, `../`, caller-relative
paths, alternate filenames, symlinks, hard links, extra files, or source
content. Every path component is server-derived canonical UUID state and every
authority-sensitive read is descriptor-relative and no-follow.

## 5. Future storage contract

Phase 14 reserves the following conceptually; Phase 14.0 creates no directory:

```text
DEPTSLM_DATA_DIR/training_runs/
  <department_uuid>/<execution_uuid>/
    attempts/<attempt_uuid>/
      input/ scratch/ logs/ output_stage/
    final/
```

Only canonical server UUIDs may form path components. Directories are private,
symlinks and hard-link adoption are rejected, and filesystem existence never
grants authority. Attempt scratch is not a successful result and raw logs are
not PostgreSQL authority. All runtime data remains outside Git and derives from
`DEPTSLM_DATA_DIR`; physical directory creation is deferred to Phase 14.1.

The authoritative final surface is limited to `adapter_config.json`,
`adapter_model.safetensors`, and one DeptSLM-generated content-free execution
manifest. Trainer state, optimizer state, checkpoints, tokenizer copies,
TensorBoard events, model cards, arbitrary JSON, and third-party reports are
not automatically authoritative and require a future cleanup policy.

## 6. Model and network boundary

Normal execution uses only the server-owned, locally prepared model path for
`Qwen/Qwen3-0.6B` at revision
`c1899de289a04d12100db370d81485cdf75e47ca`. It uses local-files-only behavior
and `trust_remote_code=false`. Model preparation remains an explicit
administrator operation through the existing reviewed model-preparation
boundary. Normal execution has no Hugging Face token and must enforce offline
behavior equivalent to `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, and
`HF_DATASETS_OFFLINE`. No automatic download is permitted.

LoRA requires an explicitly validated training environment; there is no silent
device fallback. QLoRA NF4 requires a reviewed CUDA/bitsandbytes environment.
Unsupported hardware fails before training, with no fallback to LoRA, CPU,
changed precision, or removed quantization. macOS/MPS and NVIDIA/CUDA support
are not claimed until Phase 14.2 records exact validated environment
fingerprints.

## 7. Process supervision

The future supervisor uses one exact executable and fixed argv grammar, in the
spirit of `llamafactory-cli train <server-owned-execution-config>`, but Phase
14.0 never executes it. `shell=True`, `eval`, `exec`, arbitrary command strings,
and inherited environments are forbidden. The child runs in a dedicated
process group/session with unrelated descriptors closed and a sanitized,
server-owned environment.

Startup and wall-clock deadlines, bounded stdout/stderr and log bytes,
bounded filesystem output and process count, disk-full handling, child exit
classification, cancellation, shutdown, complete process-tree kill, and
bounded reap are mandatory. A hung, killed, timed-out, claim-lost, or
cancelled child can never become `succeeded`. Child exception text and training
examples never enter PostgreSQL or public responses, and credentials never
enter logs.

## 8. Logs and output authority

Training logs are private external runtime artifacts only. They are not
PostgreSQL authority, public API or browser content, audit-event content, or
Git content. They need explicit byte bounds, retention and purge rules before
real execution; external telemetry/reporting is disabled. PostgreSQL may later
retain only status, fixed error code, timestamps, attempt/profile enums, safe
exit classification, versions, and content-free digest/size metadata.

A zero exit code is insufficient. Before final publication, the exact adapter
pair must pass the existing Phase 12.1A model-free static adapter validator;
Phase 14.3 must reuse that validator rather than fork or weaken it. A
successful execution is still not an Adapter, committed Phase 12 source,
evaluation, approval, promotion, deployment, or runtime authority.

## 9. Supervised provenance and fingerprints

A future successful execution may claim only:

> DeptSLM's reviewed local training executor recorded that this exact execution
> attempt consumed the verified server-created snapshot of the specified Phase
> 11 bundle and the pinned prepared base-model authority, and produced the
> verified output surface under the reviewed execution contract.

This is not cryptographic or hardware attestation, certified provenance, proof
against host/runtime compromise, proof of model quality, or proof that training
improved the model. Evaluation remains Phase 12.2 and approval remains Phase
12.3.

The future immutable execution fingerprint binds the execution-contract
version, complete Phase 11 and captured Phase 10 authority, profile,
LlamaFactory version, model identity/revision, input-snapshot manifest digest,
training dependency-lock fingerprint, environment profile, and relevant code
revision. Control plane, runtime, output manifest, and final PostgreSQL state
must agree before success.

## 10. Proposed lifecycle and retention fences

Proposed states are `queued`, `running`, `succeeded`, `failed`,
`cancel_requested`, and `cancelled`; Phase 14.0 adds no schema. Only a
same-department `system_admin` or `department_admin` may eventually enqueue or
cancel; read scope requires a separate review. There is no system-admin
cross-department bypass, automatic scheduler, automatic retry, automatic
adapter intake, evaluation, approval, promotion, rollback, or fallback. An
explicit retry creates a new attempt, and one exact job/profile may have at
most one active execution under a future reviewed uniqueness rule.

Queued or running execution must fence purge and authority-invalidating
mutation of its exact Phase 11 job. A succeeded execution with retained output
must retain the exact Phase 11 job and upstream authority until the output is
explicitly purged or a reviewed Phase 12 handoff consumes and retains the exact
lineage. Phase 11 purge followed by a surviving Phase 14 result with silently
broken provenance is forbidden.

## 11. Crash and non-atomicity model

Future implementation must handle registration before snapshot, partial input,
complete snapshot before request, runtime start versus durable `running`, child
death, successful exit before output validation, partial output, validation
before rename, rename before PostgreSQL success, cancellation/exit races, lease
loss, host crash, and publication interruption. PostgreSQL and filesystem
publication are not atomic; filesystem presence alone never proves success.
Unknown output is never adopted automatically. Later reconciliation must be
descriptor-bound and authority-driven and is not part of Phase 14.0.

## 12. Closed future runtime envelopes

The conceptual control-plane request contains only server-generated,
content-free fields: contract version; department, execution, attempt, and
training-job IDs; Phase 11 publication identity; input fingerprint; profile;
model display ID and immutable revision; execution profile; and server-owned
attempt namespace. It contains no examples, arbitrary YAML, paths, argv,
environment, identity/roles, database/Qdrant/API credentials, or deployment
authority. A closed result reports `process_ready`, `execution_started`,
`execution_succeeded`, `execution_failed`, or `execution_cancelled`, with exact
IDs, input/runtime fingerprints, a safe fixed error code, and content-free
output descriptor metadata. No HTTP or IPC is implemented in Phase 14.0.

## 13. Resource limits awaiting review

Before Phase 14.2, reviewed numeric values are required for maximum concurrent
attempts per runtime, wall-clock and startup/load deadlines, per-attempt disk
and scratch quotas, log and output-file limits, process count, cancellation and
tree-reap timeouts, and supported hardware fingerprints. No arbitrary
production placeholder is defined here. Existing Phase 12 adapter-file limits
may be reused only where they govern the same final files.

## 14. Explicit non-goals

Phase 14.0 does not install or invoke LlamaFactory, load a model/tokenizer,
download weights or datasets, create adapters, add a runtime, queue, worker,
API, UI, migration, dependency, Docker/Compose service, or Phase 10/11/12
behavior. It does not alter adapter evaluation, governance, routing, intake,
approval, promotion, rollback, or retention. Phase 14.1 and Phase 15 have not
started, and no production-readiness claim is made.

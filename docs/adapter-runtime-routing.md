# Phase 12.4 adapter runtime routing

Phase 12.4 is completed. Phases 12.0 through 12.3 are complete. This phase activates the reviewed
department deployment pointer for the existing one-turn endpoint:

`POST /departments/{department_id}/rag/answers`

The browser still sends only a question. It cannot select an adapter, model,
runtime, prompt, sampling setting, deployment, or fallback.

## Immutable admission target

At admission the API first takes the canonical department authorization fence,
then resolves the exact deployment pointer and all of its stored governance
IDs in one short PostgreSQL transaction. It creates the existing run, one
content-free `rag_answer_runtime_snapshots` row, and `rag.answer.start` audit
together. There is no snapshot for historical runs created before this phase.

An absent pointer is implicit base (`deployment_id = NULL`, deployment version
zero). An explicit reviewed base pointer is also base, but has positive
deployment and row versions. An adapter target contains the exact adapter,
review, Phase 12.2 evaluation and suite, registry attempt/publication/number/
execution scope, manifest/config/model digest and size, dependency, base model,
and deployment authority. The snapshot is immutable and content-free. Its
fingerprint is SHA-256 over the canonical closed server-owned target.

Adapter admission resolves only those exact IDs. It never asks for a latest
review or evaluation. The adapter must remain validated and unpurged; review
must remain approved and unarchived; evaluation must be succeeded, uncancelled,
gate-passed, and have exactly one baseline and candidate evidence row; the
active suite, registry publication, dependency, base revision, and all Phase
12 governance lineage must still match. Active purge authority, released
dependencies, stale versions, and malformed authority fail closed.

## Retrieval stays Phase 7

Query embedding always uses the existing Phase 7 base runtime, embedding model,
Qdrant collection, department filter, retrieval authority, candidate limits,
evidence loader, prompt, answer contract, citation lexer, insufficient-
information result, and final source reauthorization. Adapter deployment changes
generation only. No public search, selector, query-vector, or runtime-control
route exists. Phase 9 and Phase 12.2 continue to receive their explicit base
and evaluation runtimes and never consult the deployment pointer.

With no authorized evidence, the API returns the existing insufficient-
information result without invoking either generation runtime. This is not a
base fallback.

## Routed generation

For a base snapshot, generation calls only the existing `rag-runtime`. For an
adapter snapshot, generation calls only the private `adapter-runtime`; a timeout,
disconnect, load error, target mismatch, malformed response, or unavailable
service becomes the generic grounded-answer failure. The API never retries with
base, mutates the deployment pointer, enqueues rollback, or invokes rollback
automatically. An administrator may use the existing explicit rollback-to-base
operation later.

The API keeps the Phase 7 `DEPTSLM_RAG_REQUEST_TIMEOUT_SECONDS` contract for
the base runtime (30 seconds by default). Adapter generation has a separate
server-owned `DEPTSLM_ADAPTER_RUNTIME_REQUEST_TIMEOUT_SECONDS` envelope,
bounded to 450–600 seconds and defaulting to 450 seconds. That envelope covers
the adapter runtime's 300-second target-load clock, 120-second generation clock,
and a fixed transport margin. HTTP connect, write, and pool setup remain
short-bounded; no public request field can select or extend either timeout.

The production request is a closed internal contract containing the target
authority, contract version, fingerprint, bounded question and labeled evidence,
prompt version, and answer contract version. It accepts no seed, sampling
controls, path, model override, PEFT configuration, client metadata, or
department selector. A successful response includes only a content-free
`served_target_fingerprint` in addition to the normal answer contract; the API
compares it and strips it before validation/publication.

## Production runtime boundary

`adapter-runtime` is a separate service and security domain from both
`rag-runtime` and `adapter-eval-runtime`. It has no host port and joins only
`adapter-prod-internal`. Its only storage mounts are read-only `model_cache` and
`adapters/registry`, plus a private `0700` `/tmp/adapter-runtime` tmpfs. It has
a read-only root filesystem, drops all capabilities, and uses
`no-new-privileges`. It receives no database, Qdrant, API-auth, evaluation,
upload, extraction, dataset, training-bundle, export, registry-write,
Hugging Face, cloud, or proxy credentials. Its bearer token is distinct from
both the base RAG and evaluation-runtime tokens. Base API availability does not
depend on adapter-runtime health.

The service verifies the exact registry final through no-follow descriptor
chains. It requires the three fixed files, private ownership and modes, exact
department/adapter path, manifest authority, base revision, publication and
attempt identity, config/model digests and sizes, canonical Phase 12.1A config,
and the complete reviewed safetensors header contract. It copies bytes through
retained descriptors into a private ephemeral directory and verifies the copy
before PEFT sees it. The manifest descriptor remains open for the complete
operation: its identity, directory entry, canonical digest, and parsed
authority are revalidated after config/model copying and before success. A
manifest or final-directory substitution fails closed and removes the private
copy. The external registry path is never handed to PEFT.

The fixed model contract is `Qwen/Qwen3-0.6B` at revision
`c1899de289a04d12100db370d81485cdf75e47ca`, local files only,
`trust_remote_code=false`, safetensors only, the existing Phase 7 tokenizer and
context contract, and the existing sampling values (thinking disabled, 512
new-token reserve, temperature 0.7, top-p 0.8, top-k 20, min-p 0.0). PEFT
`0.18.1`, Transformers `4.55.0`, and safetensors `0.7.0` are pinned. The
runtime calls `validate_generation_model_store(data_dir)` once per target load
and passes that exact returned `generation.path` to both the tokenizer and base
model loaders; the `model_cache` root is never treated as a model directory.
Fake mode is test-only and still runs the same target/static verification and
target-ready transition; normal workers never download weights.

## Process and cache isolation

One production child serves one target at a time. Startup reaches only
`process_ready`; a separate supervised `load_target` operation must complete
registry verification, private-copy verification, model-store validation,
tokenizer/base/PEFT loading, and context checks before the child records
`target_ready(loaded_target_fingerprint)`. Only that loaded session state may
produce `served_target_fingerprint`; request bytes cannot manufacture it. The
fixed clocks are 30 seconds for process/IPC startup, 300 seconds for target
loading, and 120 seconds for generation. An exact-key request may reuse the
child. Any change to department, adapter, deployment, publication, execution
scope, digest, size, or other target authority retires and reaps the old
process group before a clean replacement is loaded. A failed replacement does
not keep the prior adapter serving and leaves no orphan child or private copy.
IPC is bounded and framed, descriptors are closed, the child has an exact
secret-free environment, and timeout, cancellation, disconnect, protocol,
load, generation, and shutdown paths use SIGTERM/SIGKILL escalation plus
guaranteed reaping.

## Migration downgrade compatibility

Revision `0017_phase12_adapter_runtime_routing` extends the content-free
`rag_answer_runs.error_code` allowlist. Its downgrade maps
`adapter_runtime_timeout` to `runtime_timeout`; `adapter_runtime_unavailable`,
`adapter_load_failed`, `adapter_runtime_target_mismatch`, and
`deployment_authority_changed` to `runtime_unavailable` before recreating the
Phase 7 constraint. The mapping is deterministic and data-safe for populated
databases; no follow-up migration is required.

## Deployment changes and purge

The request keeps its admission snapshot even when A is promoted to B or rolled
back to base while it is running; the next request sees the new pointer. The
completion transaction reauthorizes the caller, revalidates all supplied source
authority, and locks/rechecks the exact immutable snapshot. It does not require
the current pointer to remain unchanged.

Phase 12.1E-B registration and finalization fence a running adapter-target run
whose exact snapshot names the adapter/version. This fence remains effective
after the deployment pointer moves away and rollback retention is released. A
terminal answered, insufficient-information, or failed run no longer blocks by
itself. Phase 12.1E-C remains metadata-only lifecycle release.

The exact current deployment is an E-B registration-time fence: an
administrator cannot register purge authority for the deployed adapter and
version. Every existing purge operation also revalidates its immutable
original target version before registry-byte movement, and finalization checks
that version again. Once deployment has moved away and rollback retention and
other byte-retention references are released, a running Phase 12.4 request is
the remaining E-B fence. No automatic fallback or rollback is introduced.

PostgreSQL, external files, and runtime processes are not one atomic system.
An already in-flight request cannot be retroactively fenced by PostgreSQL, and
activated bytes without a committed succeeded authority remain untrusted.
There is no automatic fallback or rollback, and no claim that deployment
improves answer quality; evaluation gates are evidence rather than proof.

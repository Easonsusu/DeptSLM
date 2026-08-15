# Phase 12.2 adapter-target evaluation

Phase 12.2 is the completed, administrator-only evaluation boundary. It
compares one exact validated adapter with the exact Phase 7 base lane for one
immutable department-scoped Phase 9 evaluation suite. The phase produces
evidence only; review, approval, promotion, rollback, and runtime routing are
separate future phases.

## Paired production policy

The evaluator delegates retrieval, PostgreSQL authority checks, selected-chunk
loading, prompt construction, generation-response validation, citation checks,
metrics, and quality gates to the existing Phase 7/9 policy. Each case performs
one retrieval and one transient context preparation. Baseline and candidate
lanes receive the same selected evidence, prompt contract, and deterministic
case seed. Candidate-minus-baseline deltas use `Decimal` arithmetic only. No
baseline-relative tolerance, LLM judge, semantic grader, or automatic
promotion is implemented.

## Candidate boundary

The candidate is identified only by server-owned department, adapter, version,
registry publication attempt, manifest digest, file digests, and sizes. The
private runtime revalidates the exact registry allowlist and Phase 12.1A
contract through descriptor-relative no-follow handles, copies bytes into a
private ephemeral directory, and loads the pinned `Qwen/Qwen3-0.6B` base at
revision `c1899de289a04d12100db370d81485cdf75e47ca` with PEFT `0.18.1`,
Transformers `4.55.0`, and safetensors `0.7.0`. Normal workers are offline;
the fake provider is test-only. Load failure never falls back to the base lane.

The runtime receives no database URL, Qdrant credentials, application auth
secret, Hugging Face token, proxy, cloud credential, or arbitrary path. It is
an internal service with a separate token, a killable child process, bounded
framed IPC, and no public route.

## Metadata and external results

PostgreSQL retains only department-scoped run, attempt, evidence, and case
metadata: IDs, statuses, counts, fixed contract versions, Decimal metrics,
gate results, safe error codes, and artifact digest/size authority. Questions,
accepted answers, prompts, evidence, generated answers, vectors, filenames,
paths, model output, and adapter bytes are never persisted or logged.

The worker publishes exactly these private external files beneath
`DEPTSLM_DATA_DIR/eval_results/adapter_runs/<department>/<evaluation>`:

* `manifest.json`
* `summary.json`
* `case_results.jsonl`

Publication uses a private UUID staging path, complete allowlist/digest checks,
same-filesystem rename, and post-rename verification. A failed attempt is
cleaned only by its exact publication attempt. PostgreSQL succeeded state and
the verified result manifest remain the authority; PostgreSQL and external
storage are not transactionally atomic.

## Authorization, leases, and purge fencing

Create/cancel operations require same-department administrator authorization;
metadata reads are closed and department-scoped. Enqueue captures the exact
validated registry attempt, active retention dependency, immutable suite, and
Phase 10/11 lineage. The worker uses PostgreSQL server-time leases, fresh
claim/publication UUIDs after reclaim, cancellation, heartbeats, and stale
claim denial. Finalization rechecks requester membership, adapter/registry
authority, suite/source authority, and the absence of an active E-B purge
before writing numeric evidence and one completion audit.

Evaluation registration and finalization are fenced against active adapter
purge using the same department-first locking boundary. Purge may not mutate
an adapter while an evaluation is queued or running, and an evaluation may not
start while purge authority is active. A late filesystem or network failure
cannot be treated as a successful evaluation; uncommitted external artifacts
remain untrusted and are cleaned only within their exact attempt scope.

## Explicit non-goals

Phase 12.2 does not train models, invoke LlamaFactory, create or modify
adapters, approve or promote adapters, route production requests through an
adapter, add a public evaluation/search/RAG endpoint, persist evaluation
content, add an LLM judge, or change feedback, retrieval, prompt, generation,
or quality-gate behavior. Phase 12.3 now provides separate review,
promotion, rollback, retention, and deployment-event metadata authorities; it
still does not load or route adapters. Phase 12.4 runtime routing remains
unstarted.

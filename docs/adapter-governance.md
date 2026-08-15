# Phase 12.3 adapter governance

Phase 12.3 is the reviewed control-plane boundary between completed Phase 12.2
adapter evaluation and the future Phase 12.4 runtime router. It records human
decisions and explicit deployment operations only. It does not load an adapter,
change RAG retrieval, or route a production request through an adapter.

## Separate authorities

`Adapter.status` remains the Phase 12.1 artifact/publication lifecycle:
`queued`, `running`, `validated`, `validation_failed`, `failed`,
`purge_pending`, and `purged`. Governance never adds `approved`, `promoted`,
`superseded`, or `rejected` to that column and never increments `Adapter.version`
for a decision.

The migration `0016_phase12_adapter_governance` adds five metadata-only
authorities:

- `adapter_reviews` binds a department, immutable adapter version, exact Phase
  12.2 evaluation, baseline/candidate evidence, suite, registry publication,
  upstream dependency, fixed contracts, and result digests. Its states are
  `pending`, `approved`, `rejected`, and `archived`.
- `department_adapter_deployments` stores one explicit department pointer.
  An absent pointer means the base model; an adapter pointer includes the
  approved review and evaluation version.
- `adapter_deployment_operations` is the explicit promote, adapter-rollback,
  or base-rollback queue. It captures the complete reviewed authority snapshot,
  uses PostgreSQL server-time leases, and can be reclaimed only by a fresh
  exact claim.
- `adapter_deployment_events` is append-only, content-free supersession and
  rollback history. It records before/after targets, the approved review and
  evaluation when the target is an adapter, and no prompts, answers, bytes, or
  model output.
- `adapter_rollback_retentions` keeps an exact approved adapter/evaluation
  reference active until it is explicitly released or reactivated by a reviewed
  rollback. Release is a separate metadata mutation.

## Review and deployment rules

Starting a review requires one succeeded, uncancelled Phase 12.2 evaluation,
the exact same-department validated adapter, active suite/dependency, succeeded
registry attempt, and every stored digest, size, contract, code-revision, and
execution-scope field. Approval requires the existing candidate quality and
safety gate to pass. There is no new percentage, delta, or regression threshold;
baseline/candidate deltas remain evidence for the human reviewer.

Approval never promotes. Administrators must enqueue an explicit promotion or
rollback with optimistic adapter, review, retention, and deployment versions.
The worker locks the department first, checks the current deployment version,
revalidates the complete PostgreSQL snapshot, verifies the exact private
registry-final allowlist and digests through descriptor-bound handles, and only
then writes the deployment pointer and one success event. PostgreSQL remains
the authority; filesystem publication is not transactionally atomic.

## Isolation and retention

All review, deployment, operation, event, and retention reads and writes carry
the path department and resolve current membership on the server. `system_admin`
has no cross-department bypass. The governance worker receives only PostgreSQL
and a read-only `adapters/registry` mount. It contains no model, tokenizer,
Qdrant, RAG-runtime, upload, extraction, evaluation-result, dataset, training,
adapter-write, cloud-credential, or application-auth configuration.

Phase 12.1E-B purge fences active governance operations and references, while
Phase 12.1E-C release rejects active or reappeared governance authority. A
retained adapter is never deleted by a deployment worker; release is explicit,
audited, and still does not remove registry bytes, backups, or historical audit
events. Phase 12.4 runtime loading, request snapshots, fail-closed routing, and
explicit runtime rollback remain unstarted.

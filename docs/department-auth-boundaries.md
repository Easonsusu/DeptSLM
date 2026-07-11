# Department and Authentication Boundaries

## Status

Phase 2 implements reusable authentication and fail-closed authorization foundations. Persistent memberships, department CRUD, production identity integration, and product APIs remain deferred.

## Security objective

Every department-owned operation must authenticate the caller, derive allowed departments from trusted membership data, and enforce one explicit `department_id`. A client-provided identifier is a resource selector, not authorization. Missing, invalid, or ambiguous department scope must fail closed.

## Roles

| Role | Intended authority |
| --- | --- |
| `system_admin` | Operate platform-wide configuration and approved support workflows. Cross-department access must be explicit, audited, and limited to a defined administrative purpose. |
| `department_admin` | Manage one department's settings, memberships, approved sources, jobs, evaluations, adapters, and exports. |
| `instructor` | Use and curate approved department knowledge, review grounded answers, and perform explicitly granted content or evaluation actions. |
| `student` | Query approved department assistants and use resources made visible to students. No administrative, ingestion, training, or adapter-management authority. |
| `viewer` | Read explicitly shared department resources without mutation authority. |

Roles grant permissions only within active memberships. A role name must never create access to a department that is absent from the authenticated membership set.

## Membership model

A future membership must bind one user to one department with:

- non-null `user_id` and `department_id`
- one reviewed role
- active, suspended, or revoked status
- creation, update, and optional expiry timestamps
- actor and reason metadata for role or status changes

The database should prevent duplicate active memberships for the same user and department. Authorization must use current server-side membership state rather than cached client claims alone. Removing or suspending membership must invalidate future access promptly.

## Department-scoped access rules

1. Authenticate before resolving department context.
2. Resolve allowed departments from active membership.
3. Validate the requested department against that set.
4. Pass an immutable authorization context to repositories, jobs, storage helpers, and model workflows.
5. Require `department_id` in every department-owned query and mutation.
6. Include department scope in constraints, indexes, vector filters, paths, job payloads, cache keys, logs, and exports.
7. Reject cross-department joins, fallback indexes, datasets, adapters, caches, and exports.
8. Re-authorize asynchronous work when it is queued and before consequential execution or delivery.

## APIs requiring department checks

All future endpoints involving these resources require authenticated membership-derived department scope:

- departments, settings, memberships, invitations, and roles
- documents, uploads, versions, extraction, chunks, and deletion
- ingestion and re-indexing jobs
- embeddings, Qdrant collections or points, retrieval, and citations
- conversations, messages, feedback, and saved prompts
- training datasets, examples, reviews, and exports
- training jobs, logs, checkpoints, evaluations, and cancellation
- adapters, approval, promotion, routing, and rollback
- audit events, reports, dashboards, and generated exports

System-level health and version endpoints may remain unscoped only when they expose no department data or dependency secrets.

## Data requiring department isolation

The following must carry a non-null `department_id` where department-owned: memberships, documents, document versions, extracted text, chunks, embeddings, vector payloads, jobs, conversations, messages, feedback, datasets, examples, adapters, evaluations, logs, audit events, caches, and exports.

PostgreSQL constraints and indexes should include department ownership. Qdrant operations must always apply a department payload filter. File paths beneath `DEPTSLM_DATA_DIR` must use validated department segments and prevent traversal or symlink escape.

## Visibility rules

### Documents

- A document is visible only to active members of its owning department with a role permitted by its publication state.
- Draft, quarantined, failed, retired, or restricted documents require narrower permissions than approved sources.
- Students and viewers may access only explicitly published material.
- Source metadata and citations must not reveal another department's names, paths, titles, or identifiers.

### Training datasets

- Datasets are private to the owning department and limited to authorized operators and reviewers.
- Each example must retain approved source provenance and review status.
- Dataset export, duplication, or use in a job requires authorization for the same department.
- Cross-department aggregation is prohibited unless a separately reviewed system-level workflow defines consent, de-identification, and audit controls.

### Adapters

- Adapters are visible and routable only within their owning department unless a separately approved platform artifact is explicitly classified as global.
- Selection requires matching department, base-model revision, approval state, and compatibility metadata.
- Missing adapters must fall back only to an approved base model, never to another department's adapter.
- Promotion, rollback, deletion, and export require department-admin or explicitly delegated authority and an audit event.

## Audit requirements

Audit records should capture:

- actor, authenticated subject, effective role, and department
- action, resource type, stable resource identifier, and outcome
- timestamp, request or job correlation identifier, and originating service
- authorization decision and policy reason without exposing credentials or source content
- membership, visibility, dataset, training, adapter, export, and administrative changes

Audit events must be append-oriented, access-controlled, retained under a reviewed policy, and protected from cross-department discovery. `system_admin` access and denied cross-department attempts require explicit audit coverage.

## Risks when filtering is missing

Missing or inconsistent `department_id` enforcement can expose confidential documents, citations, conversations, student data, training examples, model behavior, logs, exports, or adapters across departments. It can also poison retrieval, train on unauthorized content, route requests to the wrong adapter, leak information through caches or errors, and make deletion or retention obligations impossible to satisfy.

UI filtering, client claims, path naming, and model prompts are not security boundaries. Authorization must be enforced server-side at every storage and service boundary.

## Phase 2 implementation boundary

The API now validates development/test HS256 bearer tokens, exposes safe identity metadata through `GET /auth/me`, and supplies immutable department scope and authorization context types. Runtime membership resolution deliberately denies every department request until Phase 3 provides persistent server-side memberships. Focused test-only routes exercise department and role dependencies; no department product endpoint has been added.

## Acceptance criteria for Phase 2

- Authentication produces a server-validated user identity.
- Active membership determines allowed departments and roles.
- An immutable request authorization context carries one explicit department scope.
- Database access helpers require department scope and reject unscoped calls.
- Cross-department create, read, update, delete, list, search, job, cache, log, and export attempts fail.
- Qdrant and file-path helpers cannot operate without a valid department filter or segment.
- Role permissions are tested for all five roles, including suspended and revoked memberships.
- System-admin cross-department actions are narrowly defined and audited.
- Audit events cover successful and denied security-sensitive actions without logging source content or secrets.
- Tests prove tenant enumeration and direct-object-reference attempts fail.
- Documentation identifies deferred SSO, session, recovery, invitation, and production identity-provider decisions.

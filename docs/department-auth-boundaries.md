# Department and Authentication Boundaries

## Status

Phase 2 implemented reusable authentication and fail-closed authorization foundations. Phases 3–5 added persistent departments, documents, extraction jobs, and chunks. Phase 6 applies the same boundary to vector-indexing jobs and all Qdrant operations. Production identity integration and later product data APIs remain deferred.

## Security objective

Every department-owned operation must authenticate the caller, derive allowed departments from trusted membership data, and enforce one explicit `department_id`. A client-provided identifier is a resource selector, not authorization. Missing, invalid, or ambiguous department scope must fail closed.

## Roles

| Role | Intended authority |
| --- | --- |
| `system_admin` | Reserved for future platform operations. Phase 3 treats it only as a same-department administrative membership and provides no cross-department bypass. |
| `department_admin` | Manage one department's settings, memberships, approved sources, jobs, evaluations, adapters, and exports. |
| `instructor` | Use and curate approved department knowledge, review grounded answers, and perform explicitly granted content or evaluation actions. |
| `student` | Query approved department assistants and use resources made visible to students. No administrative, ingestion, training, or adapter-management authority. |
| `viewer` | Read explicitly shared department resources without mutation authority. |

Roles grant permissions only within active memberships. A role name must never create access to a department that is absent from the authenticated membership set.

## Membership model

Phase 3 persists each membership as a binding between one identity and one department with:

- non-null `user_id` and `department_id`
- one reviewed role
- active, suspended, or revoked status
- creation, update, and optional expiry timestamps
- actor and reason metadata for role or status changes

The database prevents duplicate identity/department memberships. Authorization uses current server-side membership state rather than cached client claims. Revoking, suspending, expiring, or demoting a membership, suspending or revoking its identity, or archiving its department invalidates subsequent access.

## Department-scoped access rules

1. Authenticate before resolving department context.
2. Resolve allowed departments from active membership.
3. Validate the requested department against that set.
4. Treat request selectors and earlier contexts as hints only; revalidate current authority in the database transaction before resource access or mutation.
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

- Phase 4 metadata is visible to any active role only inside its owning department. There is no document-content or download endpoint.
- Upload requires same-department `system_admin`, `department_admin`, or `instructor`; soft deletion requires same-department `system_admin` or `department_admin`.
- Admission is checked before streaming and current authority is checked again under a department lock before finalization.
- Deleted metadata is hidden, retained source bytes still count against quota, and cross-department identifiers return no resource data.
- Publication states and narrower student/viewer content visibility are deferred until content delivery exists.
- Source metadata and citations must not reveal another department's names, paths, titles, or identifiers.
- Phase 5 extraction enqueue/retry requires same-department `system_admin`, `department_admin`, or `instructor`; all five roles may read safe status/provenance metadata. Every query uses department, document, and extraction scope; APIs expose no extracted/chunk text, internal hashes, paths, claims, or worker identity.
- Workers carry explicit department/document IDs, revalidate active document ownership at finalization, and cannot publish after lease/claim loss. Composite database constraints prevent cross-department extraction or chunk assignment.
- Phase 6 indexing enqueue/retry uses the same curator roles; all five active roles may read content-free status. Every PostgreSQL query uses exact department/document/extraction/indexing scope, and every Qdrant operation requires typed `DepartmentScope` plus an internally constructed exact department filter.

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

Phase 3 emits safe process-level `AuditSink` events for authentication and transaction-time authorization decisions, including denied and unavailable decisions. Successful mutations separately append PostgreSQL `audit_events` rows atomically with the state change. Denied, unavailable, and no-op operations do not create mutation-success rows. Phase 3 does not claim persistent denied-event storage or tamper-resistant production audit storage.

Future production audit storage must be append-oriented, access-controlled, retained under a reviewed policy, and protected from cross-department discovery. Any future system-admin support access and denied cross-department attempts require explicit audit coverage.

## Risks when filtering is missing

Missing or inconsistent `department_id` enforcement can expose confidential documents, citations, conversations, student data, training examples, model behavior, logs, exports, or adapters across departments. It can also poison retrieval, train on unauthorized content, route requests to the wrong adapter, leak information through caches or errors, and make deletion or retention obligations impossible to satisfy.

UI filtering, client claims, path naming, and model prompts are not security boundaries. Authorization must be enforced server-side at every storage and service boundary.

## Phase 2 implementation boundary

The API validates development/test HS256 bearer tokens, exposes safe identity metadata through `GET /auth/me`, and supplies immutable department scope and authorization context types. These Phase 2 boundaries remain the base for persistent Phase 3 authorization.

## Phase 3 persistence boundary

Server-side membership resolution requires exact issuer, opaque subject, and path department matching. Resource services repeat this check in their request session, and mutations lock the department first before revalidating the actor and locking targets. Each production-route authorization attempt emits one safe process decision after current membership and role state is known. Stale authorization after archival, suspension, revocation, expiry, or demotion fails without a success audit row. Effective-administrator checks join active identities and memberships and serialize changes per department. Scoped repository methods always include `department_id`; cross-department membership IDs appear not found. Department creation is restricted to a reviewed local bootstrap command, public APIs cannot grant `system_admin`, and final effective administrators cannot be removed transactionally. Production SSO, platform administration, and product data remain deferred.

## Phase 4 document boundary

Document repositories require `DepartmentScope` and always filter `department_id`. Upload admission uses a short session; streaming holds no database transaction. Finalization starts a new transaction, locks the department first, repeats exact identity/membership/role validation, and serializes retained-byte quota decisions. Fixed-field process events distinguish admission, finalization, validation, storage, database, read, and delete decisions without recording filenames, headers, bodies, hashes, or paths. Successful upload/delete mutations append `document.upload` or `document.delete` persistent audit rows in the same transaction as metadata.

## Phase 5 extraction boundary

Extraction and chunk repositories require `DepartmentScope` plus document/extraction selectors. Enqueue/retry repeats transaction-time membership checks and appends success audit rows with the queued attempt. Workers use database-owned department/document IDs, verify source integrity, and finalization locks department, document, then extraction before checking document state, source identity, pipeline, lease, and claim. Composite foreign keys reject cross-department metadata even if application filtering fails. Public schemas exclude content, paths, hashes, requestor/worker identity, and claims.

## Phase 6 vector boundary

Indexing repositories require `DepartmentScope` plus exact document/extraction/indexing selectors. Composite `RESTRICT` keys prevent cross-department jobs. The Qdrant adapter accepts no raw scope string, optional scope, client filter, or collection; attempt operations add exact indexing/vector-attempt filters and search adds current pipeline plus publication. Returned IDs/payload are validated and future retrieval cross-checks succeeded indexing, stored document, succeeded extraction, and exact chunk ownership in PostgreSQL. No system-admin cross-department bypass or public search exists.

Process logs contain fixed action/result/reason plus department/job IDs only. Transactional success rows are `document.vector_index.enqueue`, `document.vector_index.retry`, and `document.vector_index.complete`; model, artifact, Qdrant, claim, cleanup, and database failures never receive completion-success rows. Chunk text, vectors, hashes, filenames, paths, URLs, keys, SQL, and authentication values are excluded.

## Phase 7 grounded-answer boundary

All five active same-department roles may request one grounded answer. The URL department is a selector only: admission uses current exact-issuer membership, and completion reauthorizes in a new transaction before success. Qdrant search always uses typed scope plus exact `department_id`, current pipeline, and `published=true`; each candidate is cross-checked in PostgreSQL. Final citations must still belong to stored documents, succeeded extractions, current succeeded indexings, and exact chunks in that department. Cross-department, deleted, stale, malformed, or revoked state fails closed without resource enumeration. `system_admin` has no bypass.

Transactional `rag.answer.start` records only content-free admitted-run metadata. `rag.answer.complete` is written only with an applied answered or insufficient result. Questions, answers, prompts, evidence, vectors, hashes, paths, raw model output, tokens, and dependency details are excluded from PostgreSQL, audit, and logs.

## Phase 8 feedback boundary

All five active roles may submit structured feedback only when their exact identity owns the completed same-department RAG run. Reviewer list/read/transition requires active same-department `system_admin`, `department_admin`, or `instructor`; purge requires same-department `system_admin` or `department_admin`. Every operation repeats membership resolution inside its database transaction, and `system_admin` has no cross-department bypass. Foreign or expired objects fail without disclosing existence.

Feedback rows, reasons, and source targets carry exact non-null department/run scope. Targets must reference a persisted citation from that same scope. Public contracts expose no submitter or reviewer identity, content, filename, document/chunk/indexing metadata, or infrastructure detail. Feedback is immutable; review uses constrained forward-only transitions and optimistic versions. Expiry, visibility, and purge use PostgreSQL server time. Purge is explicit and bounded, deletes children before parents, preserves runs/citations, and appends transactional content-free audits. Feedback code has no Qdrant, artifact, extracted-text, RAG runtime, or model access and cannot alter retrieval, prompts, generation, evaluation, or training.

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

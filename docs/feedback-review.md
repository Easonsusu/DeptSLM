# Feedback Review

The Phase 8 reviewer workflow exposes structured, content-free feedback metadata to active `system_admin`, `department_admin`, and `instructor` memberships in the exact department. Students and viewers cannot use reviewer endpoints. `system_admin` has no cross-department bypass, and reviewer self-review is intentionally not prohibited in this phase.

## Queue and reads

`GET /departments/{department_id}/rag/feedback` lists non-expired records oldest first by `(created_at, id)`. Optional exact `status` and `sentiment` filters and a limit from 1 through 100 are supported. Pagination uses an opaque cursor bound to the department, selected filters, and ordering; offset pagination and cross-filter cursor reuse are rejected.

`GET /departments/{department_id}/rag/feedback/{feedback_id}` returns one non-expired same-department record. Foreign and expired IDs return a safe 404. Responses contain only the run identifier, answer outcome, sentiment, reviewed reason codes, public source labels, workflow state, reviewed resolution, timestamps, and version. They contain no identity, question, answer, source text, filename, or document metadata.

## Transitions

`PATCH /departments/{department_id}/rag/feedback/{feedback_id}` requires `expected_version` and permits:

- `open` to `triaged`, `resolved`, or `dismissed`
- `triaged` to `resolved` or `dismissed`

Resolved and dismissed records are terminal. Triaged records have no resolution code. Resolved codes are `confirmed_quality_issue`, `confirmed_safety_issue`, `addressed_externally`, and `no_action_required`; dismissed codes are `duplicate`, `not_reproducible`, `out_of_scope`, and `no_issue_found`. Backward, no-op, incompatible, expired, or stale-version mutations fail without a success audit. Each applied transition increments the version exactly once, records PostgreSQL server time and the reviewer internally, and appends one transactional `rag.feedback.review` audit event.

The frontend queue is a minimal department view with status and sentiment filters, cursor pagination, safe access-denied and expired states, and only valid structured transitions. It has no free-text notes, identities, content display, analytics, persistence API, or cross-department aggregation.

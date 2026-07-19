# Feedback Retention and Purge

`DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS` controls structured feedback retention. It defaults to 180 and accepts strict ASCII integers from 30 through 730. Signs, decimals, whitespace, Unicode digits, and malformed values fail configuration validation.

PostgreSQL `clock_timestamp()` is the authority for creation, expiry, visibility, review eligibility, and purge eligibility. Host clock skew does not set retention. Review transitions do not extend expiry. At or after `expires_at`, owner reads, reviewer reads, and reviewer queues treat the feedback as unavailable even if its rows have not yet been deleted.

Purge is explicit, authorized, department-scoped, and batch-bounded; it is not a scheduled worker. Run one dry inspection or applied batch with:

```bash
python -m app.admin purge-rag-feedback \
  --department-id <uuid> \
  --actor-issuer <exact-issuer> \
  --actor-subject <opaque-subject> \
  --limit 500

python -m app.admin purge-rag-feedback \
  --department-id <uuid> \
  --actor-issuer <exact-issuer> \
  --actor-subject <opaque-subject> \
  --limit 500 \
  --apply
```

Only an active same-department `system_admin` or `department_admin` may apply or inspect a batch. The command uses oldest-first selection, PostgreSQL server-time eligibility, a limit from 1 through 1000, and `SKIP LOCKED` for concurrent workers. Apply explicitly deletes source targets and reasons before each parent, preserves RAG runs and citations, and appends one transactional `rag.feedback.purge` audit row per deleted parent. A dry run mutates and audits nothing. Repeated runs are idempotent, and database failure rolls back children, parent, and audit together.

Command output reports counts and expiry boundaries only; it never reports reasons, source labels, run content, identities, questions, answers, or evidence. Persistent audit events may outlive the purged feedback record under the separate audit-retention policy.

Local Compose provides no automatic purge and is not a production privacy or retention claim. Production scheduling, policy enforcement, monitoring, legal holds, backup expiry, and audit-retention coordination remain deferred.

# Feedback Retention and Purge

`DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS` controls structured feedback retention. It defaults to 180 and accepts strict ASCII integers from 30 through 730. Signs, decimals, whitespace, Unicode digits, and malformed values fail configuration validation.

PostgreSQL server time is the authority for creation, expiry, visibility, review eligibility, and purge eligibility. Mutations and purge use `clock_timestamp()`. Read eligibility uses the stable `statement_timestamp()` for the same PostgreSQL statement that selects the complete parent/reason/source response. If expiry or purge races a read, the response is either the complete canonical view eligible at that statement snapshot or a safe 404, never a partial record. Host clock skew does not set retention. Review transitions do not extend expiry.

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

Only an active same-department `system_admin` or `department_admin` may apply or inspect a batch. The command loads only `DATABASE_URL` through a narrow `FeedbackPurgeSettings` boundary; it does not construct full API settings and requires no `DEPTSLM_DATA_DIR`, uploads, extracted text, model cache, Qdrant, RAG runtime, model revision, quota, or retention setting. The database URL must use `postgresql+psycopg`.

The service itself rejects non-integers, booleans, and limits outside 1 through 1000 before authorization or SQL construction, independently of argparse. Valid batches use oldest-first selection, PostgreSQL server-time eligibility, and `SKIP LOCKED` for concurrent workers. Apply explicitly deletes source targets and reasons before each parent, preserves RAG runs and citations, and appends one transactional `rag.feedback.purge` audit row per deleted parent. A dry run mutates and audits nothing. Repeated runs are idempotent, and database failure rolls back children, parent, and audit together.

Command output reports counts and expiry boundaries only; it never reports reasons, source labels, run content, identities, questions, answers, or evidence. Persistent audit events may outlive the purged feedback record under the separate audit-retention policy.

Local Compose provides no automatic purge and is not a production privacy or retention claim. Purge does not claim deletion from backups, replicas outside this command's database transaction, or historical audit records. Production scheduling, policy enforcement, monitoring, legal holds, backup expiry, and audit-retention coordination remain deferred.

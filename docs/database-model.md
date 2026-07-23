# Database Model Through Phase 8

DeptSLM uses PostgreSQL 16, SQLAlchemy 2, psycopg 3, and Alembic. Revision `0006_phase8_rag_feedback` follows `0005_phase7_rag_answers`. Alembic is the only schema-creation mechanism; runtime never calls `metadata.create_all`.

## Entities

```mermaid
erDiagram
    USER_IDENTITIES ||--o{ MEMBERSHIPS : has
    DEPARTMENTS ||--o{ MEMBERSHIPS : contains
    USER_IDENTITIES ||--o{ AUDIT_EVENTS : acts
    DEPARTMENTS ||--o{ AUDIT_EVENTS : scopes
    DEPARTMENTS ||--o{ DOCUMENTS : owns
    USER_IDENTITIES ||--o{ DOCUMENTS : uploads
    DOCUMENTS ||--o{ DOCUMENT_EXTRACTIONS : processes
    DOCUMENT_EXTRACTIONS ||--o{ DOCUMENT_CHUNKS : produces
    DOCUMENT_EXTRACTIONS ||--o{ DOCUMENT_VECTOR_INDEXINGS : indexes
    DEPARTMENTS ||--o{ RAG_ANSWER_RUNS : scopes
    USER_IDENTITIES ||--o{ RAG_ANSWER_RUNS : requests
    RAG_ANSWER_RUNS ||--o{ RAG_ANSWER_CITATIONS : cites
    DOCUMENT_CHUNKS ||--o{ RAG_ANSWER_CITATIONS : supports
    RAG_ANSWER_RUNS ||--o{ RAG_ANSWER_FEEDBACK : receives
    USER_IDENTITIES ||--o{ RAG_ANSWER_FEEDBACK : submits
    RAG_ANSWER_FEEDBACK ||--o{ RAG_ANSWER_FEEDBACK_REASONS : classifies
    RAG_ANSWER_FEEDBACK ||--o{ RAG_ANSWER_FEEDBACK_SOURCE_TARGETS : targets
    RAG_ANSWER_CITATIONS ||--o{ RAG_ANSWER_FEEDBACK_SOURCE_TARGETS : references
```

- `user_identities`: UUID identity keyed uniquely by the exact opaque `(issuer, subject)`. Subjects are not lowercased or interpreted as email addresses. Status is `active`, `suspended`, or `revoked`.
- `departments`: UUID department with a unique canonical lowercase slug, display name, lifecycle status, and version. Slugs are immutable through Phase 3 APIs.
- `memberships`: unique `(user_id, department_id)` assignment with one reviewed role, lifecycle status, optional expiry, creator, and version. Security foreign keys use `RESTRICT`, not cascading deletion.
- `audit_events`: append-only application interface for safe mutation metadata. It intentionally has no token, secret, request body, document, training content, or database URL fields.
- `documents`: department-owned source metadata with an internal uploader relation, normalized filename, canonical media type, positive size, SHA-256 digest, lifecycle state, version, and timestamps. It stores no body or path, and public document schemas do not expose internal identity IDs; see [document-model.md](document-model.md).
- `document_extractions`: immutable attempt history and PostgreSQL queue state, including source/pipeline identity, claim lease, safe result metadata, and an allowlisted error code. It stores no content, path, filename, stderr, or exception.
- `document_chunks`: department/document/extraction-scoped offsets, byte size, internal digest, and mutually exclusive page/line provenance. Chunk text remains external.
- `document_vector_indexings`: department/document/extraction-scoped queue/history with the fixed model/vector contract, safe counts/errors, retry relation, claim/lease authority, and no text, vectors, paths, URLs, keys, or raw Qdrant data.
- `rag_answer_runs`: department/requestor-scoped content-free attempt metadata with lifecycle, safe counts/errors, and exact embedding, generation, prompt, and answer-contract versions.
- `rag_answer_citations`: restrictive run/department/document/extraction/indexing/chunk provenance with server label, rank, internal score, ordinal, and page/line range. It stores no answer or evidence.
- `rag_answer_feedback`: one immutable structured submission per department/run/requester, with sentiment, constrained review lifecycle, internal submitter/reviewer relations, PostgreSQL-server-time expiry, optimistic version, and no content.
- `rag_answer_feedback_reasons`: one through five server-ordered reviewed reason identifiers, or zero through four for helpful feedback, with exact parent department/run scope and no free text.
- `rag_answer_feedback_source_targets`: up to eight ordered exact citation references from the same department and run; it duplicates no label, filename, text, score, hash, path, document, extraction, or indexing metadata.

Composite unique and `RESTRICT` foreign-key constraints bind documents, extractions, retries, and chunks to the same department/document. Partial unique indexes allow one active attempt per document and one successful result per source checksum/pipeline. Lifecycle checks make queued, running, succeeded, failed, and cancelled metadata internally consistent.

Departments are archived and memberships are revoked; neither has a hard-delete API. Archived departments, inactive identities or memberships, and expired memberships cannot authorize access. Mutation and audit rows are flushed and committed in the same request transaction.

Issuer and opaque subject values preserve their exact meaningful characters; database constraints reject empty or whitespace-only values. They are never lowercased or reinterpreted.

Document filename checks are defined identically in the SQLAlchemy model and revision `0002_phase4_documents`: the value must contain a non-whitespace character, `char_length` must not exceed 255, and `octet_length` must not exceed 255. The byte constraint prevents a valid character count from exceeding the storage contract when UTF-8 encoded.

## Transaction and administrator invariants

Department reads and mutations revalidate the actor in the request-scoped database session. Mutations lock the active department row first, then the acting identity/membership, then any target identity/membership. This consistent order serializes administrator-changing operations per department and closes stale-context gaps after revocation, suspension, expiry, demotion, or archival.

An effective administrator requires an active department, active `UserIdentity`, active membership, an unexpired membership, and role `department_admin` or same-department `system_admin`. Suspended or revoked identities and inactive or expired memberships do not count. An active department cannot lose its final effective administrator through membership mutation. PostgreSQL row locking covers application mutations; direct out-of-band SQL remains an operational trust boundary.

## Migrations

From `apps/api`, with `DATABASE_URL` set to a `postgresql+psycopg://` URL:

```bash
python -m alembic upgrade head
python -m alembic current
python -m alembic downgrade base  # isolated development/test database only
```

Production migration execution, backup, recovery, and rollback procedures remain deferred. Never point destructive migration tests at a shared or production database. Phase 8 tests require PostgreSQL 16 and exercise `0005` to `0006`, empty-to-head, downgrade/upgrade, repeated-head behavior, ORM synchronization, lifecycle checks, exact feedback/run/citation scope, immutable idempotency, review concurrency, server-time retention, purge, and audit.

`document_vector_indexings` permits at most one queued/running job per extraction and embedding pipeline and one succeeded job per extraction/current model revision/dimension/vector schema. Failed attempts do not block explicit retry. Workers use PostgreSQL server time and `SKIP LOCKED`; an expired claim cannot heartbeat, fail, requeue, activate, or finalize.

For Compose, use `./scripts/compose.sh run --rm api python -m alembic upgrade head`. Its `DATABASE_URL` uses the internal `postgres` hostname; host-shell commands must use `localhost` or another host-accessible address.

Phase 7 deliberately does not persist question text, answer text, prompts, retrieved evidence, raw model output, query vectors, hashes, paths, tokens, or dependency configuration. `rag.answer.complete` and citation rows commit only with an answered or insufficient terminal state. PostgreSQL cannot make Qdrant, external artifact reads, or model inference atomic; final short-transaction revalidation is the acceptance authority.

Phase 8 preserves that prohibition. Feedback tables add no question, answer, prompt, evidence, excerpt, comment, note, vector, model output, filename, path, hash, token, URL, or Qdrant field. Database checks enforce the sentiment/status/resolution allowlists, lifecycle shape, positive version, expiry ordering, and bounded child ranks. Composite restrictive foreign keys bind parent, reason, source target, run, department, and exact citation. Application transactions enforce compatible reasons, contiguous canonical ranks, immutability, reviewer transitions, version checks, and explicit oldest-first purge.

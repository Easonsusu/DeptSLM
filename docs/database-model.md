# Phase 3 Database Model

Phase 3 uses PostgreSQL 16, SQLAlchemy 2, psycopg 3, and Alembic. Alembic revision `0001_phase3` is the only schema-creation mechanism; the API never calls `metadata.create_all`.

## Entities

```mermaid
erDiagram
    USER_IDENTITIES ||--o{ MEMBERSHIPS : has
    DEPARTMENTS ||--o{ MEMBERSHIPS : contains
    USER_IDENTITIES ||--o{ AUDIT_EVENTS : acts
    DEPARTMENTS ||--o{ AUDIT_EVENTS : scopes
```

- `user_identities`: UUID identity keyed uniquely by the exact opaque `(issuer, subject)`. Subjects are not lowercased or interpreted as email addresses. Status is `active`, `suspended`, or `revoked`.
- `departments`: UUID department with a unique canonical lowercase slug, display name, lifecycle status, and version. Slugs are immutable through Phase 3 APIs.
- `memberships`: unique `(user_id, department_id)` assignment with one reviewed role, lifecycle status, optional expiry, creator, and version. Security foreign keys use `RESTRICT`, not cascading deletion.
- `audit_events`: append-only application interface for safe mutation metadata. It intentionally has no token, secret, request body, document, training content, or database URL fields.

Departments are archived and memberships are revoked; neither has a hard-delete API. Archived departments, inactive identities or memberships, and expired memberships cannot authorize access. Mutation and audit rows are flushed and committed in the same request transaction.

Issuer and opaque subject values preserve their exact meaningful characters; database constraints reject empty or whitespace-only values. They are never lowercased or reinterpreted.

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

Production migration execution, backup, recovery, and rollback procedures remain deferred. Never point destructive migration tests at a shared or production database.

For Compose, use `./scripts/compose.sh run --rm api python -m alembic upgrade head`. Its `DATABASE_URL` uses the internal `postgres` hostname; host-shell commands must use `localhost` or another host-accessible address.

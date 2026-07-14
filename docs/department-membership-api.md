# Department and Membership API

Phase 3 exposes persistent department reads and department-scoped administration. Every resource path authorizes the exact `department_id` against the authenticated issuer and opaque subject in the request-scoped database session. Mutations revalidate and lock current authority inside the mutation transaction; a previously valid context is not authorization evidence. An optional `X-Department-ID`, when supplied, must exactly match the path. Database authorization failures return a generic `503`; stale, inactive, or cross-department access returns a non-challenging `403`.

## Endpoints

| Method and path | Access | Behavior |
| --- | --- | --- |
| `GET /departments` | authenticated | Paginated active departments derived from the caller's active, non-expired memberships only. |
| `GET /departments/{department_id}` | any active same-department role | Safe department metadata. |
| `PATCH /departments/{department_id}` | same-department admin | Update display name; slug remains immutable. |
| `DELETE /departments/{department_id}` | same-department admin | Soft archive after exact `confirm_slug`. Reactivation is deferred. |
| `GET /departments/{department_id}/memberships` | same-department admin | Paginated, scoped memberships. |
| `POST /departments/{department_id}/memberships` | same-department admin | Create one membership using the configured token issuer and an opaque subject. |
| `GET /departments/{department_id}/memberships/{membership_id}` | same-department admin | Lookup by both department and membership IDs. |
| `PATCH /departments/{department_id}/memberships/{membership_id}` | same-department admin | Change reviewed role, status, or expiry. |
| `DELETE /departments/{department_id}/memberships/{membership_id}` | same-department admin | Soft revoke. |

Administrative roles are `department_admin` and a `system_admin` membership in the same department. `system_admin` never bypasses membership lookup, and public APIs cannot grant it. An effective administrator also requires an active identity, active department, active membership, and null or future expiry. Department-first PostgreSQL locking serializes admin creation, role/status/expiry changes, and revocation; removing the final effective administrator returns `409`.

Pagination defaults to 25 and accepts `limit` from 1 through 100 plus a non-negative `offset`. Cross-department membership identifiers return `404` within an already authorized department and do not reveal ownership.

An empty membership PATCH or a request combining `expires_at` with `clear_expiry` returns `422`. A valid PATCH that would not change stored role, status, or expiry returns the current row without incrementing its version or writing a success audit event.

## Audit behavior

Every production department-route authorization attempt emits one typed process-level `AuditSink` event after transaction-time membership and role evaluation. Safe outcomes include `active_membership`, `membership_denied`, `role_denied`, and `membership_store_unavailable`. These events never include credentials, request bodies, database connection details, SQL, or department content.

Successful state mutations separately append one PostgreSQL `audit_events` row in the same transaction. Denied or unavailable requests and no-op mutations do not append a mutation-success row. Persistent denied-event storage and tamper-resistant production audit storage are not implemented.

## Local bootstrap

Department creation is deliberately absent from the public API. After applying migrations, run from `apps/api`:

```bash
ENVIRONMENT=development python -m app.admin bootstrap-department \
  --slug computer-science \
  --display-name "Computer Science" \
  --admin-issuer https://local-issuer.invalid \
  --admin-subject opaque-admin-subject
```

`DEPTSLM_DATA_DIR` and `DATABASE_URL` must also be configured. The command is allowed only in explicit `local`, `development`, `dev`, or `test` environments and atomically creates an identity, department, initial `department_admin`, and audit event. It never accepts or prints a bearer token or database URL. Production bootstrap and platform administration remain deferred.

With Compose, use:

```bash
./scripts/compose.sh run --rm api python -m app.admin bootstrap-department \
  --slug computer-science --display-name "Computer Science" \
  --admin-issuer https://local-issuer.invalid --admin-subject opaque-admin-subject
```

## Limitations

Phase 3 has no production identity provider, invitations, global administration, frontend administration UI, hard deletion, or audit-query API. Real university data and production deployment are not approved.

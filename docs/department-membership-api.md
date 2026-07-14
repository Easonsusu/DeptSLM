# Department and Membership API

Phase 3 exposes persistent department reads and department-scoped administration. Every resource path authorizes the exact `department_id` against the authenticated issuer and opaque subject. An optional `X-Department-ID`, when supplied, must exactly match the path. Database authorization failures return a generic `503`; inactive or cross-department access returns a non-challenging `403`.

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

Administrative roles are `department_admin` and a `system_admin` membership in the same department. `system_admin` never bypasses membership lookup, and public APIs cannot grant it. The final active, non-expired administrator is protected with PostgreSQL row locking; removal, suspension, immediate expiry, or demotion returns `409`.

Pagination defaults to 25 and accepts `limit` from 1 through 100 plus a non-negative `offset`. Cross-department membership identifiers return `404` within an already authorized department and do not reveal ownership.

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

## Limitations

Phase 3 has no production identity provider, invitations, global administration, frontend administration UI, hard deletion, or audit-query API. Real university data and production deployment are not approved.

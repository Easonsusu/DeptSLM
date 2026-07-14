# Authentication Foundation

## Implemented model

Phase 2 adds immutable typed models for authenticated principals, UUID department scopes, department authorization contexts, five department roles, and active, suspended, or revoked membership states. `GET /auth/me` returns only the server-validated subject and issuer.

Phase 3 replaces the runtime deny-all resolver with a PostgreSQL-backed resolver. It matches the exact validated issuer, opaque subject, and requested department UUID, and requires active identity, department, and membership rows plus a non-expired membership. Client role or department claims remain untrusted. A `system_admin` still requires same-department membership and has no global bypass. Database errors fail closed with a generic `503`; authentication behavior and the Bearer challenge remain unchanged.

Authentication and membership resolution are separate boundaries. A token proves identity; it does not prove department access. A client-provided department identifier is only a resource selector.

## Development and test JWT mode

`DEPTSLM_AUTH_MODE=hs256` enables an established PyJWT verifier only when `ENVIRONMENT` is explicitly set to `local`, `development`, `dev`, or `test`. Missing, blank, unknown, staging, preview, QA, production, misspelled, and all other environments stop startup with a configuration error. Disabled authentication retains the normal local environment default.

The verifier accepts only HS256 and validates signature, subject, issuer, audience, expiration, and `nbf` when present. Unsigned tokens and other algorithms are rejected.

The required variables are:

- `DEPTSLM_AUTH_ISSUER`
- `DEPTSLM_AUTH_AUDIENCE`
- `DEPTSLM_AUTH_SECRET`

Explicit HS256 selection requires every variable and stops startup if any value is missing or empty. The secret must contain at least 32 UTF-8 bytes and must not match a known placeholder. Generate a local secret without committing it:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

The repository provides no usable default signing secret. Store the generated value only in the untracked local `.env`. HS256 is not a production identity design.

## Runtime membership behavior

Phase 3 stores identities, departments, and memberships in PostgreSQL and exposes production department and membership APIs. Each department route matches the validated token issuer and opaque subject to the path `department_id` inside the request database transaction. It requires an active identity, active department, active non-expired membership, and an allowed same-department role. JWT department or role claims are not authorization evidence.

Mutations lock the department first, then revalidate and lock the actor identity and membership before locking target rows. An earlier dependency result or authorization context is not evidence for the mutation. Suspended or revoked identities, archived departments, suspended, revoked, or expired memberships, role mismatches, and cross-department selectors fail closed. Database or membership-store failures return a generic `503` without exposing connection or query details.

## HTTP behavior

- `/health` and `/version` remain public.
- `/auth/me` requires valid authentication and returns `401` with `WWW-Authenticate: Bearer` otherwise.
- Production department and membership endpoints require one explicit UUID path scope and an active matching server-side membership.
- Missing, malformed, unknown, suspended, revoked, cross-department, and role-incompatible scope returns `403` without confirming resource existence.
- `system_admin` receives no global bypass.

## Audit events

Authentication and transaction-time department authorization decisions emit typed process-level events through `AuditSink`. Events may include actor subject, action, allowed or denied result, a safe reason code, selected department, and a validated UUID correlation identifier. Authorization events are emitted only after current database membership and role state produces an allowed, denied, or unavailable decision.

Separately, successful department and membership mutations append a PostgreSQL `audit_events` row in the same transaction as the state change. Denied requests, unavailable authorization checks, and no-op mutations do not create a mutation-success row. Process events cover those authorization outcomes without claiming persistent denied-event storage.

Neither audit boundary can carry bearer tokens, JWT signatures, auth secrets, raw request bodies, database URLs, SQL statements, hostnames, personal profiles, documents, retrieved sources, or training content. The process logging sink and PostgreSQL success rows are not claimed to be tamper-resistant production audit storage; retention, access, export, and operational hardening remain deferred.

## External storage path safety

Artifact paths use typed areas such as `DEPTSLM_DATA_DIR/uploads/<validated-department-uuid>/...`. The helper requires both `ArtifactArea` and `DepartmentScope` and rejects unsafe path segments. Phase 4 upload I/O additionally uses descriptor-relative no-follow operations so symlinks cannot redirect staging or final sources. Tests use temporary directories and never read or write the real Google Drive folder.

## Future vector scope

The pure `DepartmentVectorScope` helper always produces a mandatory `department_id` payload condition. It has no Qdrant dependency and does not connect to a vector database.

## Threat assumptions and limitations

- Local HS256 secrets must be generated and stored outside Git.
- No token revocation, key rotation, JWKS, OIDC discovery, OAuth flow, session, cookie, or frontend login exists.
- No production identity provider is selected or integrated.
- Development/test HS256 is the only configured identity mode; production identity integration remains deferred.
- Authorization decision events are process logs, while persistent rows cover successful state mutations only. Neither is a complete tamper-resistant production audit system.
- Rate limiting, operational monitoring, and audited system-admin support workflows remain deferred.

Production identity-provider selection must define asymmetric verification, key rotation, issuer metadata, token lifetime, revocation, account lifecycle, and deployment secret management.

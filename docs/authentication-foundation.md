# Authentication Foundation

## Implemented model

Phase 2 adds immutable typed models for authenticated principals, UUID department scopes, department authorization contexts, five department roles, and active, suspended, or revoked membership states. `GET /auth/me` returns only the server-validated subject and issuer.

Authentication and membership resolution are separate boundaries. A token proves identity; it does not prove department access. A client-provided department identifier is only a resource selector.

## Development and test JWT mode

`DEPTSLM_AUTH_MODE=hs256` enables an established PyJWT verifier for local development and tests. It accepts only HS256 and validates signature, subject, issuer, audience, expiration, and `nbf` when present. Unsigned tokens and other algorithms are rejected.

The required variables are:

- `DEPTSLM_AUTH_ISSUER`
- `DEPTSLM_AUTH_AUDIENCE`
- `DEPTSLM_AUTH_SECRET`

The repository contains placeholders only. Incomplete configuration fails closed. HS256 mode is rejected when `ENVIRONMENT` is `production` or `prod`; it is not a production identity design.

## Runtime membership behavior

The runtime membership resolver denies all department access. Tests use an in-memory resolver to verify authorization behavior. Phase 3 will add persistent server-side department and membership models. JWT department or role claims are not used as authorization evidence.

## HTTP behavior

- `/health` and `/version` remain public.
- `/auth/me` requires valid authentication and returns `401` otherwise.
- Department dependencies require one explicit UUID scope and an active matching server-side membership.
- Missing, malformed, unknown, suspended, revoked, cross-department, and role-incompatible scope returns `403` without confirming resource existence.
- `system_admin` receives no global bypass.

Department dependencies are exercised through test-only routes. No department product endpoint exists yet.

## Audit events

Authentication and authorization decisions emit typed events to a safe logging sink. Events may include actor subject, action, allowed or denied result, policy reason, authorized department, and a validated UUID correlation identifier.

The event schema cannot carry bearer tokens, signatures, secrets, raw request bodies, personal profiles, documents, retrieved sources, or training content. Persistent audit storage and retention policy are deferred.

## External storage path safety

Department paths use the form `DEPTSLM_DATA_DIR/departments/<validated-uuid>/...`. The helper requires a `DepartmentScope`, rejects absolute and traversal children, resolves paths before returning them, and rejects symlink escape. Tests use temporary directories and never read or write the real Google Drive folder.

## Future vector scope

The pure `DepartmentVectorScope` helper always produces a mandatory `department_id` payload condition. It has no Qdrant dependency and does not connect to a vector database.

## Threat assumptions and limitations

- Local HS256 secrets must be generated and stored outside Git.
- No token revocation, key rotation, JWKS, OIDC discovery, OAuth flow, session, cookie, or frontend login exists.
- No production identity provider is selected or integrated.
- Memberships are not persistent and runtime department authorization intentionally denies all requests.
- Audit events are process logs, not tamper-resistant persistent records.
- Rate limiting, operational monitoring, and audited system-admin support workflows remain deferred.

Production identity-provider selection must define asymmetric verification, key rotation, issuer metadata, token lifetime, revocation, account lifecycle, and deployment secret management. Persistent membership work belongs to Phase 3.

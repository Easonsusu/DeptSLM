# API

## Status

The Phase 5 API keeps the Phase 4 upload boundary and adds department-scoped extraction enqueue/retry and safe status/provenance metadata. Long-running work is PostgreSQL-backed and runs only in the RAG worker. Extracted text, chunk content, download, Qdrant, RAG, training, and production identity integration remain unavailable.

For the default local configuration, the base URL is:

```text
http://localhost:8000
```

`API_PORT` may change the host port used by Docker Compose.

## Current endpoints

### `GET /health`

Reports whether the API process can answer a basic liveness request.

**Request**

```bash
curl --fail http://localhost:8000/health
```

**Successful response**

- Status: `200 OK`
- Content type: `application/json`

```json
{
  "status": "ok"
}
```

Phase 0 health is intentionally shallow. It does not guarantee that PostgreSQL, Qdrant, model runtimes, workers, Google Drive, or any future dependency is ready. Separate liveness and dependency-readiness checks should be designed before production deployment.

### `GET /version`

Returns the API name and application version.

**Request**

```bash
curl --fail http://localhost:8000/version
```

**Successful response**

- Status: `200 OK`
- Content type: `application/json`

```json
{
  "name": "DeptSLM",
  "version": "0.1.0"
}
```

Clients should treat the fields as informational. Phase 0 does not define compatibility negotiation from this response.

### `GET /auth/me`

Requires a valid bearer token and returns only the validated subject and issuer.

```bash
curl --fail \
  -H "Authorization: Bearer <development-token>" \
  http://localhost:8000/auth/me
```

Successful responses contain `subject` and `issuer`. Missing, malformed, disabled, expired, incorrectly signed, or otherwise invalid authentication returns `401` with `WWW-Authenticate: Bearer`. The response never includes the raw token, secret, audience, membership data, roles, or authentication configuration.

Department-scoped dependencies return `403` for missing, malformed, unknown, inactive, expired, cross-department, or role-incompatible membership scope. Database verification failures return a generic `503`. Neither response receives a Bearer challenge or exposes database details.

Authentication and transaction-time department authorization decisions emit safe process-level `AuditSink` events. Successful state mutations separately append transactional PostgreSQL `audit_events` rows. Denied, unavailable, and no-op operations do not create mutation-success rows; persistent denied-event storage is not implemented.

## Current error behavior

Unknown routes use FastAPI's default JSON `404 Not Found` response. Authentication failures use `401` with a Bearer challenge; authenticated department authorization failures use `403` without that challenge. No project-wide error envelope is defined yet.

Development HS256 startup requires an explicit `ENVIRONMENT` of `local`, `development`, `dev`, or `test`, complete issuer/audience/secret configuration, and a non-placeholder secret of at least 32 UTF-8 bytes. Unknown or missing environments and incomplete settings stop startup.

## Current department endpoints

Phase 3 implements `GET /departments`, same-department `GET/PATCH/DELETE /departments/{department_id}`, and scoped membership list/create/read/update/revoke routes. Department creation uses the local bootstrap command and is not a public endpoint. All lists are paginated and all membership-resource queries include the path department predicate.

## Current document endpoints

- `GET /departments/{department_id}/documents`
- `POST /departments/{department_id}/documents`
- `GET /departments/{department_id}/documents/{document_id}`
- `DELETE /departments/{department_id}/documents/{document_id}`

List/read accepts every active same-department role. Upload accepts same-department `system_admin`, `department_admin`, and `instructor`; soft deletion accepts same-department `system_admin` and `department_admin`. Deleted documents are hidden, source bytes are retained, and there is no global list, search, download, extraction, or indexing endpoint. Upload uses a raw request body and the strict headers, formats, limits, storage layout, and cleanup rules in [document-upload.md](document-upload.md).

`Content-Length` is optional for upload. If supplied, it must be one nonzero ASCII-decimal header, is checked early against the per-file maximum, and must match the streamed byte count. Independent streaming limits still protect requests that omit it. Document create, list, read, and delete responses expose safe metadata only; they never expose uploader/deletion identity IDs, issuer, subject, checksum, or storage path.

Document errors use `403` for authorization, `409` for department quota exhaustion, `413` for the per-file size limit, `415` for unsupported or invalid content, `422` for malformed metadata/filename, empty input, duplicate metadata headers, or length mismatch, and a generic `503` for database or storage unavailability. Successful upload finalization records transactional action `document.upload`; deletion continues to record `document.delete`.

## Current extraction endpoints

- `POST /departments/{department_id}/documents/{document_id}/extractions` (`202`)
- `GET /departments/{department_id}/documents/{document_id}/extractions`
- `GET /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}`
- `GET /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/chunks`
- `POST /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/retry` (`202`)

Enqueue and retry require same-department `system_admin`, `department_admin`, or `instructor`; all five active roles may read metadata. Retry is explicit and allowed only for a failed attempt. Stored documents only are visible, every query includes department/document/extraction scope, and foreign identifiers disclose no resource.

Responses omit requestor/worker identity, claim token, lease, source/normalized/chunk hashes, filename, path, parser stderr, exception text, and all source/normalized/chunk content. Chunk lists require a succeeded extraction and return ordinal, normalized character offsets, UTF-8 byte size, and one page or line range. Successful enqueue/retry record `document.extraction.enqueue` or `document.extraction.retry`; worker publication records `document.extraction.complete`.

## Future API conventions

The following conventions should be decided before business endpoints are introduced:

- stable paths under a version prefix such as `/api/v1`
- authentication and membership-derived department context
- consistent request/response schemas and error envelopes
- pagination, filtering, sorting, and idempotency behavior
- asynchronous job resources for ingestion and training
- upload size, media type, timeout, and rate limits
- audit metadata and request correlation
- deprecation and compatibility policy

OpenAPI generated by FastAPI can be used as a development aid, but generated documentation does not replace an authorization or threat-model review.

## Department isolation requirement

Every future department-owned endpoint must authenticate the caller and resolve allowed `department_id` values from server-side membership. A department identifier supplied in a URL, query, header, or body is only a resource selector; it is not proof of authorization.

Database queries, Qdrant operations, jobs, paths, caches, logs, adapter selection, and exports must all use the same authorized department scope. Missing or ambiguous scope must fail closed. Tests must demonstrate that a caller from one department cannot enumerate, retrieve, modify, train on, query, or export another department's data.

## Planned API sections

Names and paths below are conceptual and may change after contract design.

### Departments and memberships

- department workspace metadata
- membership and role management
- department configuration and lifecycle

### Documents and ingestion

- document upload and metadata
- ingestion job status and errors
- source reprocessing, retirement, and deletion
- department-scoped index status

Uploaded sources are stored beneath `DEPTSLM_DATA_DIR/uploads/<department_id>/<document_id>/source`, not in Git or arbitrary API-server paths. Extracted text remains deferred.

### Chat and retrieval

- department-scoped conversations and messages
- source-grounded query requests
- answer, citation, and retrieval metadata
- explicit insufficient-information outcomes

Retrieved passages are untrusted content. The server must keep them separated from higher-priority instructions and must never let document text alter authorization, tool use, or department scope. If no adequate source is retrieved, the response must say that the system does not have enough information rather than fabricate an answer or citation.

### Training datasets and jobs

- reviewed dataset creation and versioning
- LoRA or QLoRA job creation and monitoring
- cancellation and safe retry behavior
- training configuration and provenance

Long-running work should return a job resource rather than hold an HTTP request open.

### Adapters

- department-scoped adapter registry
- evaluation status
- controlled promotion and rollback
- exact base-model and dataset provenance

An adapter may be selected only for the department that owns it. Cross-department fallback is prohibited.

### Evaluations and exports

- evaluation runs and comparisons
- approved report generation
- export status and access-controlled download

Generated evaluation outputs and exports belong under `DEPTSLM_DATA_DIR`.

## Security considerations for future endpoints

- Authenticate before resolving department context and authorize every object operation.
- Validate file names, sizes, media types, and content; do not trust extensions alone.
- Do not expose internal paths, prompts, model credentials, stack traces, or raw dependency errors.
- Apply output encoding appropriate to the browser or downstream consumer.
- Rate-limit expensive upload, retrieval, model, training, and export operations.
- Record security-relevant audit metadata without copying sensitive source content into logs.
- Define retention and deletion semantics across PostgreSQL, Qdrant, external artifacts, caches, backups, and exports.

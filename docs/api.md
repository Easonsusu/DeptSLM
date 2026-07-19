# API

## Status

The Phase 8 API preserves the completed grounded-answer boundary and adds structured feedback submission and review metadata. Feedback routes use PostgreSQL only and expose no content or identity IDs. There is no public vector search, query-vector API, conversation history, streaming, reranking, evaluation, training, or production identity integration.

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

## Current vector-indexing endpoints

- `POST /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings` (`202`)
- `GET /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings`
- `GET /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings/{indexing_id}`
- `POST /departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings/{indexing_id}/retry` (`202`)

Enqueue/retry accept same-department `system_admin`, `department_admin`, and `instructor`; all five active roles may read safe metadata. A stored document and succeeded extraction are required, duplicate active/current-success jobs return `409`, and retry accepts only failed history. All selectors are matched exactly and foreign IDs do not enumerate resources.

Responses expose the fixed pipeline/model display ID/dimension/distance/schema, counts, state, safe error, attempt number, and public timestamps. They omit requestor/worker IDs, claim/vector-attempt IDs, lease, model revision/path, collection management, Qdrant URL/key, hashes, content, and vectors. API handlers never read artifacts, load models, or call Qdrant. There is no public search or query-vector route. See [vector-indexing.md](vector-indexing.md).

## Current grounded-answer endpoint

- `POST /departments/{department_id}/rag/answers`

All five active same-department roles may submit `{"question":"..."}`; the question is NFC-normalized, trimmed, control-checked, and limited to 2,000 characters. Admission and completion each resolve current server-side membership. Cross-department selectors return `403` without a Bearer challenge, while missing/invalid authentication retains the `401` Bearer challenge.

An answered `200` response contains a run UUID, `status: "answered"`, plain answer text, the fixed generation-model display ID, creation time, and citations with server label, document/chunk IDs, original filename, ordinal, and page/line provenance. An inadequate-evidence result uses `status: "insufficient_information"`, the exact safe message, and an empty citation list. Internal scores, department/extraction/indexing IDs, identities, model revisions, hashes, paths, text chunks, vectors, prompts, raw output, URLs, and credentials are never public.

The endpoint is intentionally synchronous and non-streaming. Dependency or source-state failures return a generic `503`; invalid question shape returns `422`. Questions, answers, prompts, evidence, and vectors are transient and are not stored. See [rag-answering.md](rag-answering.md).

## Current structured-feedback endpoints

- `PUT /departments/{department_id}/rag/answers/{run_id}/feedback`
- `GET /departments/{department_id}/rag/answers/{run_id}/feedback`
- `GET /departments/{department_id}/rag/feedback`
- `GET /departments/{department_id}/rag/feedback/{feedback_id}`
- `PATCH /departments/{department_id}/rag/feedback/{feedback_id}`

All five active same-department roles may submit structured feedback only for their own completed run. A PUT accepts `sentiment`, reviewed `reason_codes`, and public citation `source_ids`; extra keys and arbitrary strings fail validation. First creation returns 201, an identical canonical replay returns 200 without mutation or audit, and a conflicting immutable replacement returns 409. The owner GET requires current active membership and hides expired or foreign feedback with 404.

The reviewer queue and detail/mutation routes require active same-department `system_admin`, `department_admin`, or `instructor` membership. The queue supports exact status/sentiment filters, limit 1 through 100, oldest-first ordering, and an opaque filter-bound cursor. Review PATCH supports only the documented forward transitions and requires `expected_version`; stale or invalid transitions return 409. Expired and foreign records return safe 404. Authentication failures retain the 401 Bearer challenge; authorization 403 responses do not.

Public responses contain feedback/run IDs, answer outcome, sentiment, reason codes, source labels, workflow state, reviewed resolution, timestamps, expiry, and version only. They omit user/reviewer identities, questions, answers, prompts, evidence, filenames, document/chunk/indexing details, hashes, paths, scores, vectors, URLs, database details, and credentials. Feedback does not change RAG behavior and is neither an evaluation result nor training data. See [rag-feedback.md](rag-feedback.md), [feedback-review.md](feedback-review.md), and [feedback-retention.md](feedback-retention.md).

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

### Chat and retrieval beyond Phase 7

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

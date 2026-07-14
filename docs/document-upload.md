# Phase 4 Document Upload

## Endpoint

`POST /departments/{department_id}/documents` accepts the document as the raw HTTP request body. It does not accept multipart form data. The request requires:

- `Authorization: Bearer ...`
- `Content-Disposition: attachment; filename="..."` or an RFC 5987 UTF-8 `filename*`
- exactly one `Content-Type`
- exactly one positive decimal `Content-Length`
- absent or `identity` `Content-Encoding`

Supported pairs are PDF (`.pdf`, `application/pdf`, `%PDF-` signature), UTF-8 text (`.txt`, `text/plain`), and UTF-8 Markdown (`.md` or `.markdown`, `text/markdown` or `text/plain`). Text charset may be absent, `utf-8`, or `us-ascii`; a US-ASCII declaration rejects non-ASCII bytes. Invalid UTF-8, NUL, empty bodies, mismatched types/extensions, unsupported encodings, and Office, archive, HTML, image, or executable uploads are rejected.

Filenames are metadata only. They are normalized to Unicode NFC and never used as paths, URLs, logs, or audit fields. Blank names, dot segments, path separators, NUL/control characters, malformed percent encoding, invalid UTF-8, and names over 255 characters or UTF-8 bytes are rejected.

## Limits

- `DEPTSLM_DOCUMENT_MAX_BYTES` defaults to `26214400` (25 MiB) and cannot exceed `104857600` (100 MiB).
- `DEPTSLM_DEPARTMENT_DOCUMENT_QUOTA_BYTES` defaults to `1073741824` (1 GiB) and must be at least the per-file limit.

Explicit values must be positive ASCII decimals or startup fails. Compose passes these settings only to the API. `Content-Length` is rejected early when too large, while streamed bytes are independently counted and must equal the declaration.

## Authorization and streaming sequence

1. Validate the bearer identity and path/header department selector.
2. Open a short database session for upload admission; require a current allowed membership, then close the session before reading bytes.
3. Validate headers and create an exclusive `0600` staging file beneath the authorized department.
4. Consume `Request.stream()` incrementally. Each chunk is validated, counted, hashed, and written via thread-offloaded blocking I/O. No whole-body, multipart, spooled, named-temporary, process-temp, or checkout-local buffer is used.
5. Flush and `fsync` the staged source.
6. In a new transaction, lock the department, revalidate current authority, enforce quota, atomically rename the source, insert metadata, and append the mutation-success audit row.
7. Commit and return safe metadata without a path or checksum.

Revocation, suspension, expiry, demotion, or department archival during streaming causes final authorization to fail and staging to be removed. Disconnects, cancellation, length/type failures, quota denial, storage errors, and database errors also clean the staged file. Process audit events contain fixed decision fields only—never filenames, disposition headers, bodies, digests, paths, tokens, secrets, or database details.

## External storage layout

```text
DEPTSLM_DATA_DIR/
└── uploads/
    └── <department_uuid>/
        ├── .staging/<upload_uuid>.part
        └── <document_uuid>/source
```

The external `uploads` root must already exist as a writable real directory, not a symlink. Storage operations use descriptor-relative, no-follow, exclusive creation; department and document directories are `0700`, source files are `0600`, finalization is a same-filesystem atomic rename, and existing destinations are never overwritten. Tests and CI create an isolated temporary `uploads` root; they never use the real Google Drive directory.

## Current limitations

Validation is intentionally shallow: PDF validation checks only the signature, and text/Markdown validation checks encoding and NUL. Phase 4 has no malware scanner, archive/Office support, parser, content-disposition compatibility fallback, resumable upload, download endpoint, rate limit, antivirus quarantine, orphan reconciler, or production storage design. Google Drive synchronization is a local-development convenience, not a production object-store guarantee.

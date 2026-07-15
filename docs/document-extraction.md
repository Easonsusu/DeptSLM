# Phase 5 Document Extraction

## Scope

Phase 5 processes stored Phase 4 PDF, UTF-8 text, and Markdown sources outside API request handlers. The API only authorizes enqueue/retry requests and returns safe metadata. PostgreSQL is the queue and history store; the RAG worker verifies, extracts, normalizes, chunks, and publishes external artifacts.

OCR, malware scanning, rendering, embedded-file handling, downloads, Qdrant, embeddings, LlamaIndex, model inference, RAG, and production storage are not implemented.

## Queue and states

`document_extractions` is a department/document-scoped attempt history. Its states are:

- `queued`: no claim or result metadata
- `running`: owned by a random worker UUID and fresh claim token with a finite lease
- `succeeded`: parser, normalized-output, published-output, and chunk-count metadata are complete
- `failed`: finished with one allowlisted error code and no result metadata
- `cancelled`: unavailable before execution, currently because its document was soft-deleted

Only an explicit API retry of a failed attempt creates another row. It points to the failed row, increments `attempt_number`, and never rewrites history. There is no automatic unbounded retry loop.

At most one queued/running attempt exists per document. A current source checksum and pipeline version can have at most one successful result. Workers claim queued or expired-running work with `SELECT ... FOR UPDATE SKIP LOCKED`, commit the claim before reading the source, and never hold a transaction during parsing.

## Source integrity

The worker derives the source exclusively from:

```text
DEPTSLM_DATA_DIR/uploads/<department_uuid>/<document_uuid>/source
```

It uses validated UUID scope, descriptor-relative directory opens, no-follow file opens, and read-only access. Every component must be a real directory and the source a regular file. The worker recomputes both byte size and SHA-256 and requires exact agreement with the immutable document metadata before parsing. It never uses `original_filename`, modifies the source, or logs the path, filename, digest, or content.

## Parser subprocess boundary

Parsing runs through the installed `deptslm_worker.extraction_runner` module under Python isolated mode. The parent launches a fixed executable/module and reviewed arguments with `shell=False`, a new process session, closed unrelated descriptors, and inherited source/output/result descriptors. Stdout and stderr are discarded; a bounded JSON result file carries only safe parser metadata or an allowlisted failure code.

The child receives a minimal environment with no `DATABASE_URL`, JWT/auth values, bearer token, user environment, `PYTHONPATH`, hostname, original filename, or external host path. `TMPDIR` is a descriptor alias for the claim-specific extraction staging directory. On POSIX, the parent applies CPU, address-space, output-size, open-file, child-process, and core-dump limits where supported. Timeout or shutdown sends `SIGTERM` and then `SIGKILL` to the entire subprocess group. Normal Python sockets and subprocess creation are disabled inside the runner as defense in depth.

This is constrained subprocess isolation, not a kernel-enforced malware sandbox. Parser vulnerabilities remain possible. Seccomp, microVMs, a dedicated sandbox service, antivirus, and content-disarm controls remain deferred. Phase 5 does not claim that arbitrary PDFs are safe.

## Supported formats

- `application/pdf`: pypdf processes pages in order. Encrypted, malformed, page-limit, and textless/image-only PDFs fail safely. There is no OCR, JavaScript, attachment, embedded-file, image, or external-resource processing.
- `text/plain`: decoded again as UTF-8 and rejected on invalid encoding or NUL.
- `text/markdown`: treated exactly as untrusted plain text. HTML, code fences, links, images, includes, and templates are not rendered, executed, or fetched.

## Deterministic normalization

`phase5-normalization-v1`:

1. decode UTF-8;
2. remove one leading BOM;
3. convert CRLF and CR to LF;
4. normalize Unicode to NFC;
5. reject NUL and all-whitespace output;
6. preserve all other meaningful whitespace and Markdown syntax.

No cleanup model, summary, translation, redaction, or instruction interpretation occurs. PDF pages are joined in order with the deterministic `\n\f\n` boundary and mapped to one-based pages. Text and Markdown character spans map to one-based lines.

## External publication and quota

Claim-specific staging and final output use:

```text
DEPTSLM_DATA_DIR/extracted_text/<department_uuid>/<document_uuid>/.staging/
  <extraction_uuid>/<claim_token>/
DEPTSLM_DATA_DIR/extracted_text/<department_uuid>/<document_uuid>/<extraction_uuid>/
```

Each final directory contains private `normalized.txt`, `chunks.jsonl`, and `manifest.json`. The database stores no text or path. Directories use `0700`, files use `0600`, creation is exclusive, symlinks are rejected, and an existing final directory is never overwritten.

Finalization locks department, document, then extraction; revalidates active ownership, source metadata, current claim/lease, and pipeline; enforces `DEPTSLM_DEPARTMENT_EXTRACTED_QUOTA_BYTES`; inserts chunk metadata; renames staging atomically; updates the attempt; and appends `document.extraction.complete` in the database transaction. All retained successful output, including output for soft-deleted documents, counts toward the separate extraction quota.

PostgreSQL and the filesystem cannot commit atomically. Handled post-rename database failures compensate only the new exact output. A hard crash between rename and commit can leave an orphaned final directory; unknown directories are never automatically deleted and reconciliation is deferred.

## Safe failures

Public status may expose only: `source_missing`, `source_integrity_mismatch`, `unsupported_media_type`, `invalid_utf8`, `invalid_pdf`, `encrypted_pdf`, `page_limit_exceeded`, `extraction_timeout`, `extraction_output_limit`, `no_extractable_text`, `chunk_limit_exceeded`, `extraction_quota_exceeded`, `parser_failed`, `storage_unavailable`, `database_unavailable`, `document_unavailable`, `claim_lost`, and `worker_shutdown`.

Exception text, parser stderr, SQL, OS errors, paths, filenames, hashes, and content are never API or audit fields.

# Document Model Through Phase 6

Phase 4 adds PostgreSQL document metadata through Alembic revision `0002_phase4_documents`. Source bytes remain external and are never stored in PostgreSQL or Git.

## Metadata and ownership

Each `documents` row has a UUID, non-null `department_id`, uploader identity, normalized original filename, canonical media type, positive byte size, lowercase SHA-256 digest, lifecycle status, version, and timestamps. Soft deletion records both the deletion time and deleting identity. The database stores no source body or filesystem path.

Foreign keys use `RESTRICT`. Matching ORM and migration checks require a non-whitespace filename of at most 255 characters and 255 UTF-8 bytes, allow only `application/pdf`, `text/plain`, and `text/markdown`, require a 64-character lowercase hexadecimal digest, and require deleted metadata exactly when status is `deleted`. Indexes support department/status/creation lists and department/checksum lookup. API responses omit the checksum, storage path, uploader and deletion identity IDs, issuer, and subject.

## Isolation and visibility

Every repository query includes the authorized department. Active members in any of the five roles may list and read stored metadata. Same-department `system_admin`, `department_admin`, and `instructor` memberships may upload. Only same-department `system_admin` and `department_admin` memberships may soft-delete. `system_admin` has no cross-department bypass.

Deleted rows are hidden from list/read APIs, but their source files are retained and their bytes continue to count against quota. Repeated deletion returns not found and creates no second mutation-success audit row. Physical deletion, retention scheduling, legal holds, recovery, and purge are deferred.

## Transaction boundary

Upload finalization locks the department first, revalidates the exact issuer, subject, membership, expiry, role, and active department state, then calculates retained bytes. This serializes quota decisions per department. The final metadata row and `document.upload` audit row commit in one transaction. Soft deletion and its `document.delete` audit row also commit together.

PostgreSQL and the filesystem cannot share an atomic transaction. Normal handled failures after a move compensate by removing only the newly created destination. A process or host crash can still leave an orphaned source between the atomic rename and database commit; automatic orphan discovery or deletion is intentionally not implemented in Phase 4.

## Extraction and indexing behavior

Phase 5 adds extraction attempt and chunk metadata without changing document source ownership or exposing content. Stored documents may enqueue extraction; soft deletion cancels queued attempts, blocks running-worker finalization, hides extraction/chunk APIs, and retains successful output for quota and future retention review.

Phase 6 adds vector-indexing history without exposing content. Soft deletion also cancels queued indexing attempts in the same PostgreSQL transaction and blocks running indexing finalization; it makes no synchronous Qdrant call. Previously succeeded points may remain physically retained, but the internal retrieval authority check rejects deleted documents. Automatic vector/source/extraction purge remains deferred.

The system still does not render, scan, OCR, download, preview, restore, publicly search, run RAG, or physically purge documents. See [document-extraction.md](document-extraction.md), [chunk-model.md](chunk-model.md), and [vector-indexing.md](vector-indexing.md).

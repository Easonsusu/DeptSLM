# Chunk Model Through Phase 7

## Version and algorithm

`phase5-character-chunker-v1` is deterministic and uses Unicode code-point counts rather than a tokenizer. Defaults are 1,200 maximum characters and 200 overlapping characters. Overlap must be less than and no more than half the maximum.

For each chunk, the algorithm prefers a paragraph boundary near the target, then newline, whitespace, and finally a hard character boundary. It avoids a boundary immediately before a Unicode combining mark where practical. Every loop has a reviewed progress floor of the previous start plus one; combining-mark adjustment can never move behind that floor. A combining sequence longer than the overlap or boundary window therefore favors strict progress and the size limit rather than looping. Chunks are nonempty, contain non-whitespace text, never exceed the configured maximum, and use zero-based ordinals plus half-open normalized offsets `[char_start, char_end)`.

Identical normalized input and settings produce identical text, offsets, byte sizes, hashes, ordering, and provenance. `DEPTSLM_MAX_CHUNKS_PER_DOCUMENT` stops pathological output without partial publication.

## Provenance

PDF chunks have one-based `page_start`/`page_end` and no line range. Text and Markdown chunks have one-based `line_start`/`line_end` and no page range. Offsets always refer to `normalized.txt`; overlapping chunks may therefore have overlapping ranges. Page and line provenance are never mixed.

## Persistence boundary

PostgreSQL `document_chunks` rows contain department, document, extraction, ordinal, character range, UTF-8 byte size, internal SHA-256, provenance kind/range, and creation time. Composite foreign keys prevent cross-department/document assignment. The database contains no chunk text, normalized text, filename, or filesystem path.

External `chunks.jsonl` is the content-bearing artifact. Each line includes chunk text and the corresponding metadata. Metadata APIs omit text, content hashes, paths, source hashes, and internal claim fields. Phase 5 has no chunk-content endpoint.

Phase 6 keeps `DocumentChunk.id` as the server-owned Qdrant point UUID. The indexing worker incrementally compares each external line with the exact department/document/extraction/ordinal row before embedding. Qdrant payload retains only IDs, ordinal, page/line provenance, pipeline version, attempt identity, and publication state; chunk text, its digest, normalized text, filename, and paths remain absent. There is still no chunk-content or public search endpoint.

Phase 7 treats the UUID as a lookup key, never as authorization. Each retrieved point must pass PostgreSQL authority before the API incrementally locates the exact selected record. The server assigns transient source labels only after selection. Public citations may expose chunk UUID, ordinal, and page/line provenance, but never chunk text, hashes, extraction/indexing IDs, paths, or scores.

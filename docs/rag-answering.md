# Phase 7 Grounded RAG Answering

## Scope

Phase 7 adds one non-streaming endpoint:

```text
POST /departments/{department_id}/rag/answers
```

All five active same-department roles may call it. The path value is only a selector: the API resolves the exact issuer/subject and current membership in PostgreSQL at admission and again before completion. `system_admin` has no cross-department bypass. The endpoint does not expose vector search, query vectors, chunks, prompts, or model controls.

The request contains only `question`, normalized with Unicode NFC, trimmed, checked for forbidden controls, and limited to 2,000 characters. The response is either an answered result with reviewed citation metadata or the exact insufficient-information message. There is no conversation, message history, streaming, feedback, reranking, adapter selection, or training behavior.

## Retrieval and selection

The internal runtime embeds the question with `Qwen/Qwen3-Embedding-0.6B` revision `d23109d65ca9fdf61eef614209744716f337f50f`, normalized 1,024-dimensional cosine output, and the exact instruction:

```text
Given a user question, retrieve passages from the authorized department documents that directly support an answer.
```

The API validates the vector, verifies the fixed Qdrant collection, and calls the reviewed adapter with typed `DepartmentScope`. Search always includes exact `department_id`, `published=true`, and `phase6-qwen3-embedding-v1`. It retrieves at most 20 candidates. The existing PostgreSQL authority method then accepts only candidates backed by a succeeded indexing row, stored document, succeeded extraction, current vector attempt, and exact department/document/extraction/chunk ownership.

Selection is deterministic: descending finite score, then chunk UUID. The provisional default threshold is `0.45`; at most eight sources and two sources per document are selected, with at most 6,000 evidence characters in total. These values are bounded configuration, not a claim of calibrated production quality.

## Artifact and final authority

Only selected chunks are read from the exact Phase 5 `normalized.txt`, `chunks.jsonl`, and `manifest.json` directory beneath `DEPTSLM_DATA_DIR/extracted_text`. The reader incrementally verifies the manifest, file identity, sizes, hashes, ordinals, offsets, provenance, and exact PostgreSQL chunk metadata before and after the scan. An answered model result triggers a second exact artifact read; the evidence must still be byte-for-byte identical to what generation received. The reader does not write text to disk or return unselected chunks.

After generation, the API reloads the complete evidence set and requires byte-identical text. It then starts a new short transaction, locks the run and every supplied document, extraction, indexing attempt, and chunk in deterministic UUID order, and reauthorizes the caller. The exact PostgreSQL snapshot captured during retrieval is compared field by field, including Phase 5 versions, artifact sizes and hashes, chunk offsets/provenance, the current vector attempt, point counts, the complete embedding/vector contract, and the fixed collection. This applies even to evidence the model did not cite: an uncited supplied source may have influenced generation, so any stale, deleted, altered, mismatched, or unauthorized supplied item invalidates the whole result.

The supplied-evidence set and cited subset are intentionally distinct. `selected_source_count` records how many sources were supplied to generation, including a generated insufficient-information result; `rag_answer_citations` and the public response contain only labels actually referenced by the validated answer. A no-evidence insufficient result records zero selected sources.

## Persistence and consistency

Revision `0005_phase7_rag_answers` stores content-free run and citation provenance metadata. It stores counts, fixed model/prompt contracts, safe status/error codes, source labels, IDs, ranks, scores, and page/line provenance. It never stores question or answer text, prompts, evidence, chunk text, vectors, hashes, paths, tokens, model output, or dependency URLs.

`rag.answer.start` is committed with the admitted run. `rag.answer.complete` is committed only with an applied answered or insufficient result and exact citation rows. Failures do not create a completion-success audit. Qdrant client-close failure after a committed result is reported only as a content-free process event and cannot rewrite that result; handled unexpected failures best-effort mark a still-running row failed with a stage-specific safe code. PostgreSQL, Qdrant, external artifacts, the supervised model child, runtime HTTP, and API HTTP do not share a transaction; post-generation artifact verification, final authorization, and PostgreSQL source authority reduce stale-state acceptance but do not claim distributed atomicity or an atomic filesystem/database snapshot.

## Insufficient information

When no authorized source meets the threshold, the API does not call generation. It returns:

```text
I do not have enough information in the authorized department sources to answer that question.
```

The same safe result is accepted from the strict model contract. The service never fabricates an answer or citation when evidence is absent, irrelevant, inconsistent, or unsupported.

## Limitations

The prompt-injection defense is layered validation, not a proof that a language model cannot be manipulated. Threshold calibration, quality evaluation, production rate limiting, TLS, secrets management, model distribution, observability, reconciliation, and hardware sizing remain deferred. See [prompt-injection-boundary.md](prompt-injection-boundary.md), [citation-model.md](citation-model.md), and [rag-runtime.md](rag-runtime.md).

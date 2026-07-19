# Phase 7 Citation Model

## Public contract

An answered result contains plain answer text and a non-empty citation list. Each citation exposes only:

- server source label;
- document UUID and reviewed original filename;
- chunk UUID and ordinal;
- page or line provenance.

It exposes no department ID, extraction/indexing ID, internal score, hashes, paths, identities, tokens, query vector, chunk text, or dependency configuration. The answer may reference only exact ASCII bracketed labels issued for that request, `[S1]` through `[S8]`. Unsafe format controls, combining grapheme joiner, other disallowed controls, surrogates, and noncharacters never reach public text. Variation selectors, ordinary combining accents, and ordinary emoji remain supported. An unsafe original filename is displayed with deterministic visible `\u{XXXX}` escapes while its database value remains unchanged; the bounded renderer never truncates inside an escape.

## Validation

The generation response must be an exact JSON object with `status`, `answer`, and `citations`. For `answered`, answer text is non-empty, every referenced bracket label is in the citations array, the array is non-empty and duplicate-free, and every label was assigned by the server to selected authorized evidence. Citation-list order must equal first appearance.

A focused linear lexer recognizes only literal ASCII `[S1]` through `[S8]`. It rejects paired, dangling, unbalanced, mixed-bracket, full-width, lowercase, whitespace, leading-zero, out-of-range, hidden-character, and arbitrarily long source-like forms, including closing brackets beyond the old short scan window. Ordinary prose such as `[Section 1]`, `[equation x]`, mathematical intervals, and normal parentheses remains valid. For `insufficient_information`, answer is empty and citations is empty. Unknown fields, missing support, invented sources, excessive output, unsafe Unicode, and thinking tags fail closed.

Before success, every source supplied to generation—not only the cited subset—maps back to its exact selected department/document/extraction/indexing/chunk snapshot. PostgreSQL must still show a stored document, supported succeeded extraction, succeeded current indexing attempt, complete point counts, the exact embedding/vector/collection contract, and unchanged chunk metadata. A post-generation artifact read must match the complete exact evidence set sent to the model. The API writes only cited metadata and the `rag.answer.complete` audit in the same transaction as the answered run.

## Persistence

`rag_answer_citations` stores only content-free provenance: run and source IDs, server label, rank, internal retrieval score, ordinal, and mutually exclusive page/line ranges. `rag_answer_runs.selected_source_count` counts the complete evidence set supplied to generation; citation-row count is the smaller or equal actually referenced subset. Restrictive composite foreign keys prevent cross-department or cross-source citation rows. Answers and quoted evidence are deliberately not recoverable from PostgreSQL.

Citation metadata shows which reviewed source record supported the transient answer, but it is not a durable answer archive or a guarantee that the wording is correct. Source retention, physical deletion, export, replay, citation snapshots, and long-term reproducibility remain deferred.

Phase 8 may attach structured feedback targets only by public `S1` through `S8` labels already persisted for the exact run. The API resolves each label to the existing same-department citation row and stores only its UUID in a restrictive composite foreign key; it never copies the label, filename, source text, score, provenance, hash, path, document, extraction, or indexing metadata into feedback. Citation-target controls exist only for answered runs with `wrong_citation` or `irrelevant_source`, and structured feedback does not establish whether a citation is objectively correct.

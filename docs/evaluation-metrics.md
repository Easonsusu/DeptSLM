# Evaluation metrics

Phase 9 uses `phase9-deterministic-metrics-v1`; it has no LLM judge, semantic grader, external API, BLEU, ROUGE, BERTScore, or embedding-similarity score.

For answered cases, exact authorized top-20 chunk UUIDs produce macro-averaged recall@5, recall@10, recall@20, and reciprocal rank@20. Duplicate candidates invalidate the case. Foreign or PostgreSQL-unauthorized candidates are excluded by the production retrieval authority.

Answer status measures `answered` versus `insufficient_information`. Answer normalization `phase9-answer-normalization-v1` applies NFC, Unicode casefold, edge trimming, and Unicode-whitespace collapse while retaining punctuation. Normalized exact match and non-whitespace Unicode code-point multiset character F1 use the maximum across accepted answers. Insufficient cases are excluded from answer-match and retrieval denominators.

Citation precision and recall compare final exact chunk UUIDs with the imported exact relevant set. A chunk from a relevant document is not relevant unless that exact chunk was declared. Malformed answer or citation output contributes to invalid-contract rate.

Exact match and character F1 do not establish semantic correctness. Citation metrics measure identifier overlap, not claim entailment. Results are useful regression signals, not production-quality claims.

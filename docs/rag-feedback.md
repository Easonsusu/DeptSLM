# Structured RAG Feedback

Phase 8 records department-scoped feedback as structured PostgreSQL metadata. Feedback is not an answer history: the tables contain no question, answer, prompt, evidence, excerpt, filename, path, vector, model output, credential, or free-text comment. It neither establishes answer correctness nor changes retrieval, prompting, generation, evaluation, or training automatically.

## Submission contract

The original requester may submit one immutable feedback record for an `answered` or `insufficient_information` run. All five active roles may submit for their own run after transaction-time membership reauthorization in the exact department. `system_admin` has no cross-department bypass.

Sentiments are `helpful`, `unhelpful`, and `report`. Helpful feedback accepts zero to four of `clear`, `complete`, `well_supported`, and `useful_citations`. Unhelpful and report feedback require one to five reviewed negative reasons. `insufficient_when_expected` is limited to insufficient-information runs; `wrong_citation` and `irrelevant_source` are limited to answered runs and require one or more exact persisted citation labels from that run. Source labels are canonicalized numerically and reasons use server-owned order.

`PUT /departments/{department_id}/rag/answers/{run_id}/feedback` creates the record with HTTP 201. Its JSON body is incrementally limited to 4,096 bytes before strict UTF-8 decoding or JSON/Pydantic validation. Reason codes and source IDs are exact reviewed identifiers (`S1` through `S8` for sources), not arbitrary strings. Malformed transport or JSON returns 400, an oversized body returns 413, and schema or structured-contract failure returns 422. An identical canonical replay returns the existing record with HTTP 200 without changing its version, timestamps, or audit history. A different payload returns 409; there is no edit or withdrawal endpoint. `GET` on the same route returns only the requester's active feedback.

## Safety boundaries

- Parent, reason, and source-target rows are created atomically.
- Source targets reference exact citation metadata from the same department and run; citation UUIDs are never accepted from clients.
- Public feedback responses omit submitter and reviewer identities and all answer, document, chunk, indexing, runtime, and storage details.
- Feedback code uses PostgreSQL only. It cannot call Qdrant, the RAG runtime, artifact readers, model code, or external storage.
- Submission and review body limits are enforced while streaming, including when `Content-Length` is absent; bodies are never logged or written to disk.
- Browser controls are not an authorization boundary and persist no feedback in browser storage.

Feedback is a user-provided review signal, not an evaluation result, ground truth, quality gate, or training dataset. Phase 9 modules do not import feedback models or services, create cases from feedback, weight cases, tune gates, trigger runs, or resolve feedback.

## Retention

Creation and visibility use PostgreSQL server time. The validated retention setting defaults to 180 days and permits 30 through 730 days. Expired feedback becomes inaccessible before physical purge. Review never extends expiry. See [Feedback retention](feedback-retention.md).

## Non-goals

Phase 8 does not add free-text comments, reviewer notes, answer replay or history, reopening, automatic triage, classifiers, prompt or ranking changes, model changes, evaluation, exports, notifications, datasets, adapters, scheduled purge, or cross-department analytics.

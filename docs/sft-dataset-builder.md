# Phase 10 SFT dataset builder

Phase 10 is under review. It builds private, department-scoped supervised fine-tuning dataset artifacts from immutable human-authored source bundles; it does not train a model, use LLaMA-Factory, create an adapter, or begin Phase 11 or Phase 12.

## Source and authority contract

A source directory contains only `manifest.json` and `examples.jsonl`. The source manifest uses `phase10-sft-source-v1`; each example uses `phase10-sft-example-v1` with an `example_id`, `group_id`, canonical instruction and response, and one through eight exact source chunk UUIDs. The importer rejects unknown fields, duplicate JSON keys, malformed UTF-8, unsafe Unicode, duplicate IDs/pairs, conflicting responses, duplicate source references, unsafe paths, symlinks, hard links, and mismatched digests/counts.

Every source reference must resolve to the exact same department, a stored document, a succeeded extraction, a succeeded indexing, and a current chunk. The builder repeats this authority check before publication. This establishes traceability, not answer entailment or evaluation quality. Operators remain responsible for preventing evaluation contamination; feedback and Phase 9 evaluation suites are never reused as SFT data.

## Deterministic external dataset

The builder normalizes with `phase10-sft-normalization-v1` and uses `phase10-sft-group-split-v1`: groups never cross splits, the validation target is exactly 0.10, ordering derives from SHA-256 over canonical bytes of the build UUID, group UUID, and fixed split version, and output JSON has LF endings, canonical serialization, and a final newline. Final files are exactly `manifest.json`, `train.jsonl`, `validation.jsonl`, and `provenance.jsonl` under `DEPTSLM_DATA_DIR/training_datasets`.

`train.jsonl` and `validation.jsonl` use a closed two-message user/assistant shape with no system message. `provenance.jsonl` has only example, group, split, and source authority identifiers; it never repeats content. PostgreSQL retains content-free metadata and post-rename verified digests only. There is no content or download endpoint.

## Lifecycle and limits

Same-department active `system_admin`, `department_admin`, and `instructor` roles may import sources and enqueue/cancel builds. Only active `system_admin` and `department_admin` roles may approve, reject, archive, purge, or reconcile. A succeeded build enters pending review; approve/reject are one-way pending transitions and archive follows an approved/rejected decision. This is not a two-person approval guarantee.

The worker uses finite PostgreSQL-server-time leases and exact worker/token/publication-attempt ownership. Contentful dataset construction runs only through a fixed exec child with a closed request schema, an exact secret-free environment, `close_fds`, and explicitly passed private source/stage descriptors; it receives no database connection, application configuration, model, Qdrant, or runtime capability. Parent-to-child requests and child responses are framed, bounded, deadline-controlled, and heartbeat-supervised. Dataset bytes never cross that IPC boundary.

The parent hashes final artifacts outside database locks, then retains the exact no-follow final directory and file descriptors through the short success transaction. It rechecks directory entries, retained file identity, the exact manifest, current membership, source authority, and claim ownership before committing success; it does not rehash a large artifact while holding locks. External storage and PostgreSQL are not transactionally atomic, so an activated final artifact whose commit fails remains untrusted and may require explicit reconciliation.

Reconciliation registers every possible exact-owned surface for an interrupted source or dataset lifecycle: the private UUID stage and, when durable manifest ownership exists, the final artifact. A blocked final surface prevents cleanup confirmation even if a stage deletion succeeded. A later authorized reconciliation creates a fresh operation item for the same exact resource/attempt; completed absence is idempotent, while blocked history remains auditable. Stage deletion uses one descriptor chain and does not parse stage markers, so missing, zero-byte, truncated, or partial `.deptslm-stage-owner` states are recoverable only inside the metadata-owned path. Unknown or unsafe paths fail closed. Purge is explicit, bounded, retention-based, preserves metadata and audits, and does not delete backups. Google Drive is not a production object store or backup.

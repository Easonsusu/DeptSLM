# Evaluation suites

Phase 9 suites are immutable, department-scoped inputs for internal evaluation. An active same-department `system_admin`, `department_admin`, or `instructor` imports a reviewed absolute directory outside the repository:

```bash
python -m app.evaluation_admin import-suite \
  --department-id <UUID> \
  --actor-issuer <issuer> \
  --actor-subject <subject> \
  --source-directory <absolute-path> \
  --apply
```

The command is a dry run without `--apply`. The source allowlist is exactly `suite.json` and `cases.jsonl`, with at most 500 cases and 16 MiB total input. Symlinks, unknown files, malformed UTF-8 or JSONL, unsafe Unicode, duplicate identifiers or normalized answers, and noncanonical Decimal gates fail closed.

Answered cases contain one to eight exact relevant chunk UUIDs and one to eight accepted answers. Insufficient-information cases contain neither. Import validates each chunk against same-department stored document, succeeded Phase 5 extraction, current succeeded Phase 6 indexing, and exact external artifact integrity. Canonical cases are enriched with server-owned source snapshots and published only under `DEPTSLM_DATA_DIR/eval_results/suites/<department>/<suite>`.

Questions, accepted answers, and source identifiers never enter PostgreSQL or public APIs. Archival is one-way and prevents new runs. PostgreSQL and external publication are compensating operations, not an atomic transaction.

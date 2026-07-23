# Evaluation runner

The dedicated evaluator claims one department-scoped run at a time:

```bash
python -m deptslm_worker.evaluator --once
python -m deptslm_worker.evaluator --poll
```

Claims use PostgreSQL server time, exact worker and claim UUIDs, non-revivable leases, department-first locking, bounded heartbeats, and fresh tokens after reclaim. The worker revalidates the department, active suite, requester evaluator membership, fixed production contracts, suite hashes, and every ground-truth snapshot before execution and final publication. Cancellation is observed between cases and before publication. A stale worker cannot publish or finalize; a replacement removes only the exact stale attempt staging directory.

Every case calls the same internal Phase 7 query normalization, embedding, typed `DepartmentScope` retrieval, PostgreSQL authority check, relevance threshold, source selection, artifact reader, prompt builder, runtime client, answer validator, citation lexer, and final all-evidence revalidation as production. The evaluator creates no normal answer run, citation row, feedback, public search endpoint, or content persistence.

The run records exact code and model revisions plus deterministic base/per-case seed policy. Fixed seeds improve repeatability but cannot guarantee bit-identical generation across hardware, libraries, or kernels. PostgreSQL, Qdrant, external artifacts, and the model runtime are not transactionally atomic.

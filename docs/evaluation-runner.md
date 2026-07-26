# Evaluation runner

The dedicated evaluator claims one department-scoped run at a time:

```bash
python -m deptslm_worker.evaluator --once
python -m deptslm_worker.evaluator --poll
```

Claims use PostgreSQL server time, exact worker and claim UUIDs, non-revivable leases, department-first locking, bounded heartbeats, and fresh tokens after reclaim. Suite reading, each case, and final ground-truth verification run in killable child process groups with an operation deadline. The parent renews the exact live claim while work is active; timeout, cancellation, shutdown, claim loss, or a heartbeat database failure terminates and reaps the child group. A stale worker cannot publish or finalize; shutdown leaves work reclaimable, while cancellation becomes cancelled. A replacement removes only the exact stale attempt staging directory.

Before publication, the worker captures and later revalidates every case's complete ground-truth authority: document state, extraction and artifact identity, indexing status and current attempt, chunk metadata, fixed contracts, active suite, and requester membership. The final check takes deterministic PostgreSQL locks but does not perform a large external scan while holding them. Any relevant source mutation, suite archive, or requester revocation prevents final artifacts, case rows, and `evaluation.run.complete`.

Every case calls the same internal Phase 7 query normalization, embedding, typed `DepartmentScope` retrieval, PostgreSQL authority check, relevance threshold, source selection, artifact reader, prompt builder, runtime client, answer validator, citation lexer, and final all-evidence revalidation as production. The evaluator creates no normal answer run, citation row, feedback, public search endpoint, or content persistence.

The run records exact code and model revisions plus deterministic base/per-case seed policy. Fixed seeds improve repeatability but cannot guarantee bit-identical generation across hardware, libraries, or kernels. PostgreSQL, Qdrant, external artifacts, and the model runtime are not transactionally atomic. Process fencing cannot revoke an already in-flight remote request; final PostgreSQL authority and content-free publication fail closed rather than claiming a distributed transaction.

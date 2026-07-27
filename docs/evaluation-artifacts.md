# Evaluation artifacts

All Phase 9 files live beneath external `DEPTSLM_DATA_DIR/eval_results`:

```text
eval_results/
  suites/<department UUID>/<suite UUID>/{manifest.json,cases.jsonl}
  runs/<department UUID>/<run UUID>/{manifest.json,summary.json,case_results.jsonl}
  staging/{suites,runs}/...
```

Paths use server-owned UUIDs only. Operations reject symlinks, hard links, path replacement, and unknown entries; use private permissions, exclusive staging, descriptor-relative no-follow handles, bounded incremental JSONL, SHA-256 and byte-size verification, and atomic final rename. Hashing and parsing use the same open descriptor. Staging is reverified immediately before rename, and every final file is rehashed through verified descriptors after rename; PostgreSQL stores only those final digests. Final artifacts are immutable.

Run manifests bind the department, suite, run, server-generated publication UUID, positive run attempt number, and code revision. Reconciliation verifies that entire tuple and every allowlisted payload digest before removing either exact staging or final content. Suite staging can contain questions and accepted answers, so reconciliation reads only closed, no-follow descriptor-verified files and never emits suite content, hashes, or paths.

`reconcile-artifacts` registers a durable content-free batch before deletion. A crash after registration or filesystem deletion leaves a resumable batch; a later authorized invocation verifies the same ownership tuple, finalizes terminal metadata, and writes one batch audit. Completed batches, succeeded runs, committed suites, malformed artifacts, and unowned artifacts are never selected for deletion. This is compensating recovery, not backup or historical-audit deletion.

Suite content may contain questions, accepted answers, and server-owned ground-truth snapshots, but remains external and has no public download API. Run artifacts are content-free: manifests contain fixed contracts and digests, summaries contain numeric aggregates and exact gate results, and case results contain only case UUID, statuses, counts, numeric metrics, booleans, and safe error codes.

Generated answers, prompts, evidence, source filenames, source IDs, vectors, runtime responses, and question/answer hashes are never written to run artifacts. Failed or cancelled runs have no final result directory. External publication and PostgreSQL success are compensating operations; neither implies a distributed transaction.

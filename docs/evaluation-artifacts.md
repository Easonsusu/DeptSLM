# Evaluation artifacts

All Phase 9 files live beneath external `DEPTSLM_DATA_DIR/eval_results`:

```text
eval_results/
  suites/<department UUID>/<suite UUID>/{manifest.json,cases.jsonl}
  runs/<department UUID>/<run UUID>/{manifest.json,summary.json,case_results.jsonl}
  staging/{suites,runs}/...
```

Paths use server-owned UUIDs only. Operations reject symlinks and unknown entries, use private permissions, exclusive staging, no-follow descriptors where available, bounded incremental JSONL, SHA-256 and byte-size verification, and atomic final rename. Final artifacts are immutable. Cleanup is restricted to the exact department/resource/attempt staging path.

Suite content may contain questions, accepted answers, and server-owned ground-truth snapshots, but remains external and has no public download API. Run artifacts are content-free: manifests contain fixed contracts and digests, summaries contain numeric aggregates and exact gate results, and case results contain only case UUID, statuses, counts, numeric metrics, booleans, and safe error codes.

Generated answers, prompts, evidence, source filenames, source IDs, vectors, runtime responses, and question/answer hashes are never written to run artifacts. Failed or cancelled runs have no final result directory. External publication and PostgreSQL success are compensating operations; neither implies a distributed transaction.

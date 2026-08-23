# Reviewed recovery guide

This is a safe recovery guide for the local prototype, not a production
disaster-recovery or backup system. PostgreSQL authority and external bytes are
not atomically committed. Filesystem presence never grants authority; do not
adopt unknown orphan files, edit rows to force success, or rewrite Qdrant
payloads to manufacture authority.

| Failure | Safe system behavior | Supported recovery action | Unsafe/manual action to avoid | Remaining limitation |
| --- | --- | --- | --- | --- |
| API/container restart | Requests resume from PostgreSQL state | Restart the service and retry the same operation | Recreate metadata by hand | In-flight calls are lost |
| PostgreSQL unavailable | Claims and success transitions fail closed | Restore connectivity, then retry | Mark jobs succeeded manually | External bytes are non-atomic |
| Qdrant unavailable | Indexing/RAG fails without success authority | Restore Qdrant and retry exact attempts | Treat payloads as authority | Qdrant durability is local |
| Qdrant state loss | PostgreSQL indexing authority no longer proves vectors | Rebuild through the reviewed indexing queue | Edit payloads or bypass collection checks | No coordinated backup exists |
| Base model cache loss | Offline runtime remains unavailable | Run the explicit `python -m deptslm_worker.model_admin prepare-rag-models` preparation command again | Enable automatic download | Supply-chain trust remains operator responsibility |
| Adapter-runtime crash/load failure | Request fails closed without base fallback | Inspect the exact deployment and restart the runtime; enqueue an explicit rollback-to-base operation through the reviewed governance worker when authorized | Change target or silently route base | Rollback is separate and never automatic |
| Adapter registry unavailable | Governance/runtime admission fails closed | Restore external storage and retry | Adopt an orphan directory | Drive sync is not atomic |
| Upload crash/orphan | Handled failures clean staging; a crash after filesystem rename and before the PostgreSQL commit leaves bytes non-authoritative | **Supported automated recovery: none for this post-rename/pre-database-commit orphan window.** Safe operator action: preserve and report the unexpected orphan; do not adopt its pathname/file as PostgreSQL authority, edit PostgreSQL to make it authoritative, or delete an unknown orphan merely because its pathname looks plausible. A future separately reviewed orphan reconciliation/operator procedure is required. | Move bytes into a final path, edit metadata, or delete the unknown orphan | Non-authoritative retained bytes may remain after this crash window |
| Extraction lease expiry | Old claim cannot publish; replacement cleans exact attempt | Let the worker reclaim and retry | Reuse a stale token | Filesystem and DB remain non-atomic |
| Indexing lease expiry | Stale Qdrant attempt is fenced and cleaned exactly | Reclaim the exact vector attempt | Activate stale points manually | In-flight network writes cannot be fenced retroactively |
| Phase 9 evaluation crash | Lease/reconciliation keeps result untrusted | Restart the evaluator worker and reclaim the exact run; its implemented stale-publication reconciliation is `reconcile_stale_publication` | Publish a partial result | External result files can outlive a crash |
| Phase 10 SFT crash | Attempt metadata remains content-free and resumable | Run the implemented `python -m app.admin reconcile-sft-artifacts` command for the exact department | Copy dataset records into PostgreSQL | External datasets need independent protection |
| Phase 11 bundle crash/purge | Descriptor-bound attempt cleanup remains exact | Run `python -m app.admin reconcile-training-job-artifacts` or the explicit `purge-training-job-artifacts` command for the exact job | Execute the generated config as recovery | LlamaFactory execution is out of scope |
| Phase 12 E-A reconciliation | Unsafe or unknown surfaces stay blocked | Run the implemented `python -m app.admin reconcile-adapter-artifacts` operation and review exact department/resource items | Adopt or delete unknown files | Manual operator review is required |
| Phase 12 E-B purge | Reservations and tombstones make purge resumable | Run the implemented `python -m app.admin purge-adapter-artifacts` command for the exact adapter after resolving a scoped conflict | Delete by department or filename | Purge never removes backups/history |
| Phase 12 E-C release | Dependency release requires completed E-B authority | Run the implemented `python -m app.admin release-adapter-upstream-dependency` command for the exact metadata operation | Delete artifacts during release | Release does not repair storage |
| Governance interruption | Review/deployment operation remains explicit metadata | Restart `adapter-governance-worker` and retry the same operation authority snapshot | Promote by editing `Adapter.status` | No automatic promotion or rollback |
| `.env` exposure | Wrapper rejects unsafe file permissions | Rotate values and recreate mode-600 file | Print resolved Compose config | No production secret manager exists |
| Google Drive sync conflict | PostgreSQL remains final authority | Resolve the exact external path and retry | Treat a synced copy as authoritative | Cloud sync is not transactional |
| Synthetic demo failure/reset | Unique project and external root are isolated | Re-run `scripts/demo.sh` | Remove the normal project/data root | Demo is not a backup or restore test |

Runtime failures never trigger automatic base fallback. Model preparation is
explicit, and this repository has no reviewed production backup/restore
mechanism. Never copy a live PostgreSQL data directory or Qdrant storage
directory and call it a safe backup.

The recovery-table audit above intentionally distinguishes implemented worker
reclaim/reconciliation and administrative commands from unsupported cases. In
particular, no document-orphan reconciler exists for the upload rename/commit
crash window; no automatic disaster recovery, live PostgreSQL directory copy,
or live Qdrant directory copy is supported. Feedback purge, evaluation
reconciliation, SFT reconciliation/purge, training-job reconciliation/purge,
adapter E-A reconciliation, E-B purge, E-C release, governance retry, explicit
rollback-to-base, and explicit model preparation all remain bounded,
department-scoped operations with PostgreSQL as final authority.

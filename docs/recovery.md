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
| Base model cache loss | Offline runtime remains unavailable | Explicitly prepare the pinned model again | Enable automatic download | Supply-chain trust remains operator responsibility |
| Adapter-runtime crash/load failure | Request fails closed without base fallback | Inspect exact deployment and restart runtime | Change target or silently route base | Explicit rollback is separate |
| Adapter registry unavailable | Governance/runtime admission fails closed | Restore external storage and retry | Adopt an orphan directory | Drive sync is not atomic |
| Upload crash/orphan | Staging is bounded and not authority | Re-run authorized document cleanup | Move bytes into a final path | Crash windows can leave untrusted files |
| Extraction lease expiry | Old claim cannot publish; replacement cleans exact attempt | Let the worker reclaim and retry | Reuse a stale token | Filesystem and DB remain non-atomic |
| Indexing lease expiry | Stale Qdrant attempt is fenced and cleaned exactly | Reclaim the exact vector attempt | Activate stale points manually | In-flight network writes cannot be fenced retroactively |
| Phase 9 evaluation crash | Lease/reconciliation keeps result untrusted | Reclaim or run reviewed reconciliation | Publish a partial result | External result files can outlive a crash |
| Phase 10 SFT crash | Attempt metadata remains content-free and resumable | Reconcile the exact dataset attempt | Copy dataset records into PostgreSQL | External datasets need independent protection |
| Phase 11 bundle crash/purge | Descriptor-bound attempt cleanup remains exact | Retry the same job or reviewed purge | Execute the generated config as recovery | LlamaFactory execution is out of scope |
| Phase 12 E-A reconciliation | Unsafe or unknown surfaces stay blocked | Review exact department/resource item | Adopt or delete unknown files | Manual operator review is required |
| Phase 12 E-B purge | Reservations and tombstones make purge resumable | Retry exact operation after resolving a scoped conflict | Delete by department or filename | Purge never removes backups/history |
| Phase 12 E-C release | Dependency release requires completed E-B authority | Retry the exact metadata operation | Delete artifacts during release | Release does not repair storage |
| Governance interruption | Review/deployment operation remains explicit metadata | Retry with the same authority snapshot | Promote by editing `Adapter.status` | No automatic promotion or rollback |
| `.env` exposure | Wrapper rejects unsafe file permissions | Rotate values and recreate mode-600 file | Print resolved Compose config | No production secret manager exists |
| Google Drive sync conflict | PostgreSQL remains final authority | Resolve the exact external path and retry | Treat a synced copy as authoritative | Cloud sync is not transactional |
| Synthetic demo failure/reset | Unique project and external root are isolated | Re-run `scripts/demo.sh` | Remove the normal project/data root | Demo is not a backup or restore test |

Runtime failures never trigger automatic base fallback. Model preparation is
explicit, and this repository has no reviewed production backup/restore
mechanism. Never copy a live PostgreSQL data directory or Qdrant storage
directory and call it a safe backup.

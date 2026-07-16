# Planned Architecture

## Status and boundaries

Phase 6 adds department-scoped PostgreSQL indexing jobs, pinned offline Qwen3 chunk embeddings, and a mandatory-filter Qdrant adapter to the Phase 5 extraction boundary. Public retrieval, RAG, generation, reranking, model serving, training, and adapter flows remain designs, not implemented capabilities.

## System context

DeptSLM is planned as a department-isolated monorepo application. The web client will call a FastAPI control plane. PostgreSQL will hold application metadata and authorization relationships; Qdrant will hold embeddings with department-scoped payloads. Long-running ingestion and training work will live outside request handlers. File-based artifacts will be stored outside the checkout under `DEPTSLM_DATA_DIR`.

```mermaid
flowchart TB
    User["Authorized department user"]

    subgraph App["DeptSLM application"]
        Web["Next.js web app"]
        API["FastAPI API"]
        RAG["RAG worker (extraction)"]
        Index["Indexing worker (embedding and Qdrant)"]
        Train["Training worker (planned)"]
    end

    subgraph Data["State services"]
        PG[("PostgreSQL\nmetadata and memberships")]
        QD[("Qdrant\ndepartment-filtered vectors")]
    end

    subgraph RAGStack["RAG and inference (planned)"]
        LI["LlamaIndex workflows"]
        Embed["Qwen3-Embedding"]
        Runtime["Qwen3 model runtime"]
    end

    subgraph TrainingStack["Adapter training (planned)"]
        Factory["LLaMA-Factory"]
        Adapter["Department LoRA / QLoRA adapter"]
    end

    Drive[("External runtime storage\nDEPTSLM_DATA_DIR on Google Drive")]

    User -->|"HTTPS in a future deployment"| Web
    Web -->|"JSON API"| API
    API -->|"authorized metadata queries"| PG
    API -->|"enqueue and inspect jobs"| RAG
    API -->|"enqueue and inspect metadata"| Index
    API -->|"enqueue and inspect jobs"| Train
    API -->|"department-scoped query"| LI
    Index -->|"offline document embeddings"| Embed
    Index -->|"scoped staged upserts"| QD
    LI -->|"grounded prompt"| Runtime
    Train --> Factory
    Factory -->|"produces"| Adapter
    Adapter -.->|"approved adapter only"| Runtime
    RAG -->|"documents, extracted text, snapshots"| Drive
    Index -->|"read-only chunks and model cache"| Drive
    Train -->|"datasets, adapters, evaluations, logs"| Drive
    Runtime -->|"model cache"| Drive
```

The arrows describe intended responsibilities and do not imply that a production queue, model server, or network protocol has been selected in Phase 0.

## Component responsibilities

### Next.js frontend

`apps/web` is the browser-facing interface. In future phases it is expected to provide department-scoped document management, ingestion status, chat, training and evaluation views, and administrative controls. It must not be treated as an authorization boundary; the API must independently authenticate and authorize every operation.

### FastAPI backend

`apps/api` is the control plane for development authentication, persistent department authorization, department administration, uploads, and extraction metadata. It enqueues PostgreSQL jobs but never opens sources for extraction, invokes parsers, normalizes, chunks, or waits for workers.

### PostgreSQL

PostgreSQL stores identities, departments, memberships, documents, extraction/chunk metadata, vector-indexing job metadata, and safe mutation audit events. It is the reviewed extraction/indexing queue: workers claim with `SKIP LOCKED` and finite non-revivable leases. Text, vectors, credentials, and filesystem paths never enter PostgreSQL.

### Qdrant

Qdrant 1.13.4 is the Phase 6 vector store for chunks embedded with the pinned Qwen3 contract. The fixed collection accepts exactly one named vector, `dense`; the adapter performs no point operation until the complete vector and payload-index schema is verified. Every operation requires typed `DepartmentScope`; fixed internal filters always include exact `department_id`, and searchable operations also require current pipeline plus `published=true`. Claim-owned mutations additionally require a live exact PostgreSQL claim and fixed contract. Payload contains IDs/provenance only, never text or hashes. Direct client calls outside the reviewed adapter are forbidden. Public search remains deferred.

### RAG worker and future LlamaIndex

The extraction path stream-copies each canonical source into a private verified claim snapshot and gives only that read-only descriptor to the installed constrained parser. It publishes exactly `normalized.txt`, `chunks.jsonl`, and `manifest.json`. The separate indexing path revalidates those artifacts incrementally, sends bounded requests to a secret-free offline embedding subprocess through interruptible nonblocking IPC, and stages content-free Qdrant points before exact-attempt activation. Reclaim verifies prior-attempt cleanup before processing and again before activation. Both use PostgreSQL server-time leases and exact stale cleanup. LlamaIndex, public retrieval, and query assembly remain future work.

Retrieved text is untrusted content. Prompt assembly must delimit it as evidence, prevent instructions in it from overriding higher-priority policy, and include only sources from the authorized department. If retrieval does not yield usable evidence, the assistant must state that it does not have enough information rather than generate a department-specific claim.

### Qwen3 and Qwen3-Embedding

Qwen3 remains the future base SLM. Phase 6 fixes `Qwen/Qwen3-Embedding-0.6B` revision `d23109d65ca9fdf61eef614209744716f337f50f`, normalized 1024-dimensional output, and cosine distance. Normal workers load only verified external safetensors offline with remote code disabled. Hardware/bitwise reproducibility, production serving, and final licensing review remain operational limitations; weights and caches never enter Git.

### LLaMA-Factory and the training worker

The training worker is planned to launch controlled LoRA or QLoRA jobs through LLaMA-Factory. Training data, outputs, logs, and adapters will live under `DEPTSLM_DATA_DIR`. Every dataset, job, evaluation, and adapter will be bound to a `department_id` and an exact base-model revision. Adapters should be evaluated and explicitly promoted before use; no cross-department adapter fallback is permitted.

### Shared package

`packages/shared` is reserved for contracts or utilities that genuinely need to be shared. It should not become a dumping ground or create a runtime dependency from Python to TypeScript; cross-language contracts should use an explicit schema or generated client once APIs stabilize.

## Planned workflows

### Document ingestion

1. The API authenticates the user, performs a short admission check, and validates the raw upload headers.
2. The upload streams to a private staging file beneath that department's external `uploads` path.
3. A new transaction locks the department, revalidates authority, enforces quota, atomically finalizes the source, and records metadata plus audit evidence.
4. The Phase 5 RAG worker claims the PostgreSQL job, creates and verifies an immutable source snapshot, extracts through the constrained subprocess and separate scratch space, re-verifies the canonical source, and publishes the exact normalized/chunk/manifest allowlist with page/line/character provenance.
5. The Phase 6 indexing worker validates the exact artifacts and PostgreSQL chunk rows, creates bounded offline embeddings, and stages content-free points with exact department/job/attempt scope.
6. It verifies count, revalidates PostgreSQL authority, repeats exact prior-attempt cleanup when reclaiming, activates only the replacement attempt, and then records job success plus audit metadata.
7. Future retrieval must filter by department/current publication and cross-check every result against succeeded PostgreSQL authority.

Phase 5 adds explicit failed-attempt retry, exact expired-claim staging recovery, and cancellation of queued work on soft deletion. A never-reclaimed crash can retain staging, and a crash between filesystem publication and database commit can retain an unknown final orphan. Malware controls, OCR, download, physical retention, and final-orphan reconciliation remain deferred.

### Department-scoped question answering

1. The API authenticates the caller and resolves the authorized department.
2. Retrieval queries Qdrant with a mandatory `department_id` filter.
3. The system evaluates whether retrieved passages are relevant enough to use.
4. LlamaIndex assembles a prompt that treats passages as untrusted evidence, not instructions.
5. Qwen3 generates an answer using an approved adapter only when one is configured for the same department.
6. The response returns source metadata for supported claims. With no adequate source, it returns the defined insufficient-information behavior.

### Adapter training and promotion

1. An authorized operator creates or selects a reviewed department dataset.
2. The training worker records the base-model revision and LLaMA-Factory configuration.
3. LLaMA-Factory produces a department-bound adapter under external storage.
4. Automated and human evaluation compare the candidate with the current approved behavior.
5. An authorized promotion action makes the adapter available to that department; rollback remains possible.

The exact training scheduler, GPU execution environment, registry schema, and approval workflow are future decisions.

## Isolation and trust boundaries

`department_id` is a mandatory security boundary, not a UI filter. In future phases it must be enforced in authentication-derived request context, PostgreSQL queries and constraints, Qdrant payload filters, job messages, paths, cache keys, adapters, logs, evaluations, and exports. Client-provided identifiers are not sufficient authorization. Missing or ambiguous scope must fail closed.

The browser, uploaded files, extracted text, document metadata, retrieved passages, and model output are untrusted. The API must validate inputs and authorize operations; prompt assembly must resist document-borne instructions; rendered output must be escaped for its context. Secrets should enter through environment or a future secret manager and must not be exposed to prompts or logs.

## Persistence boundary

The repository is for source code only. All file-based runtime artifacts derive from the required `DEPTSLM_DATA_DIR`; in the user's local environment it points to Google Drive. No component may silently create runtime directories inside the checkout. Tests and CI substitute isolated temporary directories. See [storage-policy.md](storage-policy.md).

PostgreSQL and Qdrant are service state. The Compose stack is for local development only; before either stores real data, its persistence, backup, and recovery design must be reviewed to ensure no runtime files are written into the repository and that department deletion and retention requirements can be met.

## Deferred decisions

- Authentication provider, SSO integration, and role model
- Production queue/worker scaling beyond the Phase 5 PostgreSQL lease queue
- Exact Qwen3 variants, serving runtime, and hardware profiles
- Production extraction sandbox, malware controls, and additional reviewed formats
- Hybrid retrieval, reranking, and relevance thresholds beyond the Phase 5 character chunker
- Production retention, physical purge, reconciliation, and tamper-resistant audit requirements
- Adapter evaluation gates and promotion workflow
- Production topology, secrets, observability, backup, and disaster recovery

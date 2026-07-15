# DeptSLM

DeptSLM is a university departmental small language model (SLM) customization platform. It is intended to let each department build an isolated assistant from its own approved documents, retrieval index, evaluation data, and eventually its own LoRA or QLoRA adapter.

> **Phase 5 status:** PostgreSQL-backed extraction jobs, immutable verified source snapshots, constrained PDF/text/Markdown parsing, deterministic forward-progress chunking, exact three-file publication, provenance, non-revivable leases, exact stale-claim cleanup, and safe retries are under review. OCR, malware scanning, download, Qdrant, embeddings, RAG, model serving, fine-tuning, and production identity/storage remain deferred.

The API only enqueues and reads extraction metadata; parsing runs in the RAG worker from a claim-owned verified snapshot with separate scratch space. Extracted and chunk text remains external and has no API. The parser boundary is constrained but is not a kernel-enforced malware sandbox. Never-reclaimed staging and crash-time final-orphan reconciliation remain deferred.

## Planned stack

- Next.js and TypeScript for the web application
- FastAPI for the HTTP API
- PostgreSQL for application metadata
- Qdrant for vector search
- LlamaIndex for document ingestion and RAG query workflows
- Qwen3 as the target base SLM
- Qwen3-Embedding as the target embedding model
- LLaMA-Factory for LoRA and QLoRA fine-tuning
- Docker Compose for local development

## Repository layout

```text
DeptSLM/
├── apps/
│   ├── api/                  # FastAPI application
│   └── web/                  # Next.js application
├── services/
│   ├── rag-worker/           # Phase 5 extraction and chunking jobs
│   └── training-worker/      # Future fine-tuning jobs
├── packages/
│   └── shared/               # Future shared contracts and utilities
├── data/
│   ├── eval_sets/            # Small, synthetic, versioned fixtures only
│   └── sample_docs/          # Small, synthetic, versioned fixtures only
├── docs/                     # Product and engineering documentation
├── scripts/                  # Developer setup scripts
├── .env.example
└── docker-compose.yml
```

## Runtime storage is outside this repository

GitHub stores source code and safe, synthetic fixtures only. Uploaded documents, extracted text, vector snapshots, training datasets, adapters, model files, caches, logs, evaluation results, and exports must never be written to or committed in this repository.

Every local runtime component that writes file-based persistent data must use `DEPTSLM_DATA_DIR`. On macOS, this should point to a `DeptSLM` folder in Google Drive. Applications must fail with a clear error when the variable is missing; they must not fall back to a path inside the checkout. Tests and CI must use temporary directories.

The setup script detects both `My Drive` and the localized `我的雲端硬碟` directory used by Traditional Chinese Google Drive installations. It chooses the strongest unambiguous match and stops without writing when multiple locations are equally suitable. Phase 0 Compose service state is also kept beneath this external root in `service_state/`.

See [Storage policy](docs/storage-policy.md) for the complete rules.

## Local setup

Prerequisites for the complete local stack are Git, Docker Desktop with Docker Compose, and a local Google Drive mount on macOS. Running the apps outside containers requires Node.js 20 or newer and Python 3.11 or newer.

1. Clone the repository and enter it:

   ```bash
   git clone https://github.com/Easonsusu/DeptSLM.git
   cd DeptSLM
   ```

2. Create the external runtime directory:

   ```bash
   ./scripts/setup_google_drive_storage.sh
   ```

   The script is safe to run repeatedly. Copy the printed `DEPTSLM_DATA_DIR` value.

3. Create a local environment file and replace the example storage path with the value printed by the script:

   ```bash
   cp .env.example .env
   ```

   Never commit `.env`.

4. Validate and start the local Compose project:

   ```bash
   ./scripts/compose.sh config
   ./scripts/compose.sh run --rm api python -m alembic upgrade head
   ./scripts/compose.sh up --build
   ```

   The wrapper validates the complete external directory layout and sets a guard required by `docker-compose.yml`; invoking `docker compose` directly is intentionally rejected.

5. Check the API skeleton:

   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/version
   ```

   Protected identity checks additionally require the development/test authentication variables documented in [.env.example](.env.example). Compose passes those variables only to the API container; the generated secret remains only in the untracked `.env`. The Compose migration command uses the internal `postgres` hostname. Host-shell Alembic commands must override `DATABASE_URL` with a host-accessible `localhost` URL. HS256 is allowed only with an explicit reviewed local environment and a non-placeholder secret of at least 32 bytes.

   Bootstrap the first local department only after migration:

   ```bash
   ./scripts/compose.sh run --rm api python -m app.admin bootstrap-department \
     --slug computer-science --display-name "Computer Science" \
     --admin-issuer https://local-issuer.invalid --admin-subject opaque-admin
   ```

   The default ports are controlled by `API_PORT` and `WEB_PORT` in `.env`.

Stop the stack with:

```bash
./scripts/compose.sh down
```

Run at most one extraction job or poll continuously with:

```bash
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --once
./scripts/compose.sh run --rm rag-worker python -m deptslm_worker --poll
```

## Safety and data isolation

- Future department-owned records, documents, indexes, jobs, adapters, and conversations must be scoped and authorized by `department_id` at every storage and service boundary.
- Retrieved document text is untrusted input. It must be quoted as context and must never be allowed to override system or developer instructions.
- If retrieval returns no usable source, the assistant must say that it does not have enough information. It must not invent a department-specific answer.
- Secrets, model weights, and runtime artifacts do not belong in Git history.

Contributor rules are in [AGENTS.md](AGENTS.md).

Contribution workflow and validation guidance are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

- [Product specification](docs/product-spec.md)
- [Architecture](docs/architecture.md)
- [Storage policy](docs/storage-policy.md)
- [API](docs/api.md)
- [Deployment](docs/deployment.md)
- [Roadmap](docs/roadmap.md)
- [Department and authentication boundaries](docs/department-auth-boundaries.md)
- [Authentication foundation](docs/authentication-foundation.md)
- [Database model](docs/database-model.md)
- [Department and membership API](docs/department-membership-api.md)
- [Document model](docs/document-model.md)
- [Document upload](docs/document-upload.md)
- [Document extraction](docs/document-extraction.md)
- [Chunk model](docs/chunk-model.md)
- [RAG worker](docs/rag-worker.md)

## Current non-goals

Phase 5 does not implement production OAuth/OIDC/SSO, platform administration, frontend ingestion UI, OCR, malware scanning, download/preview, Qdrant, embeddings, LlamaIndex, RAG, model inference, fine-tuning, or production deployment.

## License

No license has been selected in Phase 0. Until one is added, normal copyright restrictions apply.

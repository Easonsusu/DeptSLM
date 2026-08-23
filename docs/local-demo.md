# Synthetic local Docker demo

`scripts/demo.sh` is a deterministic, test-only demonstration of the reviewed
local path. It creates a fresh mode-700 runtime root outside the checkout, a
mode-600 temporary environment file, and a unique Compose project. It never
reads the repository `.env`, the user's Google Drive folder, real university
data, or real model weights. Fake embedding and base-runtime providers are
used with the production model revisions still pinned in the environment; the
demo-only retrieval threshold is intentionally not a production quality gate.

Run it from the repository root:

```bash
./scripts/demo.sh
```

The script validates Compose, builds and starts only the required services,
applies Alembic head `0018_phase14_training_execution_control_plane`, bootstraps two
synthetic departments through the local administrator command, creates a local
HS256 identity, and exercises health/version/authentication, raw UTF-8 upload,
extraction, fake embedding/indexing, succeeded metadata, and the public
department-scoped RAG answer path with a citation. A token for Department A is
also checked against Department B and must receive `403`. The web landing page
is checked when the web service is started.

While the services are running it performs real container DNS probes: web can
reach only the API, the API reaches PostgreSQL/Qdrant/base runtime and the
production adapter runtime but not the adapter-evaluation runtime, extraction
cannot resolve Qdrant, evaluator and adapter-evaluator workers reach only their
declared PostgreSQL/Qdrant/runtime peers, and the three runtime domains cannot
resolve one another. Required indexing paths and the model-admin egress
boundary are checked without publishing extra host ports.

The demo proves image/build compatibility, local authentication and department
authorization, raw upload, extraction, fake indexing/Qdrant orchestration,
base-runtime routing, citation validation, network segmentation, and external
runtime cleanup. It does not prove Qwen answer or embedding quality, adapter
quality or PEFT loading, training, production identity/TLS/availability,
backup/restore durability, or production security certification. Optional
real-model smoke tests remain separate and opt-in.

The script traps cleanup, removes the unique project and temporary data/env,
scans logs for secrets and synthetic question/answer sentinels, and verifies
that the checkout remains clean. It never performs broad deletion against the
normal Compose project or the user's normal `DEPTSLM_DATA_DIR`.

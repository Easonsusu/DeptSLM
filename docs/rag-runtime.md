# Phase 7 Internal RAG Runtime

## Boundary

`services/rag-runtime` is a private, non-root, read-only container on an internal Compose network. It has no host port, database URL, Qdrant URL or key, API JWT secret, Google Drive credentials, tools, or outbound model-download behavior. It mounts only `DEPTSLM_DATA_DIR/model_cache` read-only and runs with Hugging Face and Transformers offline flags.

The API authenticates to it with a long untracked bearer token using two narrow endpoints:

- `POST /internal/v1/query-embedding`
- `POST /internal/v1/generate`

Requests have manual byte limits before JSON parsing, exact field allowlists, normalized bounded questions, at most eight sequential server labels, and at most 6,000 evidence characters. A nonblocking single-operation capacity gate rejects overlap. Errors are generic and do not echo content or dependency details.

The FastAPI process is only an HTTP supervisor. One dedicated child process loads both pinned models once and communicates through bounded length-delimited JSON frames. Request transmission and response receipt share a fixed monotonic deadline. Timeout, malformed or oversized output, child exit, client disconnect, task cancellation, or runtime shutdown terminates and reaps the whole child process group; capacity is released and the next accepted request starts a clean child. An API-side timeout alone is not treated as proof that inference stopped—the runtime actively observes disconnection and kills the child. Successful requests reuse the persistent child rather than reload models.

The child inherits closed descriptors and an exact environment allowlist containing only offline/model execution settings, fixed roots and revisions, test/real provider selection, and safe locale/encoding values. It receives no runtime HTTP bearer token, database or Qdrant setting, application authentication secret, Hugging Face token, cloud credential, proxy variable, or unrelated host environment. The supervisor itself fails startup if forbidden project secrets or proxy values are unexpectedly present. The container mounts only `model_cache`, so the child has no upload or extracted-text mount.

## Exact models

Question embeddings use `Qwen/Qwen3-Embedding-0.6B` revision `d23109d65ca9fdf61eef614209744716f337f50f`, exact query instruction, normalized 1,024-dimensional output, and cosine semantics.

Generation uses `Qwen/Qwen3-0.6B` revision `c1899de289a04d12100db370d81485cdf75e47ca`, `trust_remote_code=False`, local-files-only safetensors, `enable_thinking=False`, and at most 512 new tokens. The pinned model context contract is 40,960 tokens, while the lower reviewed operational input limit is 8,192 tokens; the runtime requires input tokens plus the 512-token reserve to fit and never truncates. Query-embedding input is independently limited to 2,048 exact tokenizer tokens. Both complete inputs are tokenized before inference, pinned tokenizer/model context metadata is validated, and any over-limit or mismatched configuration fails with content-free output. No environment value can expand these bounds. The reviewed sampling contract uses temperature `0.7`, top-p `0.8`, top-k `20`, and min-p `0.0`.

Normal startup validates exact external integrity manifests and never downloads or silently substitutes models. `model-admin prepare-rag-models` is the only reviewed download/preparation path and stages entirely beneath external `model_cache`. No model asset or Hugging Face cache belongs in the image, repository, home cache, or temporary directory.

## Test boundary

A deterministic fake provider is permitted only with exact `ENVIRONMENT=test` and explicit fake configuration. It uses the actual pure prompt builder, returns deterministic normalized query embeddings and schema-valid content-free test responses, and never downloads real models. Controlled-child tests cover blocked embedding/generation, deadline, cancellation, shutdown, child exit, malformed/oversized frames, restart, reaping, capacity release, and exact secret-free environment behavior.

The real two-model smoke test is opt-in only. It requires both exact model directories already prepared under an external temporary/test data root, an explicit switch, and offline environment. Missing assets cause a skip, never a download.

## Limitations

The local container and process boundaries are defense in depth, not a production model-serving platform. PostgreSQL, Qdrant, artifacts, runtime child IPC, runtime HTTP, and API HTTP are not transactionally atomic. Production TLS/mTLS, network policy, credential rotation, GPU scheduling, queueing, autoscaling, timeout calibration under hardware load, observability without content leakage, model distribution, license review, and denial-of-service controls remain deferred.

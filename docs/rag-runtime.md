# Phase 7 Internal RAG Runtime

## Boundary

`services/rag-runtime` is a private, non-root, read-only container on an internal Compose network. It has no host port, database URL, Qdrant URL or key, API JWT secret, Google Drive credentials, tools, or outbound model-download behavior. It mounts only `DEPTSLM_DATA_DIR/model_cache` read-only and runs with Hugging Face and Transformers offline flags.

The API authenticates to it with a long untracked bearer token using two narrow endpoints:

- `POST /internal/v1/query-embedding`
- `POST /internal/v1/generate`

Requests have manual byte limits before JSON parsing, exact field allowlists, normalized bounded questions, at most eight sequential server labels, and at most 6,000 evidence characters. A nonblocking capacity gate bounds concurrent model operations. Errors are generic and do not echo content or dependency details.

## Exact models

Question embeddings use `Qwen/Qwen3-Embedding-0.6B` revision `d23109d65ca9fdf61eef614209744716f337f50f`, exact query instruction, normalized 1,024-dimensional output, and cosine semantics.

Generation uses `Qwen/Qwen3-0.6B` revision `c1899de289a04d12100db370d81485cdf75e47ca`, `trust_remote_code=False`, local-files-only safetensors, `enable_thinking=False`, and at most 512 new tokens. The reviewed sampling contract uses temperature `0.7`, top-p `0.8`, top-k `20`, and min-p `0.0`.

Normal startup validates exact external integrity manifests and never downloads or silently substitutes models. `model-admin prepare-rag-models` is the only reviewed download/preparation path and stages entirely beneath external `model_cache`. No model asset or Hugging Face cache belongs in the image, repository, home cache, or temporary directory.

## Test boundary

A deterministic fake provider is permitted only with exact `ENVIRONMENT=test` and explicit fake configuration. It returns deterministic normalized query embeddings and schema-valid content-free test responses. Other environments reject it. Normal CI uses the fake boundary and never downloads real models.

The real two-model smoke test is opt-in only. It requires both exact model directories already prepared under an external temporary/test data root, an explicit switch, and offline environment. Missing assets cause a skip, never a download.

## Limitations

The local container boundary is defense in depth, not a production model-serving platform. Production TLS/mTLS, network policy, credential rotation, GPU scheduling, queueing, autoscaling, timeouts under hardware load, observability without content leakage, model distribution, license review, and denial-of-service controls remain deferred.

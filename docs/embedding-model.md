# Phase 6 Embedding Model

## Reviewed contract

Phase 6 embeds document chunks with [`Qwen/Qwen3-Embedding-0.6B` at immutable revision `d23109d65ca9fdf61eef614209744716f337f50f`](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/tree/d23109d65ca9fdf61eef614209744716f337f50f). The embedding pipeline is `phase6-qwen3-embedding-v1`: chunk text is encoded without a query instruction, model-provided last-token pooling is used through sentence-transformers, normalization is requested, and every result must contain exactly 1024 finite floats with cosine distance semantics.

Runtime accepts only normalized, nonzero vectors within a narrow norm tolerance. Wrong dimensions, Boolean or nonnumeric values, NaN, infinity, effectively zero vectors, and excessive values fail safely. Identical text uses the same procedure, but bitwise equality across hardware is not promised. Filenames, IDs, provenance, and instructions are never concatenated with chunk text.

The reviewed dependency ranges are sentence-transformers 5.x, Transformers 4.51 or newer but below 5, safetensors 0.5 or newer but below 1, and Hugging Face Hub 0.30 or newer but below 1. `trust_remote_code=False` and offline loading are mandatory. Python/model repository code, pickle weights, PyTorch `.bin` weights, native libraries, scripts, symlinks, and unexpected manifest files are rejected.

## Explicit preparation

Normal workers never download models. An administrator prepares only the fixed revision into external storage:

```bash
./scripts/compose.sh run --rm model-admin \
  python -m deptslm_worker.model_admin prepare-embedding
```

For gated access only, add `-e HF_TOKEN` after `run --rm` to forward an already-exported, untracked token to this service. The public model needs no token. The command never prints or persists the token and never passes it to ordinary workers, PostgreSQL, Qdrant, the API, or parser subprocesses. Download staging, the Hugging Face cache, real files, and the final integrity manifest remain beneath `DEPTSLM_DATA_DIR/model_cache`. The resolved upstream SHA must equal the reviewed revision before publication.

Preparation writes `deptslm-model-manifest.json` with the model ID, immutable revision, dimension, pooling/library contract, remote-code prohibition, and SHA-256 plus size for every asset. Runtime revalidates the real non-symlink directory, exact file allowlist, sizes, and digests before loading. It never falls back to a home cache, repository path, network, another revision, or another model.

## Process and test boundary

The indexing parent launches one persistent isolated subprocess per job with a fixed executable and runner path, `shell=False`, a new process session, closed unrelated descriptors, a minimal environment, and offline flags. Only a sequence number, bounded chunk-text batch, and bounded vector response cross the pipe. Database URLs, Qdrant settings, authentication configuration, bearer tokens, department/document/user identities, filenames, and host paths are absent. Timeout, shutdown, claim loss, malformed output, or child exit terminates the process group.

The deterministic fake provider is permitted only when `ENVIRONMENT=test` is exact. It is never the default and startup rejects it in local development, preview, staging, production, unknown, missing, or misspelled environments. CI does not download the real model. A real-model smoke test is opt-in and must use an already prepared exact cache with networking disabled.

Phase 6 adds no query-embedding API, reranker, generation model, RAG, LlamaIndex, or frontend model behavior.

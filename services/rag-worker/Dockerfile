FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS worker-source

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY apps/api/requirements-worker.lock /build/api-requirements.lock
COPY apps/api/requirements.lock /build/api-rag-requirements.lock
COPY services/rag-worker/requirements-extraction.lock /build/extraction-requirements.lock
COPY services/rag-worker/requirements-indexing.lock /build/indexing-requirements.lock
COPY services/rag-worker/requirements-vector.lock /build/vector-requirements.lock

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /runtime/deptslm/uploads \
        /runtime/deptslm/extracted_text \
        /runtime/deptslm/eval_results \
        /runtime/deptslm/model_cache \
    && chown -R deptslm:deptslm /runtime/deptslm

COPY apps/api/pyproject.toml /build/api/pyproject.toml
COPY apps/api/app /build/api/app
COPY services/rag-worker/pyproject.toml /build/worker/pyproject.toml
COPY services/rag-worker/deptslm_worker /build/worker/deptslm_worker
COPY --chown=deptslm:deptslm services/rag-worker/entrypoint.sh ./entrypoint.sh

FROM worker-source AS extraction
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /build/extraction-requirements.lock \
    && pip install --no-cache-dir --disable-pip-version-check --no-deps /build/api \
    && pip install --no-cache-dir --disable-pip-version-check --no-deps /build/worker \
    && rm -rf /build

USER deptslm
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "-m", "deptslm_worker", "--poll"]

FROM worker-source AS indexing
ENV DEPTSLM_STORAGE_READ_ONLY=1
# The indexing worker performs local embeddings and therefore uses the
# separately reviewed, model-pinned vector lock rather than the vector-free
# metadata/indexing lock.
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /build/vector-requirements.lock \
    && pip install --no-cache-dir --disable-pip-version-check --no-deps /build/api \
    && pip install --no-cache-dir --disable-pip-version-check --no-deps /build/worker \
    && rm -rf /build

USER deptslm
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "-m", "deptslm_worker.indexer", "--poll"]

FROM worker-source AS evaluator
ENV DEPTSLM_STORAGE_READ_ONLY=1
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /build/api-rag-requirements.lock \
    && pip install --no-cache-dir --disable-pip-version-check --no-deps /build/worker \
    && rm -rf /build

USER deptslm
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "-m", "deptslm_worker.evaluator", "--poll"]

FROM worker-source AS adapter-evaluator
ENV DEPTSLM_STORAGE_READ_ONLY=1
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /build/api-rag-requirements.lock \
    && pip install --no-cache-dir --disable-pip-version-check --no-deps /build/worker \
    && rm -rf /build

USER deptslm
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "-m", "deptslm_worker.adapter_evaluator", "--poll"]

FROM extraction AS final

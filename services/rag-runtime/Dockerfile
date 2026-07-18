FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    HOME=/nonexistent

WORKDIR /app

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /runtime/deptslm/model_cache \
    && chown -R deptslm:deptslm /runtime/deptslm

COPY apps/api/pyproject.toml /build/api/pyproject.toml
COPY apps/api/app /build/api/app
COPY services/rag-runtime/pyproject.toml /build/runtime/pyproject.toml
COPY services/rag-runtime/deptslm_runtime /build/runtime/deptslm_runtime
COPY services/rag-worker/deptslm_worker/__init__.py /app/deptslm_worker/__init__.py
COPY services/rag-worker/deptslm_worker/model_store.py /app/deptslm_worker/model_store.py

RUN pip install --no-cache-dir --disable-pip-version-check --no-deps /build/api \
    && pip install --no-cache-dir --disable-pip-version-check /build/runtime \
    && rm -rf /build

USER deptslm
EXPOSE 8010

CMD ["uvicorn", "deptslm_runtime.main:app", "--host", "0.0.0.0", "--port", "8010", "--no-access-log"]

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
    && mkdir -p /runtime/deptslm/model_cache /runtime/deptslm/adapters/registry /tmp/adapter-eval \
    && chown -R deptslm:deptslm /runtime/deptslm /tmp/adapter-eval

COPY apps/api/pyproject.toml /build/api/pyproject.toml
COPY apps/api/app /build/api/app
COPY services/adapter-eval-runtime/pyproject.toml /build/runtime/pyproject.toml
COPY services/adapter-eval-runtime/deptslm_adapter_runtime /build/runtime/deptslm_adapter_runtime

RUN pip install --no-cache-dir --disable-pip-version-check --no-deps /build/api \
    && pip install --no-cache-dir --disable-pip-version-check /build/runtime \
    && rm -rf /build

USER deptslm
EXPOSE 8011

CMD ["uvicorn", "deptslm_adapter_runtime.main:app", "--host", "0.0.0.0", "--port", "8011", "--no-access-log"]

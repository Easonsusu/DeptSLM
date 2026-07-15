FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /runtime/deptslm/uploads /runtime/deptslm/extracted_text \
    && chown -R deptslm:deptslm /runtime/deptslm

COPY apps/api/pyproject.toml /build/api/pyproject.toml
COPY apps/api/app /build/api/app
COPY services/rag-worker/pyproject.toml /build/worker/pyproject.toml
COPY services/rag-worker/deptslm_worker /build/worker/deptslm_worker
RUN pip install --no-cache-dir --disable-pip-version-check --no-deps /build/api \
    && pip install --no-cache-dir --disable-pip-version-check /build/worker \
    && rm -rf /build

COPY --chown=deptslm:deptslm services/rag-worker/entrypoint.sh ./entrypoint.sh

USER deptslm
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "-m", "deptslm_worker", "--poll"]

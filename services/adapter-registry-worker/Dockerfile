FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /runtime/deptslm/adapters/imports /runtime/deptslm/adapters/registry /runtime/deptslm/adapters/.staging/registry \
    && chown -R deptslm:deptslm /runtime/deptslm

COPY apps/api/pyproject.toml /build/api/pyproject.toml
COPY apps/api/app /build/api/app
COPY --chown=deptslm:deptslm services/adapter-registry-worker/entrypoint.sh ./entrypoint.sh

RUN pip install --no-cache-dir --disable-pip-version-check /build/api \
    && rm -rf /build

USER deptslm
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "-m", "app.adapter_registry_worker", "--poll"]

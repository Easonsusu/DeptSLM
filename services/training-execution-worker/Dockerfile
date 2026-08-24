FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY apps/api/requirements-worker.lock /build/requirements-worker.lock

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /runtime/deptslm/training_runs /run/deptslm \
    && chown -R deptslm:deptslm /runtime/deptslm /run/deptslm

COPY apps/api/pyproject.toml /build/pyproject.toml
COPY apps/api/app /build/app
COPY apps/api/alembic.ini /build/alembic.ini
COPY apps/api/alembic /build/alembic

RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /build/requirements-worker.lock \
    && pip install --no-cache-dir --disable-pip-version-check --no-deps /build \
    && rm -rf /build

USER deptslm
ENTRYPOINT ["python", "-m", "app.training_execution_worker", "--poll"]

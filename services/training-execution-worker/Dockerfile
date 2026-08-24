FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /runtime/deptslm/training_runs /run/deptslm \
    && chown -R deptslm:deptslm /runtime/deptslm /run/deptslm

COPY apps/api/pyproject.toml /build/pyproject.toml
COPY apps/api/app /build/app
COPY apps/api/alembic.ini /build/alembic.ini
COPY apps/api/alembic /build/alembic

RUN pip install --no-cache-dir --disable-pip-version-check /build \
    && rm -rf /build

USER deptslm
ENTRYPOINT ["python", "-m", "app.training_execution_worker", "--poll"]

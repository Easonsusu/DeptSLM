FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /runtime/deptslm/uploads /runtime/deptslm/extracted_text \
    && chown -R deptslm:deptslm /runtime/deptslm

COPY apps/api/requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY --chown=deptslm:deptslm apps/api/app ./app
COPY --chown=deptslm:deptslm apps/api/alembic.ini ./alembic.ini
COPY --chown=deptslm:deptslm apps/api/alembic ./alembic
COPY --chown=deptslm:deptslm services/rag-worker/deptslm_worker/__init__.py ./deptslm_worker/__init__.py
COPY --chown=deptslm:deptslm services/rag-worker/deptslm_worker/artifact_reader.py ./deptslm_worker/artifact_reader.py
COPY --chown=deptslm:deptslm services/rag-worker/deptslm_worker/embedding.py ./deptslm_worker/embedding.py
COPY --chown=deptslm:deptslm services/rag-worker/deptslm_worker/qdrant_adapter.py ./deptslm_worker/qdrant_adapter.py
COPY --chown=deptslm:deptslm services/rag-worker/deptslm_worker/vector_retrieval.py ./deptslm_worker/vector_retrieval.py

USER deptslm
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

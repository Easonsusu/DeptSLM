FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm

COPY --chown=deptslm:deptslm worker.py entrypoint.sh ./

USER deptslm
ENTRYPOINT ["sh", "/app/entrypoint.sh"]

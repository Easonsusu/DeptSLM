FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    HOME=/nonexistent \
    PATH=/opt/llamafactory/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/llamafactory

RUN apt-get update \
    && apt-get install --no-install-recommends -y python3.12 python3.12-venv python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3.12 -m venv /opt/llamafactory \
    && /opt/llamafactory/bin/python -m pip install --no-cache-dir --upgrade pip \
    && addgroup --system --gid 1001 deptslm \
    && adduser --system --uid 1001 --ingroup deptslm deptslm \
    && mkdir -p /run/deptslm /runtime/deptslm/model_cache \
    && chown -R deptslm:deptslm /opt/llamafactory /run/deptslm /runtime/deptslm

COPY services/training-runtime/requirements.lock /opt/llamafactory/requirements.lock
COPY services/training-runtime/environment.json /opt/llamafactory/environment.json
COPY services/training-runtime/entrypoint.sh /usr/local/bin/deptslm-training-runtime
COPY services/training-runtime/pyproject.toml /opt/build/pyproject.toml
COPY services/training-runtime/deptslm_training_runtime /opt/build/deptslm_training_runtime

RUN /opt/llamafactory/bin/python -m pip install --no-cache-dir --requirement /opt/llamafactory/requirements.lock \
    && /opt/llamafactory/bin/python -m pip check \
    && /opt/llamafactory/bin/python -m pip install --no-cache-dir --no-deps /opt/build \
    && rm -rf /opt/build \
    && chmod 0755 /usr/local/bin/deptslm-training-runtime

USER deptslm
ENTRYPOINT ["/usr/local/bin/deptslm-training-runtime"]

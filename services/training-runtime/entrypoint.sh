#!/bin/sh
set -eu
umask 077
if [ -z "${DEPTSLM_TRAINING_RUNTIME_TOKEN:-}" ] || [ "${#DEPTSLM_TRAINING_RUNTIME_TOKEN}" -lt 32 ]; then
  echo "training-runtime error: private runtime token is required" >&2
  exit 1
fi
exec /opt/llamafactory/bin/python -m deptslm_training_runtime "$@"

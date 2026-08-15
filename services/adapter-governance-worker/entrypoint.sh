#!/bin/sh
set -eu

if [ -z "${DEPTSLM_DATA_DIR:-}" ]; then
  echo "adapter-governance-worker error: DEPTSLM_DATA_DIR is required." >&2
  exit 1
fi
case "$DEPTSLM_DATA_DIR" in
  /*) ;;
  *) echo "adapter-governance-worker error: DEPTSLM_DATA_DIR must be absolute." >&2; exit 1 ;;
esac
if [ ! -d "$DEPTSLM_DATA_DIR" ] || [ ! -x "$DEPTSLM_DATA_DIR" ]; then
  echo "adapter-governance-worker error: runtime storage is unavailable." >&2
  exit 1
fi
registry="$DEPTSLM_DATA_DIR/adapters/registry"
if [ ! -d "$registry" ] || [ ! -r "$registry" ] || [ ! -x "$registry" ]; then
  echo "adapter-governance-worker error: read-only registry storage is unavailable." >&2
  exit 1
fi
if [ "${DEPTSLM_STORAGE_READ_ONLY:-1}" != "1" ]; then
  echo "adapter-governance-worker error: registry storage must be read-only." >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  set -- python -m app.adapter_governance_worker --poll
fi
exec "$@"

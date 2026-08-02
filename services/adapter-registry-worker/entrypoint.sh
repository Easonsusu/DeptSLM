#!/bin/sh
set -eu

if [ -z "${DEPTSLM_DATA_DIR:-}" ]; then
  echo "adapter-registry-worker error: DEPTSLM_DATA_DIR is required." >&2
  exit 1
fi
case "$DEPTSLM_DATA_DIR" in
  /*) ;;
  *) echo "adapter-registry-worker error: DEPTSLM_DATA_DIR must be absolute." >&2; exit 1 ;;
esac
if [ ! -d "$DEPTSLM_DATA_DIR" ]; then
  echo "adapter-registry-worker error: DEPTSLM_DATA_DIR is unavailable." >&2
  exit 1
fi
resolved_data_dir=$(CDPATH= cd -- "$DEPTSLM_DATA_DIR" && pwd -P)
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=""
candidate="$script_dir"
while [ "$candidate" != "/" ]; do
  if [ -e "$candidate/.git" ]; then repository_root="$candidate"; break; fi
  candidate=$(dirname -- "$candidate")
done
if [ -n "$repository_root" ]; then
  case "$resolved_data_dir" in "$repository_root"|"$repository_root"/*) echo "adapter-registry-worker error: runtime storage must be outside source." >&2; exit 1 ;; esac
fi
for path in \
  "$resolved_data_dir/adapters/imports" \
  "$resolved_data_dir/training_datasets/jobs"; do
  if [ ! -d "$path" ] || [ ! -r "$path" ] || [ ! -x "$path" ]; then
    echo "adapter-registry-worker error: read-only source storage is unavailable." >&2
    exit 1
  fi
done
for path in \
  "$resolved_data_dir/adapters/registry" \
  "$resolved_data_dir/adapters/.staging/registry"; do
  if [ ! -d "$path" ] || [ ! -w "$path" ] || [ ! -x "$path" ]; then
    echo "adapter-registry-worker error: writable registry storage is unavailable." >&2
    exit 1
  fi
done

if [ "$#" -eq 0 ]; then
  set -- python -m app.adapter_registry_worker --poll
fi
exec "$@"

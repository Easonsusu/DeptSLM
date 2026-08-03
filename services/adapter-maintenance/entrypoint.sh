#!/bin/sh
set -eu

if [ -z "${DEPTSLM_DATA_DIR:-}" ]; then
  echo "adapter-maintenance error: DEPTSLM_DATA_DIR is required." >&2
  exit 1
fi
case "$DEPTSLM_DATA_DIR" in
  /*) ;;
  *) echo "adapter-maintenance error: DEPTSLM_DATA_DIR must be absolute." >&2; exit 1 ;;
esac
if [ ! -d "$DEPTSLM_DATA_DIR" ]; then
  echo "adapter-maintenance error: DEPTSLM_DATA_DIR is unavailable." >&2
  exit 1
fi
resolved_data_dir=$(CDPATH= cd -- "$DEPTSLM_DATA_DIR" && pwd -P)
for path in \
  "$resolved_data_dir/adapters" \
  "$resolved_data_dir/adapters/imports" \
  "$resolved_data_dir/adapters/registry" \
  "$resolved_data_dir/adapters/.staging/imports" \
  "$resolved_data_dir/adapters/.staging/registry" \
  "$resolved_data_dir/adapters/.deleting/source_stage" \
  "$resolved_data_dir/adapters/.deleting/source_final" \
  "$resolved_data_dir/adapters/.deleting/registry_stage" \
  "$resolved_data_dir/adapters/.deleting/registry_final" \
  "$resolved_data_dir/adapters/.purge-deleting/source_stage" \
  "$resolved_data_dir/adapters/.purge-deleting/source_final" \
  "$resolved_data_dir/adapters/.purge-deleting/registry_stage" \
  "$resolved_data_dir/adapters/.purge-deleting/registry_final"; do
  if [ ! -d "$path" ] || [ ! -r "$path" ] || [ ! -w "$path" ] || [ ! -x "$path" ]; then
    echo "adapter-maintenance error: adapter storage is unavailable." >&2
    exit 1
  fi
done

if [ "$#" -eq 0 ]; then
  set -- python -m app.admin reconcile-adapter-artifacts
fi
exec "$@"

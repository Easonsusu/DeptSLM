#!/bin/sh
set -eu

if [ -z "${DEPTSLM_DATA_DIR:-}" ]; then
  echo "rag-worker error: DEPTSLM_DATA_DIR is required; point it to external runtime storage." >&2
  exit 1
fi

case "$DEPTSLM_DATA_DIR" in
  /*) ;;
  *)
    echo "rag-worker error: DEPTSLM_DATA_DIR must be an absolute path." >&2
    exit 1
    ;;
esac

if [ ! -d "$DEPTSLM_DATA_DIR" ]; then
  echo "rag-worker error: DEPTSLM_DATA_DIR does not exist or is not a directory." >&2
  exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
resolved_data_dir=$(CDPATH= cd -- "$DEPTSLM_DATA_DIR" && pwd -P)

if [ "$resolved_data_dir" = "/" ]; then
  echo "rag-worker error: DEPTSLM_DATA_DIR must not be the filesystem root." >&2
  exit 1
fi

repository_root=""
candidate="$script_dir"
while [ "$candidate" != "/" ]; do
  if [ -e "$candidate/.git" ]; then
    repository_root="$candidate"
    break
  fi
  candidate=$(dirname -- "$candidate")
done

if [ -n "$repository_root" ]; then
  source_root="$repository_root"
  source_label="source repository"
else
  source_root="$script_dir"
  source_label="worker source tree"
fi

case "$resolved_data_dir" in
  "$source_root"|"$source_root"/*)
    echo "rag-worker error: DEPTSLM_DATA_DIR must be outside the $source_label." >&2
    exit 1
    ;;
esac

case "$source_root" in
  "$resolved_data_dir"|"$resolved_data_dir"/*)
    echo "rag-worker error: DEPTSLM_DATA_DIR must not contain the $source_label." >&2
    exit 1
    ;;
esac

case "${DEPTSLM_STORAGE_READ_ONLY:-0}" in
  0)
    if [ ! -w "$resolved_data_dir" ] || [ ! -x "$resolved_data_dir" ]; then
      echo "rag-worker error: DEPTSLM_DATA_DIR is not writable and searchable." >&2
      exit 1
    fi
    ;;
  1)
    if [ ! -x "$resolved_data_dir" ]; then
      echo "rag-worker error: DEPTSLM_DATA_DIR is not searchable." >&2
      exit 1
    fi
    ;;
  *)
    echo "rag-worker error: DEPTSLM_STORAGE_READ_ONLY must be 0 or 1." >&2
    exit 1
    ;;
esac

if [ "$#" -eq 0 ]; then
  set -- python -m deptslm_worker --poll
fi

exec "$@"

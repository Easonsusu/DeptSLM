#!/usr/bin/env bash

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "${script_dir}/.." && pwd -P)

if [[ $# -eq 0 ]]; then
  printf 'Usage: %s <docker compose arguments>\n' "$0" >&2
  exit 2
fi

resolved_data_dir=$("${script_dir}/validate_data_dir.sh" --require-compose-layout)
export DEPTSLM_DATA_DIR="${resolved_data_dir}"
export DEPTSLM_COMPOSE_WRAPPER=1

if ! command -v docker >/dev/null 2>&1; then
  printf 'Error: Docker is required. Install or start Docker Desktop and try again.\n' >&2
  exit 1
fi

cd "${repository_root}"
exec docker compose "$@"

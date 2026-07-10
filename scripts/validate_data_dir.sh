#!/usr/bin/env bash

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "${script_dir}/.." && pwd -P)

load_from_dotenv() {
  local env_file="${repository_root}/.env"
  local line
  local value
  local first_character
  local last_character

  if [[ -n "${DEPTSLM_DATA_DIR:-}" || ! -f "${env_file}" ]]; then
    return
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line=${line%$'\r'}
    if [[ "${line}" =~ ^[[:space:]]*DEPTSLM_DATA_DIR[[:space:]]*=(.*)$ ]]; then
      value=${BASH_REMATCH[1]}
      value=$(printf '%s' "${value}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

      if [[ ${#value} -ge 2 ]]; then
        first_character=${value:0:1}
        last_character=${value:$((${#value} - 1)):1}
        if [[ ( "${first_character}" == '"' && "${last_character}" == '"' ) ||
              ( "${first_character}" == "'" && "${last_character}" == "'" ) ]]; then
          value=${value:1:$((${#value} - 2))}
        fi
      fi

      export DEPTSLM_DATA_DIR="${value}"
    fi
  done < "${env_file}"
}

load_from_dotenv

if [[ -z "${DEPTSLM_DATA_DIR:-}" ]]; then
  printf 'Error: DEPTSLM_DATA_DIR is required. Run scripts/setup_google_drive_storage.sh and add its output to .env.\n' >&2
  exit 1
fi

case "${DEPTSLM_DATA_DIR}" in
  /*) ;;
  *)
    printf 'Error: DEPTSLM_DATA_DIR must be an absolute path outside the source repository.\n' >&2
    exit 1
    ;;
esac

if [[ ! -d "${DEPTSLM_DATA_DIR}" ]]; then
  printf 'Error: DEPTSLM_DATA_DIR does not exist or is not a directory:\n  %s\n' "${DEPTSLM_DATA_DIR}" >&2
  exit 1
fi

resolved_data_dir=$(CDPATH= cd -- "${DEPTSLM_DATA_DIR}" && pwd -P)

if [[ "${resolved_data_dir}" == "/" ]]; then
  printf 'Error: DEPTSLM_DATA_DIR must not be the filesystem root.\n' >&2
  exit 1
fi

case "${resolved_data_dir}" in
  "${repository_root}"|"${repository_root}"/*)
    printf 'Error: DEPTSLM_DATA_DIR must be outside the source repository:\n  %s\n' "${resolved_data_dir}" >&2
    exit 1
    ;;
esac

case "${repository_root}" in
  "${resolved_data_dir}"|"${resolved_data_dir}"/*)
    printf 'Error: DEPTSLM_DATA_DIR must not contain the source repository:\n  %s\n' "${resolved_data_dir}" >&2
    exit 1
    ;;
esac

if [[ ! -w "${resolved_data_dir}" || ! -x "${resolved_data_dir}" ]]; then
  printf 'Error: DEPTSLM_DATA_DIR is not writable and searchable:\n  %s\n' "${resolved_data_dir}" >&2
  exit 1
fi

if [[ "${1:-}" == "--require-compose-layout" ]]; then
  required_directories=(
    uploads
    extracted_text
    vector_snapshots
    training_datasets
    adapters
    model_cache
    eval_results
    logs
    exports
    service_state/postgres
    service_state/qdrant
  )

  for required_directory in "${required_directories[@]}"; do
    if [[ ! -d "${resolved_data_dir}/${required_directory}" ]]; then
      printf 'Error: required Compose storage directory is missing:\n  %s\n' "${resolved_data_dir}/${required_directory}" >&2
      printf 'Run scripts/setup_google_drive_storage.sh, then try again.\n' >&2
      exit 1
    fi

    if [[ ! -w "${resolved_data_dir}/${required_directory}" ||
          ! -x "${resolved_data_dir}/${required_directory}" ]]; then
      printf 'Error: required Compose storage directory is not writable and searchable:\n  %s\n' "${resolved_data_dir}/${required_directory}" >&2
      exit 1
    fi
  done
fi

printf '%s\n' "${resolved_data_dir}"

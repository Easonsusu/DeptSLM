#!/usr/bin/env bash

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "${script_dir}/.." && pwd -P)

if [[ $# -eq 0 ]]; then
  printf 'Usage: %s <docker compose arguments>\n' "$0" >&2
  exit 2
fi

validate_dotenv_permissions() {
  local env_file="${repository_root}/.env"
  local owner
  local current_user
  local mode
  local mode_number

  [[ -e "${env_file}" || -L "${env_file}" ]] || return 0
  if [[ -L "${env_file}" || ! -f "${env_file}" ]]; then
    printf 'Error: repository .env must be a regular non-symlink file.\n' >&2
    return 1
  fi

  case "$(uname -s)" in
    Darwin)
      owner=$(stat -f '%Su' "${env_file}" 2>/dev/null) || {
        printf 'Error: could not verify the owner of repository .env.\n' >&2
        return 1
      }
      ;;
    *)
      owner=$(stat -c '%U' "${env_file}" 2>/dev/null) || {
        printf 'Error: could not verify the owner of repository .env.\n' >&2
        return 1
      }
      ;;
  esac
  current_user=$(id -un)
  if [[ "${owner}" != "${current_user}" ]]; then
    printf 'Error: repository .env must be owned by the current user.\n' >&2
    return 1
  fi

  case "$(uname -s)" in
    Darwin)
      mode=$(stat -f '%Lp' "${env_file}" 2>/dev/null) || {
        printf 'Error: could not verify permissions for repository .env.\n' >&2
        return 1
      }
      ;;
    *)
      mode=$(stat -c '%a' "${env_file}" 2>/dev/null) || {
        printf 'Error: could not verify permissions for repository .env.\n' >&2
        return 1
      }
      ;;
  esac
  mode=${mode: -3}
  if [[ ! "${mode}" =~ ^[0-7]{3}$ ]]; then
    printf 'Error: could not verify permissions for repository .env.\n' >&2
    return 1
  fi
  mode_number=$((8#${mode}))
  if (( (mode_number & 077) != 0 || (mode_number & 0100) != 0 )); then
    printf 'Error: repository .env must be private (owner-only read/write). Run chmod 600 .env.\n' >&2
    return 1
  fi
}

validate_dotenv_permissions

if [[ "$1" == "config" ]]; then
  for argument in "$@"; do
    if [[ "${argument}" == "--environment" ]]; then
      printf 'Error: config --environment is refused because it can disclose interpolation values.\n' >&2
      exit 2
    fi
  done
fi

resolved_data_dir=$("${script_dir}/validate_data_dir.sh" --require-compose-layout)
export DEPTSLM_DATA_DIR="${resolved_data_dir}"
export DEPTSLM_COMPOSE_WRAPPER=1

if ! command -v docker >/dev/null 2>&1; then
  printf 'Error: Docker is required. Install or start Docker Desktop and try again.\n' >&2
  exit 1
fi

cd "${repository_root}"
if [[ "$1" == "config" ]]; then
  if [[ "${2:-}" == "--quiet" && $# -eq 2 ]]; then
    exec docker compose config --quiet
  fi
  docker compose config --quiet
  exec docker compose config --no-interpolate
fi
exec docker compose "$@"

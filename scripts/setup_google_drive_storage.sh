#!/usr/bin/env bash

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'Error: this setup script supports macOS only.\n' >&2
  exit 1
fi

cloud_storage_dir="${HOME}/Library/CloudStorage"

if [[ ! -d "${cloud_storage_dir}" ]]; then
  printf 'Error: macOS CloudStorage directory was not found at:\n  %s\n' "${cloud_storage_dir}" >&2
  printf 'Install Google Drive for desktop, sign in, and run this script again.\n' >&2
  exit 1
fi

shopt -s nullglob
google_drive_roots=("${cloud_storage_dir}"/GoogleDrive-*)
shopt -u nullglob

if [[ ${#google_drive_roots[@]} -eq 0 ]]; then
  printf 'Error: no mounted Google Drive account was found under:\n  %s/GoogleDrive-*\n' "${cloud_storage_dir}" >&2
  printf 'Start Google Drive for desktop, then run this script again.\n' >&2
  exit 1
fi

# Google Drive localizes the personal-drive directory. These are the common
# English and Traditional Chinese names used on macOS.
personal_drive_names=("My Drive" "我的雲端硬碟")
personal_drive_candidates=()

for google_drive_root in "${google_drive_roots[@]}"; do
  for personal_drive_name in "${personal_drive_names[@]}"; do
    candidate="${google_drive_root}/${personal_drive_name}"
    if [[ -d "${candidate}" ]]; then
      personal_drive_candidates+=("${candidate}")
    fi
  done
done

if [[ ${#personal_drive_candidates[@]} -eq 0 ]]; then
  printf 'Error: found %d Google Drive account(s), but no known personal-drive directory.\n' "${#google_drive_roots[@]}" >&2
  printf 'Expected an existing directory named "My Drive" or "我的雲端硬碟".\n' >&2
  printf 'No directory was created because the correct destination is ambiguous.\n' >&2
  exit 1
fi

selected_personal_drive=""
best_score=-1
best_candidates=()

for candidate in "${personal_drive_candidates[@]}"; do
  score=0

  # An existing DeptSLM directory is the strongest signal on repeat runs.
  if [[ -d "${candidate}/DeptSLM" ]]; then
    score=$((score + 1000))
  fi

  if [[ -w "${candidate}" ]]; then
    score=$((score + 100))
  fi

  # Prefer a personal Gmail account over a managed account when otherwise tied.
  case "${candidate}" in
    *"@gmail.com"*) score=$((score + 10)) ;;
  esac

  if [[ ${score} -gt ${best_score} ]]; then
    best_score=${score}
    best_candidates=("${candidate}")
  elif [[ ${score} -eq ${best_score} ]]; then
    best_candidates+=("${candidate}")
  fi
done

if [[ ${#best_candidates[@]} -gt 1 ]]; then
  printf 'Error: multiple Google Drive personal folders are equally suitable for DeptSLM:\n' >&2
  printf '  %s\n' "${best_candidates[@]}" >&2
  printf 'No directory was created because the destination is ambiguous.\n' >&2
  printf 'Temporarily unmount the other account(s), then run this script again.\n' >&2
  exit 1
fi

selected_personal_drive="${best_candidates[0]}"
data_dir="${selected_personal_drive}/DeptSLM"
runtime_subdirectories=(
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

printf 'Detected %d mounted Google Drive account(s).\n' "${#google_drive_roots[@]}"
printf 'Selected personal drive:\n  %s\n' "${selected_personal_drive}"
printf 'Creating DeptSLM runtime directories (existing files are preserved)...\n'

if [[ -e "${data_dir}" && ! -d "${data_dir}" ]]; then
  printf 'Error: the selected DeptSLM path exists but is not a directory:\n  %s\n' "${data_dir}" >&2
  exit 1
fi

if [[ -d "${data_dir}" ]]; then
  writable_parent="${data_dir}"
else
  writable_parent="${selected_personal_drive}"
fi

if [[ ! -w "${writable_parent}" ]]; then
  printf 'Error: the selected Google Drive location is not writable:\n  %s\n' "${writable_parent}" >&2
  exit 1
fi

mkdir -p "${data_dir}"
for runtime_subdirectory in "${runtime_subdirectories[@]}"; do
  mkdir -p "${data_dir}/${runtime_subdirectory}"
done

# Escape the small set of characters that are significant inside a quoted
# dotenv or shell value.
dotenv_data_dir=${data_dir//\\/\\\\}
dotenv_data_dir=${dotenv_data_dir//\"/\\\"}
dotenv_data_dir=${dotenv_data_dir//\$/\\$}
dotenv_data_dir=${dotenv_data_dir//\`/\\\`}

printf '\nStorage is ready. Add this line to .env:\n'
printf 'DEPTSLM_DATA_DIR="%s"\n' "${dotenv_data_dir}"
printf '\nFor the current shell, run:\n'
printf 'export DEPTSLM_DATA_DIR="%s"\n' "${dotenv_data_dir}"

#!/usr/bin/env bash

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "${script_dir}/.." && pwd -P)
env_file="${repository_root}/.env"

if [[ -e "${env_file}" || -L "${env_file}" ]]; then
  printf 'Refusing to overwrite an existing .env.\n' >&2
  exit 1
fi
if [[ -z "${DEPTSLM_DATA_DIR:-}" || "${DEPTSLM_DATA_DIR}" != /* ]]; then
  printf 'Set DEPTSLM_DATA_DIR to the external runtime directory first.\n' >&2
  printf 'Example: DEPTSLM_DATA_DIR="/absolute/path/to/DeptSLM" %s\n' "$0" >&2
  exit 2
fi

random_value() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(36))'
}

base64url() {
  python3 -c 'import base64,sys; print(base64.urlsafe_b64encode(sys.stdin.buffer.read()).rstrip(b"=").decode())'
}

auth_secret=$(random_value)
postgres_password=$(random_value)
qdrant_key=$(random_value)
rag_token=$(random_value)
adapter_token=$(random_value)
adapter_eval_token=$(random_value)
training_token=$(random_value)
issued_at=$(date +%s)
header=$(printf '%s' '{"alg":"HS256","typ":"JWT"}' | base64url)
payload=$(printf '{"iss":"https://local-issuer.invalid","aud":"deptslm-local","sub":"local-web-dev","iat":%s,"exp":%s}' "${issued_at}" "$((issued_at + 86400))" | base64url)
signature=$(printf '%s' "${header}.${payload}" | AUTH_SECRET="${auth_secret}" python3 -c 'import base64,hashlib,hmac,os,sys; print(base64.urlsafe_b64encode(hmac.new(os.environ["AUTH_SECRET"].encode(), sys.stdin.buffer.read(), hashlib.sha256).digest()).rstrip(b"=").decode())')
web_token="${header}.${payload}.${signature}"

umask 077
cp "${repository_root}/.env.example" "${env_file}"
chmod 600 "${env_file}"

tmp_file="${env_file}.tmp.$$"
trap 'rm -f -- "${tmp_file}"' EXIT
awk \
  -v data_dir="${DEPTSLM_DATA_DIR}" \
  -v postgres_password="${postgres_password}" \
  -v auth_secret="${auth_secret}" \
  -v qdrant_key="${qdrant_key}" \
  -v rag_token="${rag_token}" \
  -v adapter_token="${adapter_token}" \
  -v adapter_eval_token="${adapter_eval_token}" \
  -v training_token="${training_token}" \
  -v web_token="${web_token}" \
  'BEGIN { values["DEPTSLM_DATA_DIR"] = data_dir; values["DEPTSLM_POSTGRES_PASSWORD"] = postgres_password; values["DEPTSLM_AUTH_MODE"] = "hs256"; values["DEPTSLM_AUTH_ISSUER"] = "https://local-issuer.invalid"; values["DEPTSLM_AUTH_AUDIENCE"] = "deptslm-local"; values["DEPTSLM_AUTH_SECRET"] = auth_secret; values["DEPTSLM_WEB_DEV_BEARER_TOKEN"] = web_token; values["DEPTSLM_QDRANT_API_KEY"] = qdrant_key; values["DEPTSLM_RAG_RUNTIME_TOKEN"] = rag_token; values["DEPTSLM_ADAPTER_RUNTIME_TOKEN"] = adapter_token; values["DEPTSLM_ADAPTER_EVAL_RUNTIME_TOKEN"] = adapter_eval_token; values["DEPTSLM_TRAINING_RUNTIME_TOKEN"] = training_token } /^[A-Za-z_][A-Za-z0-9_]*=/ { key=$0; sub(/=.*/, "", key); if (key in values) { print key "=\"" values[key] "\""; delete values[key]; next } } { print } END { for (key in values) print key "=\"" values[key] "\"" }' \
  "${env_file}" >"${tmp_file}"
mv -f -- "${tmp_file}" "${env_file}"
trap - EXIT
chmod 600 "${env_file}"

printf 'Created a mode-600 untracked .env with fresh local secrets.\n'
printf 'The development bearer is server-only and expires in 24 hours.\n'
printf 'Bootstrap the matching local identity with subject: local-web-dev\n'

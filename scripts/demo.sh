#!/usr/bin/env bash

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "${script_dir}/.." && pwd -P)
cd "${repository_root}"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  printf 'Docker Compose is required for the synthetic demo.\n' >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  printf 'curl and python3 are required for the synthetic demo.\n' >&2
  exit 1
fi

tmp_parent=${TMPDIR:-/tmp}
runtime_root=$(mktemp -d "${tmp_parent%/}/deptslm-demo.XXXXXX")
chmod 700 "${runtime_root}"
env_file=$(mktemp "${tmp_parent%/}/deptslm-demo-env.XXXXXX")
chmod 600 "${env_file}"
source_file=$(mktemp "${runtime_root}/source.XXXXXX")
project="deptslm-demo-${PPID}-${RANDOM}"
api_port=${DEPTSLM_DEMO_API_PORT:-18000}
web_port=${DEPTSLM_DEMO_WEB_PORT:-13000}
status_before=$(mktemp "${tmp_parent%/}/deptslm-demo-status.XXXXXX")
status_after=$(mktemp "${tmp_parent%/}/deptslm-demo-status.XXXXXX")

mkdir -p "${runtime_root}"/{uploads,extracted_text,vector_snapshots,training_datasets,model_cache,eval_results,logs,exports}
mkdir -p "${runtime_root}/training_datasets/jobs" "${runtime_root}/adapters"/{imports,registry,.staging/imports,.staging/registry}
mkdir -p "${runtime_root}/adapters/.deleting"/{source_stage,source_final,registry_stage,registry_final}
mkdir -p "${runtime_root}/adapters/.purge-deleting"/{source_stage,source_final,registry_stage,registry_final}
mkdir -p "${runtime_root}/service_state"/{postgres,qdrant}
find "${runtime_root}" -type d -exec chmod 700 {} +

random_value() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(36))'
}

auth_secret=$(random_value)
qdrant_key=$(random_value)
rag_token=$(random_value)
adapter_token=$(random_value)
adapter_eval_token=$(random_value)
issuer="https://phase13-demo.invalid"
audience="deptslm-phase13-demo"
question_sentinel="PHASE13_DEMO_QUESTION_SENTINEL"

cat >"${env_file}" <<EOF
DEPTSLM_DATA_DIR=${runtime_root}
DEPTSLM_DEMO_UID=$(id -u)
DEPTSLM_DEMO_GID=$(id -g)
DATABASE_URL=postgresql+psycopg://deptslm:deptslm@postgres:5432/deptslm
ENVIRONMENT=test
API_PORT=${api_port}
WEB_PORT=${web_port}
DEPTSLM_AUTH_MODE=hs256
DEPTSLM_AUTH_ISSUER=${issuer}
DEPTSLM_AUTH_AUDIENCE=${audience}
DEPTSLM_AUTH_SECRET=${auth_secret}
DEPTSLM_QDRANT_URL=http://qdrant:6333
DEPTSLM_QDRANT_API_KEY=${qdrant_key}
DEPTSLM_QDRANT_COLLECTION=deptslm_chunks_qwen3_0_6b_1024_v1
DEPTSLM_RAG_RUNTIME_URL=http://rag-runtime:8010
DEPTSLM_RAG_RUNTIME_TOKEN=${rag_token}
DEPTSLM_ADAPTER_RUNTIME_URL=http://adapter-runtime:8012
DEPTSLM_ADAPTER_RUNTIME_TOKEN=${adapter_token}
DEPTSLM_ADAPTER_EVAL_RUNTIME_TOKEN=${adapter_eval_token}
DEPTSLM_RAG_RUNTIME_PROVIDER=fake
DEPTSLM_EMBEDDING_PROVIDER=fake
DEPTSLM_RAG_MIN_SCORE=0.01
DEPTSLM_EMBEDDING_MODEL_REVISION=d23109d65ca9fdf61eef614209744716f337f50f
DEPTSLM_GENERATION_MODEL_REVISION=c1899de289a04d12100db370d81485cdf75e47ca
DEPTSLM_EVALUATION_CODE_REVISION=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
DEPTSLM_SFT_CODE_REVISION=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
DEPTSLM_TRAINING_JOB_CODE_REVISION=cccccccccccccccccccccccccccccccccccccccc
DEPTSLM_ADAPTER_REGISTRY_CODE_REVISION=dddddddddddddddddddddddddddddddddddddddd
EOF

git status --porcelain --untracked-files=all >"${status_before}"

export DEPTSLM_DATA_DIR="${runtime_root}"
export COMPOSE_FILE="${repository_root}/docker-compose.yml:${repository_root}/docker-compose.demo.yml"
export COMPOSE_PROJECT_NAME="${project}"
export COMPOSE_ENV_FILES="${env_file}"
set -a
# The file is generated above with shell-safe values and remains mode 0600.
. "${env_file}"
set +a

compose() {
  "${script_dir}/compose.sh" "$@"
}

cleanup() {
  set +e
  compose down --volumes --remove-orphans >/dev/null 2>&1
  rm -f -- "${env_file}" "${source_file}" "${status_before}" "${status_after}"
  case "${runtime_root}" in
    "${tmp_parent%/}/deptslm-demo."*) rm -rf -- "${runtime_root}" ;;
    *) printf 'Refusing to remove an unexpected demo path: %s\n' "${runtime_root}" >&2 ;;
  esac
}
trap cleanup EXIT INT TERM

compose config --quiet
compose --profile adapter-evaluation up --detach --build \
  postgres qdrant rag-runtime adapter-runtime adapter-eval-runtime >/dev/null

compose --profile admin build vector-admin >/dev/null
qdrant_ready=0
for _ in $(seq 1 60); do
  if compose --profile admin run --rm --no-deps vector-admin bootstrap >/dev/null 2>&1; then
    qdrant_ready=1
    break
  fi
  sleep 1
done
if [[ "${qdrant_ready}" -ne 1 ]]; then
  printf 'Synthetic Qdrant did not become ready.\n' >&2
  exit 1
fi
compose --profile adapter-evaluation up --detach --build \
  api rag-worker indexing-worker evaluator-worker web adapter-evaluator >/dev/null

probe_python_dns() {
  local service="$1"
  local target="$2"
  local expected="$3"
  local attempts=20
  local result=0
  while (( attempts > 0 )); do
    result=0
    compose exec --no-TTY "$service" python -c \
      'import socket,sys; socket.getaddrinfo(sys.argv[1], None)' "${target}" >/dev/null 2>&1 || result=$?
    if [[ "${expected}" == "yes" && ${result} -eq 0 ]] || [[ "${expected}" == "no" && ${result} -ne 0 ]]; then
      return 0
    fi
    attempts=$((attempts - 1))
    (( attempts > 0 )) && sleep 0.5
  done
  compose ps >&2 || true
  compose logs --no-color "${service}" >&2 || true
  compose exec --no-TTY "${service}" python -c \
    'import socket,sys; print(socket.getaddrinfo(sys.argv[1], None))' "${target}" >&2 || true
  printf 'Unexpected DNS reachability: %s -> %s (%s).\n' "${service}" "${target}" "${expected}" >&2
  exit 1
}

probe_node_dns() {
  local target="$1"
  local expected="$2"
  local result=0
  compose exec --no-TTY web node -e \
    'const dns=require("dns").promises; const target=process.argv[1]; let attempts=20; const probe=async()=>{try{const addresses=await dns.resolve4(target); if(addresses.length) process.exit(0);}catch(_){} if(--attempts===0) process.exit(1); setTimeout(probe,500);}; probe();' \
    "${target}" >/dev/null 2>&1 || result=$?
  if [[ "${expected}" == "yes" && ${result} -ne 0 ]] || [[ "${expected}" == "no" && ${result} -eq 0 ]]; then
    printf 'Unexpected web DNS reachability: web -> %s (%s).\n' "${target}" "${expected}" >&2
    exit 1
  fi
}

probe_node_dns api yes
probe_node_dns postgres no
probe_node_dns qdrant no
probe_node_dns rag-runtime no

probe_python_dns api postgres yes
probe_python_dns api qdrant yes
probe_python_dns api rag-runtime yes
probe_python_dns api adapter-runtime yes
probe_python_dns api adapter-eval-runtime no
probe_python_dns rag-worker qdrant no
probe_python_dns evaluator-worker postgres yes
probe_python_dns evaluator-worker qdrant yes
probe_python_dns evaluator-worker rag-runtime yes
probe_python_dns adapter-evaluator postgres yes
probe_python_dns adapter-evaluator qdrant yes
probe_python_dns adapter-evaluator rag-runtime yes
probe_python_dns adapter-evaluator adapter-eval-runtime yes
probe_python_dns rag-runtime postgres no
probe_python_dns rag-runtime qdrant no
probe_python_dns rag-runtime adapter-runtime no
probe_python_dns rag-runtime adapter-eval-runtime no
probe_python_dns adapter-runtime rag-runtime no
probe_python_dns adapter-runtime adapter-eval-runtime no
probe_python_dns adapter-eval-runtime postgres no
probe_python_dns adapter-eval-runtime qdrant no
probe_python_dns adapter-eval-runtime rag-runtime no
probe_python_dns adapter-eval-runtime adapter-runtime no
probe_python_dns indexing-worker postgres yes
probe_python_dns indexing-worker qdrant yes
if ! compose exec --no-TTY indexing-worker python -c \
  'import os,urllib.request; request=urllib.request.Request("http://qdrant:6333/collections",headers={"api-key":os.environ["DEPTSLM_QDRANT_API_KEY"]}); response=urllib.request.urlopen(request,timeout=5); print("Phase 13 Qdrant HTTP probe status:",response.status)' 2>/dev/null; then
  printf 'Phase 13 Qdrant HTTP probe failed.\n' >&2
fi
compose --profile admin run --rm --no-deps --build --entrypoint python model-admin \
  -c $'import socket\nfor target in ("postgres", "qdrant", "api", "rag-runtime", "adapter-runtime"):\n    try: socket.getaddrinfo(target, None)\n    except OSError: continue\n    raise SystemExit(1)' >/dev/null 2>&1
compose run --rm --no-deps api python -m alembic upgrade head >/dev/null

bootstrap_output=$(compose run --rm --no-deps api python -m app.admin bootstrap-department \
  --slug phase13-a --display-name "Phase 13 Synthetic A" \
  --admin-issuer "${issuer}" --admin-subject phase13-admin-a)
department_a=$(printf '%s\n' "${bootstrap_output}" | sed -nE 's/.*\(([0-9a-f-]{36})\).*/\1/p')
bootstrap_output=$(compose run --rm --no-deps api python -m app.admin bootstrap-department \
  --slug phase13-b --display-name "Phase 13 Synthetic B" \
  --admin-issuer "${issuer}" --admin-subject phase13-admin-b)
department_b=$(printf '%s\n' "${bootstrap_output}" | sed -nE 's/.*\(([0-9a-f-]{36})\).*/\1/p')
if [[ -z "${department_a}" || -z "${department_b}" ]]; then
  printf 'Synthetic department bootstrap did not return UUIDs.\n' >&2
  exit 1
fi

token_a=$(compose run --rm --no-deps api python -c \
  'import os,time,jwt; print(jwt.encode({"sub":"phase13-admin-a","iss":os.environ["DEPTSLM_AUTH_ISSUER"],"aud":os.environ["DEPTSLM_AUTH_AUDIENCE"],"exp":int(time.time())+900}, os.environ["DEPTSLM_AUTH_SECRET"], algorithm="HS256"))')

api_call() {
  local expected="$1"
  local method="$2"
  local path="$3"
  local output="$4"
  local input_file="${5:-}"
  local content_type="${6:-}"
  local content_disposition="${7:-}"
  local report_failure="${8:-1}"
  local response_file="${output}.http"
  local status
  if [[ -n "${input_file}" ]]; then
    if ! compose exec --no-TTY -e "DEMO_TOKEN=${token_a}" indexing-worker python -c \
      'import httpx,os,sys; method,path,content_type,content_disposition=sys.argv[1:5]; data=sys.stdin.buffer.read(); headers={"Authorization":"Bearer "+os.environ["DEMO_TOKEN"]}; content_type and headers.__setitem__("Content-Type",content_type); content_disposition and headers.__setitem__("Content-Disposition",content_disposition); response=httpx.request(method,"http://api:8000"+path,headers=headers,content=data,timeout=30.0); print(response.status_code); sys.stdout.write(response.text)' \
      "${method}" "${path}" "${content_type}" "${content_disposition}" <"${input_file}" >"${response_file}"; then
      return 1
    fi
  else
    if ! compose exec --no-TTY -e "DEMO_TOKEN=${token_a}" indexing-worker python -c \
      'import httpx,os,sys; method,path=sys.argv[1:3]; response=httpx.request(method,"http://api:8000"+path,headers={"Authorization":"Bearer "+os.environ["DEMO_TOKEN"]},timeout=30.0); print(response.status_code); sys.stdout.write(response.text)' \
      "${method}" "${path}" >"${response_file}"; then
      return 1
    fi
  fi
  status=$(sed -n '1p' "${response_file}" | tr -d '\r')
  tail -n +2 "${response_file}" >"${output}"
  rm -f -- "${response_file}"
  if [[ "${expected}" == "2xx" ]]; then
    if [[ "${status}" =~ ^2[0-9][0-9]$ ]]; then
      return 0
    fi
  else
    if [[ "${status}" == "${expected}" ]]; then
      return 0
    fi
  fi
  if [[ "${report_failure}" == "1" ]]; then
    printf 'Synthetic API check failed: %s %s (HTTP %s).\n' "${method}" "${path}" "${status:-unknown}" >&2
  fi
  return 1
}

api_ready=0
for _ in $(seq 1 90); do
  if api_call 2xx GET /health "${runtime_root}/health.json" "" "" "" 0; then
    api_ready=1
    break
  fi
  sleep 1
done
if [[ "${api_ready}" -ne 1 ]]; then
  compose ps --all >&2 || true
  compose logs --no-color api >&2 || true
  printf 'Synthetic API did not become ready.\n' >&2
  exit 1
fi
api_call 2xx GET /version "${runtime_root}/version.json"
api_call 2xx GET /auth/me "${runtime_root}/auth-me.json"

printf 'Phase 13 synthetic source: department-scoped runtime boundary.\n' >"${source_file}"
upload_response="${runtime_root}/upload-response.json"
api_call 2xx POST "/departments/${department_a}/documents" "${upload_response}" \
  "${source_file}" 'text/plain; charset=utf-8' 'attachment; filename="phase13.txt"'
document_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "${upload_response}")

extraction_response="${runtime_root}/extraction-response.json"
api_call 2xx POST "/departments/${department_a}/documents/${document_id}/extractions" "${extraction_response}"
extraction_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "${extraction_response}")
extraction_status=""
for _ in $(seq 1 90); do
  api_call 2xx GET "/departments/${department_a}/documents/${document_id}/extractions" "${runtime_root}/extractions.json"
  extraction_status=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["status"] for x in d["items"] if x["id"]==sys.argv[2]))' "${runtime_root}/extractions.json" "${extraction_id}")
  [[ "${extraction_status}" == "succeeded" ]] && break
  [[ "${extraction_status}" == "failed" ]] && break
  sleep 1
done
[[ "${extraction_status}" == "succeeded" ]] || { printf 'Synthetic extraction did not succeed.\n' >&2; exit 1; }

qdrant_ready=0
for _ in $(seq 1 60); do
  if compose exec --no-TTY indexing-worker python -m deptslm_worker.vector_admin bootstrap >/dev/null 2>&1; then
    qdrant_ready=1
    break
  fi
  sleep 1
done
if [[ "${qdrant_ready}" -ne 1 ]]; then
  compose exec --no-TTY indexing-worker python -m deptslm_worker.vector_admin bootstrap >&2 || true
  printf 'Synthetic Qdrant collection was not ready for indexing.\n' >&2
  exit 1
fi

compose exec --detach --no-TTY indexing-worker \
  python -m deptslm_worker.indexer --poll >/dev/null

indexing_response="${runtime_root}/indexing-response.json"
api_call 2xx POST "/departments/${department_a}/documents/${document_id}/extractions/${extraction_id}/indexings" "${indexing_response}"
indexing_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "${indexing_response}")
indexing_status=""
for _ in $(seq 1 120); do
  api_call 2xx GET "/departments/${department_a}/documents/${document_id}/extractions/${extraction_id}/indexings" "${runtime_root}/indexings.json"
  indexing_status=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(x["status"] for x in d["items"] if x["id"]==sys.argv[2]))' "${runtime_root}/indexings.json" "${indexing_id}")
  [[ "${indexing_status}" == "succeeded" ]] && break
  [[ "${indexing_status}" == "failed" ]] && break
  sleep 1
done
if [[ "${indexing_status}" != "succeeded" ]]; then
  compose ps --all >&2 || true
  compose logs --no-color indexing-worker >&2 || true
  printf 'Synthetic indexing did not succeed.\n' >&2
  exit 1
fi

answer_response="${runtime_root}/answer-response.json"
answer_request="${runtime_root}/answer-request.json"
printf '{"question":"%s"}\n' "${question_sentinel}" >"${answer_request}"
api_call 2xx POST "/departments/${department_a}/rag/answers" "${answer_response}" \
  "${answer_request}" 'application/json'
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"]=="answered"; assert d["citations"]' "${answer_response}"

cross_response="${runtime_root}/cross-department.json"
api_call 403 GET "/departments/${department_b}/documents" "${cross_response}"
compose run --rm --no-deps web node -e \
  'fetch("http://api:8000/health").then(response => { if (!response.ok) process.exit(1); }).catch(() => process.exit(1))' >/dev/null

compose logs --no-color >"${runtime_root}/compose.log" 2>&1 || true
if grep -F -e "${auth_secret}" -e "${qdrant_key}" -e "${rag_token}" -e "${adapter_token}" -e "${adapter_eval_token}" -e "${question_sentinel}" "${runtime_root}/compose.log" >/dev/null; then
  printf 'Synthetic demo logs contained a protected sentinel.\n' >&2
  exit 1
fi

git status --porcelain --untracked-files=all >"${status_after}"
cmp -s "${status_before}" "${status_after}" || {
  printf 'Synthetic demo changed the checkout.\n' >&2
  exit 1
}
printf 'Phase 13 synthetic Docker demo passed: upload, extraction, indexing, RAG citation, cross-department denial, and cleanup.\n'

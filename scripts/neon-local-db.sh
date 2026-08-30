#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${NEON_PROJECT_ID:-quiet-star-70907686}"
PRODUCTION_BRANCH_ID="${NEON_PRODUCTION_BRANCH_ID:-br-red-lake-af5pqg75}"
LOCAL_BRANCH_NAME="${NEON_LOCAL_BRANCH_NAME:-local-pr-testing}"
DATABASE_NAME="${NEON_DATABASE_NAME:-quilombo}"
ROLE_NAME="${NEON_DATABASE_ROLE:-quilombo_owner}"
API_BASE_URL="${NEON_API_BASE_URL:-https://console.neon.tech/api/v2}"

if [[ -z "${NEON_API_KEY:-}" && -f .env ]]; then
  NEON_API_KEY="$(awk -F= '$1 == "NEON_API_KEY" {sub(/^[^=]*=/, ""); print; exit}' .env)"
fi
: "${NEON_API_KEY:?Set NEON_API_KEY or add it to .env}"

api() {
  curl -fsS --retry 2 --retry-delay 1 \
    -H "Authorization: Bearer ${NEON_API_KEY}" \
    -H "Content-Type: application/json" \
    "$@"
}

branch_id() {
  api "${API_BASE_URL}/projects/${PROJECT_ID}/branches" \
    | jq -r --arg name "${LOCAL_BRANCH_NAME}" '.branches[] | select(.name == $name) | .id' \
    | head -n 1
}

endpoint_id() {
  local branch="$1"
  api "${API_BASE_URL}/projects/${PROJECT_ID}/branches/${branch}/endpoints" \
    | jq -r '.endpoints[] | select(.type == "read_write") | .id' \
    | head -n 1
}

create_branch() {
  local payload
  payload="$(jq -nc \
    --arg name "${LOCAL_BRANCH_NAME}" \
    --arg parent_id "${PRODUCTION_BRANCH_ID}" \
    '{branch: {name: $name, parent_id: $parent_id}, endpoints: [{type: "read_write"}]}')"
  api -X POST -d "${payload}" "${API_BASE_URL}/projects/${PROJECT_ID}/branches" \
    | jq -r '.branch.id'
}

create_endpoint() {
  local branch="$1"
  local payload
  payload="$(jq -nc --arg branch_id "${branch}" \
    '{endpoint: {branch_id: $branch_id, type: "read_write"}}')"
  api -X POST -d "${payload}" "${API_BASE_URL}/projects/${PROJECT_ID}/endpoints" \
    | jq -r '.endpoint.id'
}

wait_for_endpoint() {
  local branch="$1"
  local endpoint="$2"
  local state
  for _ in $(seq 1 60); do
    state="$(api "${API_BASE_URL}/projects/${PROJECT_ID}/branches/${branch}/endpoints" \
      | jq -r --arg endpoint "${endpoint}" '.endpoints[] | select(.id == $endpoint) | .current_state')"
    if [[ "${state}" == "active" || "${state}" == "idle" || "${state}" == "ready" ]]; then
      return
    fi
    sleep 2
  done
  echo "Timed out waiting for Neon endpoint ${endpoint}." >&2
  exit 1
}

ensure_branch() {
  local branch
  branch="$(branch_id)"
  if [[ -z "${branch}" ]]; then
    echo "Creating Neon branch ${LOCAL_BRANCH_NAME} from production snapshot..." >&2
    branch="$(create_branch)"
  fi

  local endpoint
  endpoint="$(endpoint_id "${branch}")"
  if [[ -z "${endpoint}" ]]; then
    echo "Creating read-write endpoint for ${LOCAL_BRANCH_NAME}..." >&2
    endpoint="$(create_endpoint "${branch}")"
  fi
  wait_for_endpoint "${branch}" "${endpoint}"
  printf '%s\t%s\n' "${branch}" "${endpoint}"
}

database_url() {
  local branch endpoint
  IFS=$'\t' read -r branch endpoint <<<"$(ensure_branch)"
  api "${API_BASE_URL}/projects/${PROJECT_ID}/connection_uri?branch_id=${branch}&endpoint_id=${endpoint}&role_name=${ROLE_NAME}&database_name=${DATABASE_NAME}&pooled=false" \
    | jq -er '.uri'
}

refresh() {
  local branch
  branch="$(branch_id)"
  if [[ -n "${branch}" ]]; then
    if [[ "${branch}" == "${PRODUCTION_BRANCH_ID}" || "${LOCAL_BRANCH_NAME}" == "main" ]]; then
      echo "Refusing to delete the production branch." >&2
      exit 1
    fi
    echo "Deleting local Neon branch ${LOCAL_BRANCH_NAME}; local test writes will be lost." >&2
    api -X DELETE "${API_BASE_URL}/projects/${PROJECT_ID}/branches/${branch}" >/dev/null
    for _ in $(seq 1 60); do
      [[ -z "$(branch_id)" ]] && break
      sleep 2
    done
    if [[ -n "$(branch_id)" ]]; then
      echo "Timed out waiting for local Neon branch deletion." >&2
      exit 1
    fi
  fi
  ensure_branch >/dev/null
  echo "Local Neon branch refreshed from the production snapshot." >&2
}

status() {
  local branch
  branch="$(branch_id)"
  if [[ -z "${branch}" ]]; then
    echo "${LOCAL_BRANCH_NAME}: missing"
    return
  fi
  api "${API_BASE_URL}/projects/${PROJECT_ID}/branches/${branch}" \
    | jq -r '"name=" + .branch.name + " id=" + .branch.id + " state=" + .branch.current_state + " parent=" + (.branch.parent_id // "")'
  api "${API_BASE_URL}/projects/${PROJECT_ID}/branches/${branch}/endpoints" \
    | jq -r '.endpoints[] | "endpoint=" + .id + " state=" + .current_state + " type=" + .type'
}

case "${1:-url}" in
  url)
    database_url
    ;;
  refresh)
    refresh
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 {url|refresh|status}" >&2
    exit 2
    ;;
esac

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_SCRIPT="${ROOT_DIR}/scripts/build-and-push.sh"

NAS_SSH_HOST="${NAS_SSH_HOST:-nas}"
NAS_REMOTE_BRIDGE="${NAS_REMOTE_BRIDGE:-${HOME}/nas-remote}"
NAS_REMOTE_STATE_DIR="${NAS_REMOTE_STATE_DIR:-${HOME}/.nas-remote}"
NAS_COMPOSE_DIR="${NAS_COMPOSE_DIR:-/share/ZFS530_DATA/.qpkg/container-station/data/application/subtitle}"
NAS_DOCKER_CONFIG="${NAS_DOCKER_CONFIG:-/share/homes/hsk/.docker}"
NAS_DISCOVERY_TIMEOUT="${NAS_DISCOVERY_TIMEOUT:-30}"
NAS_WAIT_TIMEOUT="${NAS_WAIT_TIMEOUT:-900}"

SKIP_BUILD_PUSH=false
SKIP_NAS_DEPLOY=false
DRY_RUN=false
SERVICES=()

usage() {
  cat <<'EOF'
Usage:
  scripts/release-to-nas.sh [options]

Options:
  --services svc1,svc2   Limit NAS pull/up/ps to selected services.
  --service svc          Append one service (repeatable).
  --nas-only             Skip local build+push; deploy existing images on NAS.
  --build-only           Run local build+push only; skip NAS deploy.
  --compose-dir path     Override remote compose directory.
  --docker-config path   Override remote DOCKER_CONFIG path.
  --dry-run              Print the commands and exit.
  -h, --help             Show this help.

Environment:
  NAS_SSH_HOST           SSH host alias. Default: nas
  NAS_REMOTE_BRIDGE      Preferred Terminal bridge. Default: ~/nas-remote
  NAS_REMOTE_STATE_DIR   Bridge state dir. Default: ~/.nas-remote
  NAS_COMPOSE_DIR        Remote compose dir for subtitle stack.
  NAS_DOCKER_CONFIG      Remote Docker auth config path.
  NAS_DISCOVERY_TIMEOUT  Seconds to wait for a new bridge log to appear.
  NAS_WAIT_TIMEOUT       Seconds to wait for NAS command completion.

Examples:
  scripts/release-to-nas.sh
  scripts/release-to-nas.sh --services subtitle-processor,telegram-bot
  scripts/release-to-nas.sh --nas-only --service subtitle-processor
  scripts/release-to-nas.sh --dry-run
EOF
}

log() {
  echo "INFO: $*"
}

error() {
  echo "ERROR: $*" >&2
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

has_services() {
  [[ -n "${SERVICES[*]-}" ]]
}

shell_join() {
  local out=""
  local arg

  for arg in "$@"; do
    out+="${out:+ }$(printf '%q' "$arg")"
  done

  printf '%s' "$out"
}

append_csv_services() {
  local raw_csv="$1"
  local raw_items=()
  local item=""
  IFS=',' read -r -a raw_items <<< "$raw_csv"
  for item in "${raw_items[@]}"; do
    item="$(trim "$item")"
    if [[ -n "$item" ]]; then
      SERVICES+=("$item")
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --services)
      if [[ $# -lt 2 ]]; then
        error "--services requires a value"
        exit 1
      fi
      append_csv_services "$2"
      shift 2
      ;;
    --service)
      if [[ $# -lt 2 ]]; then
        error "--service requires a value"
        exit 1
      fi
      SERVICES+=("$(trim "$2")")
      shift 2
      ;;
    --nas-only|--deploy-only)
      SKIP_BUILD_PUSH=true
      shift
      ;;
    --build-only)
      SKIP_NAS_DEPLOY=true
      shift
      ;;
    --compose-dir)
      if [[ $# -lt 2 ]]; then
        error "--compose-dir requires a value"
        exit 1
      fi
      NAS_COMPOSE_DIR="$2"
      shift 2
      ;;
    --docker-config)
      if [[ $# -lt 2 ]]; then
        error "--docker-config requires a value"
        exit 1
      fi
      NAS_DOCKER_CONFIG="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      error "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if has_services; then
  for service in "${SERVICES[@]}"; do
    if [[ -z "$service" ]]; then
      error "Service names must not be empty"
      exit 1
    fi
  done
fi

build_remote_compose_command() {
  local compose_dir_quoted docker_config_quoted service_suffix=""
  compose_dir_quoted="$(printf '%q' "${NAS_COMPOSE_DIR}")"
  docker_config_quoted="$(printf '%q' "${NAS_DOCKER_CONFIG}")"

  if has_services; then
    service_suffix=" $(shell_join "${SERVICES[@]}")"
  fi

  printf 'cd %s && DOCKER_CONFIG=%s docker compose pull%s && DOCKER_CONFIG=%s docker compose up -d --force-recreate%s && DOCKER_CONFIG=%s docker compose ps%s' \
    "${compose_dir_quoted}" \
    "${docker_config_quoted}" "${service_suffix}" \
    "${docker_config_quoted}" "${service_suffix}" \
    "${docker_config_quoted}" "${service_suffix}"
}

list_bridge_logs() {
  local log_dir="${NAS_REMOTE_STATE_DIR}/logs"
  mkdir -p "${log_dir}"
  find "${log_dir}" -maxdepth 1 -type f -name '*.log' -print 2>/dev/null | sort
}

can_use_direct_ssh() {
  ssh -o BatchMode=yes -o ConnectTimeout=5 "${NAS_SSH_HOST}" 'printf ok' >/dev/null 2>&1
}

run_remote_direct() {
  local remote_command="$1"
  local remote_quoted
  remote_quoted="$(printf '%q' "${remote_command}")"

  log "Running on NAS via direct ssh (${NAS_SSH_HOST})"
  ssh -tt "${NAS_SSH_HOST}" "/bin/sh -lc ${remote_quoted}"
}

discover_bridge_log() {
  local before_logs="$1"
  local deadline=$((SECONDS + NAS_DISCOVERY_TIMEOUT))
  local candidate=""

  while (( SECONDS < deadline )); do
    while IFS= read -r candidate; do
      [[ -z "${candidate}" ]] && continue
      if ! printf '%s\n' "${before_logs}" | grep -Fqx "${candidate}"; then
        printf '%s' "${candidate}"
        return 0
      fi
    done < <(list_bridge_logs)
    sleep 1
  done

  return 1
}

wait_for_bridge_completion() {
  local log_file="$1"
  local deadline=$((SECONDS + NAS_WAIT_TIMEOUT))
  local exit_code=""

  log "Waiting for NAS bridge log: ${log_file}"

  while (( SECONDS < deadline )); do
    if [[ -f "${log_file}" ]] && grep -q '^\[nas-remote\] Exit: ' "${log_file}"; then
      exit_code="$(awk '/^\[nas-remote\] Exit: /{code=$3} END{print code}' "${log_file}")"
      if [[ "${exit_code}" != "0" ]]; then
        error "NAS command failed with exit ${exit_code}. Log: ${log_file}"
        tail -n 40 "${log_file}" >&2 || true
        return 1
      fi
      log "NAS command finished successfully. Log: ${log_file}"
      tail -n 20 "${log_file}" || true
      return 0
    fi
    sleep 1
  done

  error "Timed out waiting for NAS command completion. Log: ${log_file}"
  return 1
}

run_remote_via_bridge() {
  local remote_command="$1"
  local before_logs log_file

  if [[ ! -x "${NAS_REMOTE_BRIDGE}" ]]; then
    error "NAS bridge not executable: ${NAS_REMOTE_BRIDGE}"
    return 1
  fi

  before_logs="$(list_bridge_logs)"
  "${NAS_REMOTE_BRIDGE}" cmd "${remote_command}"
  log_file="$(discover_bridge_log "${before_logs}")" || {
    error "Failed to discover nas-remote log file under ${NAS_REMOTE_STATE_DIR}/logs"
    return 1
  }

  wait_for_bridge_completion "${log_file}"
}

run_nas_deploy() {
  local remote_command="$1"

  if can_use_direct_ssh; then
    run_remote_direct "${remote_command}"
  else
    run_remote_via_bridge "${remote_command}"
  fi
}

remote_command="$(build_remote_compose_command)"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "Local build script: ${BUILD_SCRIPT}"
  echo "Skip build+push: ${SKIP_BUILD_PUSH}"
  echo "Skip NAS deploy: ${SKIP_NAS_DEPLOY}"
  echo "Remote compose dir: ${NAS_COMPOSE_DIR}"
  echo "Remote Docker config: ${NAS_DOCKER_CONFIG}"
  if has_services; then
    echo "Services: ${SERVICES[*]}"
  else
    echo "Services: <all compose services>"
  fi
  echo "Remote command:"
  echo "${remote_command}"
  exit 0
fi

if [[ "${SKIP_BUILD_PUSH}" == "false" ]]; then
  log "Running local build+push via ${BUILD_SCRIPT}"
  (
    cd "${ROOT_DIR}"
    "${BUILD_SCRIPT}"
  )
fi

if [[ "${SKIP_NAS_DEPLOY}" == "false" ]]; then
  log "Deploying images on NAS from ${NAS_COMPOSE_DIR}"
  run_nas_deploy "${remote_command}"
fi

log "Release flow completed."

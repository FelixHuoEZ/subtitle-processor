#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${IMAGE_PREFIX:-}" && -f "${ROOT_DIR}/images.env" ]]; then
  echo "INFO: Loading environment from images.env"
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/images.env"
  set +a
fi

IMAGE_PREFIX="${IMAGE_PREFIX:-}"
BASE_IMAGE_REGISTRY="${BASE_IMAGE_REGISTRY:-}"
NAS_SSH_HOST="${NAS_SSH_HOST:-nas}"
NAS_DOCKER_CONFIG="${NAS_DOCKER_CONFIG:-/share/homes/hsk/.docker}"
NAS_PATH_PREFIX="${NAS_PATH_PREFIX:-/share/ZFS530_DATA/.qpkg/container-station/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
DRY_RUN=false
VERIFY_ONLY=false
IMAGES=()

DEFAULT_IMAGES=(
  "library/python:3.11-slim"
  "library/python:3.9-slim"
  "nvidia/cuda:11.8.0-base-ubuntu22.04"
  "brainicism/bgutil-ytdlp-pot-provider:1.2.2"
)

usage() {
  cat <<'EOF'
Usage:
  scripts/sync-base-images.sh [options]

Options:
  --image ref         Sync one upstream image path (repeatable), e.g. library/python:3.11-slim
  --verify-only       Skip copy and only inspect mirrored images on the registry.
  --dry-run           Print the commands without executing them.
  --host host         Override NAS SSH host alias. Default: nas
  --docker-config p   Override remote DOCKER_CONFIG path.
  -h, --help          Show this help.

Environment:
  BASE_IMAGE_REGISTRY Mirror prefix, e.g. 10.0.0.23:5443/dockerhub
  IMAGE_PREFIX        Used to derive BASE_IMAGE_REGISTRY=<registry>/dockerhub when unset
  NAS_SSH_HOST        SSH host for running mirror sync on the NAS
  NAS_DOCKER_CONFIG   Remote Docker auth path used on the NAS

Examples:
  ./scripts/sync-base-images.sh
  ./scripts/sync-base-images.sh --image library/python:3.11-slim
  ./scripts/sync-base-images.sh --verify-only
EOF
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

is_private_registry_host() {
  local host="${1:-}"
  if [[ -z "${host}" ]]; then
    return 1
  fi

  case "${host}" in
    docker.io|index.docker.io|registry-1.docker.io|ghcr.io|quay.io|registry.gitlab.com|public.ecr.aws)
      return 1
      ;;
  esac

  if [[ "${host}" == *.* || "${host}" == *:* ]]; then
    return 0
  fi

  return 1
}

remote_run() {
  local remote_command="$1"
  local remote_quoted

  remote_quoted="$(printf '%q' "${remote_command}")"
  ssh "${NAS_SSH_HOST}" "/bin/sh -lc ${remote_quoted}"
}

build_sync_command() {
  local source_path="$1"
  local dest_prefix="$2"
  local source_ref="docker.io/${source_path}"
  local dest_ref="${dest_prefix%/}/${source_path}"
  local dest_repo="${dest_ref%:*}"
  local dest_tag="${dest_ref##*:}"
  local amd64_ref="${dest_repo}:${dest_tag}-amd64"
  local arm64_ref="${dest_repo}:${dest_tag}-arm64"

  cat <<EOF
export PATH=$(printf '%q' "${NAS_PATH_PREFIX}"):\$PATH
export DOCKER_CONFIG=$(printf '%q' "${NAS_DOCKER_CONFIG}")
set -e
docker pull --platform linux/amd64 $(printf '%q' "${source_ref}") >/dev/null
docker tag $(printf '%q' "${source_ref}") $(printf '%q' "${amd64_ref}")
docker push $(printf '%q' "${amd64_ref}") >/dev/null
docker pull --platform linux/arm64 $(printf '%q' "${source_ref}") >/dev/null
docker tag $(printf '%q' "${source_ref}") $(printf '%q' "${arm64_ref}")
docker push $(printf '%q' "${arm64_ref}") >/dev/null
docker manifest rm $(printf '%q' "${dest_ref}") >/dev/null 2>&1 || true
docker manifest create $(printf '%q' "${dest_ref}") $(printf '%q' "${amd64_ref}") $(printf '%q' "${arm64_ref}") >/dev/null
docker manifest annotate $(printf '%q' "${dest_ref}") $(printf '%q' "${amd64_ref}") --arch amd64 >/dev/null
docker manifest annotate $(printf '%q' "${dest_ref}") $(printf '%q' "${arm64_ref}") --arch arm64 >/dev/null
docker manifest push --purge $(printf '%q' "${dest_ref}") >/dev/null
EOF
}

build_verify_command() {
  local source_path="$1"
  local dest_prefix="$2"
  local dest_ref="${dest_prefix%/}/${source_path}"
  cat <<EOF
export PATH=$(printf '%q' "${NAS_PATH_PREFIX}"):\$PATH
export DOCKER_CONFIG=$(printf '%q' "${NAS_DOCKER_CONFIG}")
docker manifest inspect $(printf '%q' "${dest_ref}")
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --image requires a value" >&2
        exit 1
      fi
      IMAGES+=("$(trim "$2")")
      shift 2
      ;;
    --verify-only)
      VERIFY_ONLY=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --host)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --host requires a value" >&2
        exit 1
      fi
      NAS_SSH_HOST="$2"
      shift 2
      ;;
    --docker-config)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --docker-config requires a value" >&2
        exit 1
      fi
      NAS_DOCKER_CONFIG="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ${#IMAGES[@]} -eq 0 ]]; then
  IMAGES=("${DEFAULT_IMAGES[@]}")
fi

if [[ -z "${BASE_IMAGE_REGISTRY}" && -n "${IMAGE_PREFIX}" ]]; then
  registry_host="${IMAGE_PREFIX%%/*}"
  if is_private_registry_host "${registry_host}"; then
    BASE_IMAGE_REGISTRY="${registry_host}/dockerhub"
    echo "INFO: BASE_IMAGE_REGISTRY not set; defaulting to ${BASE_IMAGE_REGISTRY} based on IMAGE_PREFIX registry."
  fi
fi

if [[ -z "${BASE_IMAGE_REGISTRY}" ]]; then
  echo "ERROR: BASE_IMAGE_REGISTRY is not set. Export it or configure IMAGE_PREFIX in images.env." >&2
  exit 1
fi

echo "INFO: Using base image registry: ${BASE_IMAGE_REGISTRY}"
echo "INFO: Running sync on host: ${NAS_SSH_HOST}"

for image in "${IMAGES[@]}"; do
  if [[ -z "${image}" ]]; then
    continue
  fi

  echo "INFO: Processing ${image}"
  if [[ "${VERIFY_ONLY}" == "true" ]]; then
    remote_command="$(build_verify_command "${image}" "${BASE_IMAGE_REGISTRY}")"
  else
    remote_command="$(build_sync_command "${image}" "${BASE_IMAGE_REGISTRY}")"
  fi

  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "DRY-RUN (${NAS_SSH_HOST}):"
    printf '%s\n' "${remote_command}"
    continue
  fi

  remote_output="$(remote_run "${remote_command}")"
  if [[ "${VERIFY_ONLY}" == "true" ]]; then
    REMOTE_OUTPUT="${remote_output}" python3 -c '
import json
import os

payload = json.loads(os.environ["REMOTE_OUTPUT"])
platforms = []
for manifest in payload.get("manifests", []):
    platform = manifest.get("platform", {})
    arch = platform.get("architecture", "unknown")
    os_name = platform.get("os", "unknown")
    variant = platform.get("variant")
    label = f"{os_name}/{arch}"
    if variant:
        label += f"/{variant}"
    platforms.append(label)
print("INFO: Verified platforms:", ", ".join(platforms) if platforms else "<none>")
'
  else
    echo "INFO: Synced ${BASE_IMAGE_REGISTRY%/}/${image}"
  fi
done

#!/usr/bin/env bash
set -euo pipefail

# Build and optionally push project images for all services.
# Usage:
#   IMAGE_PREFIX=myrepo/subtitle IMAGE_TAG=v1 ./scripts/build-and-push.sh
#   IMAGE_PREFX=myrepo/subtitle PUSH=false ./scripts/build-and-push.sh
#
# Environment variables:
#   IMAGE_PREFIX   Optional registry/namespace prefix (e.g. myorg or registry/team).
#   IMAGE_TAG      Image tag (defaults to current git short SHA or 'latest').
#   PUSH           If 'true' (default) the script pushes after build. Set to 'false' to skip push.
#   PLATFORMS      Target platforms for buildx (default: linux/amd64).
#   LOAD           When PUSH=false, set LOAD=true to load the image into local Docker (uses --load).
#   DOCKERFILE_*   Optional overrides for dockerfile path per service (see map below).
#
# Services and build contexts:
#   subtitle-processor -> ./ (Dockerfile)
#   transcribe-audio   -> ./transcribe-audio (Dockerfile)
#   telegram-bot       -> ./telegram-bot (Dockerfile)

# IMAGE_PREFIX can include registry/namespace (e.g. registry.gitlab.com/org/project).
# When omitted the images are tagged locally without a registry prefix.
if [[ -z "${IMAGE_PREFIX:-}" ]]; then
  echo "WARN: IMAGE_PREFIX not set; images will be tagged locally only." >&2
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  DEFAULT_TAG=$(git rev-parse --short HEAD 2>/dev/null || true)
  DEFAULT_TAG=${DEFAULT_TAG:-latest}
else
  DEFAULT_TAG=latest
fi

IMAGE_TAG=${IMAGE_TAG:-$DEFAULT_TAG}
PUSH=${PUSH:-true}
PLATFORMS=${PLATFORMS:-linux/amd64}
if [[ "${PUSH}" == "true" ]]; then
  LOAD=${LOAD:-false}
else
  LOAD=${LOAD:-true}
fi

SERVICES=(
  "subtitle-processor=.:Dockerfile"
  "transcribe-audio=transcribe-audio:Dockerfile"
  "telegram-bot=telegram-bot:Dockerfile"
)

check_buildx() {
  if ! docker buildx version >/dev/null 2>&1; then
    echo "ERROR: docker buildx not available. Install Docker Buildx plugin or Docker 20.10+." >&2
    exit 1
  fi
}

build_service() {
  local name="$1" context="$2" dockerfile="$3"
  local repo_path="${name}"
  if [[ -n "${IMAGE_PREFIX}" ]]; then
    repo_path="${IMAGE_PREFIX}/${name}"
  fi
  local image_repo="${repo_path}:${IMAGE_TAG}"

  echo "==> Building ${image_repo}"
  local cmd=(docker buildx build "${context}" --platform "${PLATFORMS}" -t "${image_repo}" -f "${context}/${dockerfile}")

  if [[ "${PUSH}" == "true" ]]; then
    cmd+=(--push)
  else
    # Ensure single platform when loading locally
    if [[ "${PLATFORMS}" != "linux/amd64" ]]; then
      echo "WARN: --load only supports single-platform builds; overriding PLATFORMS to linux/amd64 for local load." >&2
      cmd=(docker buildx build "${context}" --platform linux/amd64 -t "${image_repo}" -f "${context}/${dockerfile}")
    fi
    cmd+=(--load)
    echo "NOTE: PUSH=false, image will be loaded into local Docker engine." >&2
  fi

  "${cmd[@]}"
}

check_buildx

for entry in "${SERVICES[@]}"; do
  IFS='=' read -r name mapping <<< "${entry}"
  IFS=':' read -r context default_dockerfile <<< "${mapping}"

  # Allow per-service dockerfile override via env var (e.g. DOCKERFILE_SUBTITLE_PROCESSOR)
  env_var="DOCKERFILE_${name//-/_}"
  dockerfile=${!env_var:-${default_dockerfile}}

  if [[ ! -f "${context}/${dockerfile}" ]]; then
    echo "ERROR: Dockerfile '${context}/${dockerfile}' not found for service ${name}" >&2
    exit 1
  fi

  build_service "${name}" "${context}" "${dockerfile}"

done

echo "All images processed with tag '${IMAGE_TAG}'."

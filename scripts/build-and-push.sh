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
#   LOAD_LOCAL_PLATFORM When PUSH=true, set to 'false' to skip the local --load pass (default: true).
#   EXTRA_TAGS     Optional comma separated extra tags to publish (e.g. "latest,prod").
#   DOCKERFILE_*   Optional overrides for dockerfile path per service (see map below).
#
# Services and build contexts:
#   subtitle-processor -> ./ (Dockerfile)
#   transcribe-audio   -> ./transcribe-audio (Dockerfile)
#   telegram-bot       -> ./telegram-bot (Dockerfile)

# IMAGE_PREFIX can include registry/namespace (e.g. registry.gitlab.com/org/project).
# When omitted the images are tagged locally without a registry prefix.
# Auto-load images.env if present and IMAGE_PREFIX not preset
if [[ -z "${IMAGE_PREFIX:-}" && -f "${PWD}/images.env" ]]; then
  echo "INFO: Loading environment from images.env"
  set -a
  # shellcheck disable=SC1091
  source "${PWD}/images.env"
  set +a
fi

if [[ -z "${IMAGE_PREFIX:-}" ]]; then
  echo "WARN: IMAGE_PREFIX not set; images will be tagged locally only." >&2
fi
IMAGE_PREFIX=${IMAGE_PREFIX:-}

CACHE_DIR="${PWD}/.image-cache"
mkdir -p "${CACHE_DIR}"

log_debug() {
  echo "DEBUG: $*" >&2
}

add_cli_plugin_dir() {
  local plugin_dir="$1"
  if [[ -z "${plugin_dir}" || ! -d "${plugin_dir}" ]]; then
    return
  fi

  log_debug "Checking CLI plugin directory: ${plugin_dir}"

  if [[ -z "${DOCKER_CLI_PLUGIN_EXTRA_DIRS:-}" ]]; then
    export DOCKER_CLI_PLUGIN_EXTRA_DIRS="${plugin_dir}"
  elif [[ ":${DOCKER_CLI_PLUGIN_EXTRA_DIRS}:" != *":${plugin_dir}:"* ]]; then
    export DOCKER_CLI_PLUGIN_EXTRA_DIRS="${plugin_dir}:${DOCKER_CLI_PLUGIN_EXTRA_DIRS}"
  fi

  if [[ -f "${plugin_dir}/docker-buildx" && ! -e "${DOCKER_CONFIG}/cli-plugins/docker-buildx" ]]; then
    log_debug "Linking docker-buildx from ${plugin_dir} into ${DOCKER_CONFIG}/cli-plugins"
    ln -sf "${plugin_dir}/docker-buildx" "${DOCKER_CONFIG}/cli-plugins/docker-buildx"
  fi
}

# Ensure we have a usable Docker config directory without hiding local plugins
if [[ -z "${DOCKER_CONFIG:-}" ]]; then
  if [[ -n "${CI:-}" ]]; then
    export DOCKER_CONFIG="${PWD}/.docker"
  else
    export DOCKER_CONFIG="${HOME:-}/.docker"
  fi
fi

mkdir -p "${DOCKER_CONFIG}"
mkdir -p "${DOCKER_CONFIG}/cli-plugins"

host_cli_plugins="${HOME:-}/.docker/cli-plugins"
add_cli_plugin_dir "${host_cli_plugins}"

docker_cli_path="$(command -v docker 2>/dev/null || true)"
if [[ -n "${docker_cli_path}" ]]; then
  docker_cli_dir="$(cd "$(dirname "${docker_cli_path}")" && pwd)"
  log_debug "Detected docker CLI at ${docker_cli_path}"
  add_cli_plugin_dir "${docker_cli_dir}/../libexec/docker/cli-plugins"
  add_cli_plugin_dir "${docker_cli_dir}/../lib/docker/cli-plugins"
  add_cli_plugin_dir "${docker_cli_dir}/../Resources/cli-plugins"
fi

add_cli_plugin_dir "/Applications/Docker.app/Contents/Resources/cli-plugins"
add_cli_plugin_dir "/usr/libexec/docker/cli-plugins"
add_cli_plugin_dir "/usr/lib/docker/cli-plugins"
add_cli_plugin_dir "/usr/local/lib/docker/cli-plugins"

log_debug "Using DOCKER_CONFIG=${DOCKER_CONFIG}"
log_debug "DOCKER_CLI_PLUGIN_EXTRA_DIRS=${DOCKER_CLI_PLUGIN_EXTRA_DIRS:-<unset>}"

BUILDKIT_CONFIG_PATH=""
if [[ -n "${IMAGE_PREFIX}" ]]; then
  REGISTRY_HOST="${IMAGE_PREFIX%%/*}"
  if [[ "${REGISTRY_HOST}" == *:* ]]; then
    BUILDKIT_CONFIG_PATH="${DOCKER_CONFIG}/buildkitd.toml"
    cat >"${BUILDKIT_CONFIG_PATH}" <<EOF
[registry."${REGISTRY_HOST}"]
  insecure = true
EOF
  fi
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
EXTRA_TAGS=${EXTRA_TAGS:-}

SERVICES=(
  "subtitle-processor=.:Dockerfile"
  "transcribe-audio=transcribe-audio:Dockerfile"
  "telegram-bot=telegram-bot:Dockerfile"
)

USE_BUILDX=true

check_buildx() {
  log_debug "Entering check_buildx"
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker CLI not available; install Docker to continue." >&2
    exit 1
  fi

  log_debug "Running 'docker buildx version'"
  local buildx_output=""
  local buildx_status=0
  set +e
  buildx_output="$(docker buildx version 2>&1)"
  buildx_status=$?
  set -e
  if [[ ${buildx_status} -ne 0 ]]; then
    log_debug "'docker buildx version' failed (exit ${buildx_status}): ${buildx_output}"
    echo "WARN: docker buildx not available (exit ${buildx_status}); falling back to 'docker build' (single-platform)." >&2
    echo "${buildx_output}" >&2
    USE_BUILDX=false
    return 0
  fi
  log_debug "'docker buildx version' succeeded: ${buildx_output}"

  log_debug "Inspecting buildx builder 'repo-builder'"
  if ! docker buildx inspect repo-builder >/dev/null 2>&1; then
    echo "Creating buildx builder 'repo-builder' (docker-container driver)"
    local create_cmd=(docker buildx create --name repo-builder --driver docker-container --driver-opt network=host --driver-opt env.BUILDKIT_TLS_INSECURE_SKIP_VERIFY=1)
    if [[ -n "${BUILDKIT_CONFIG_PATH}" ]]; then
      create_cmd+=(--config "${BUILDKIT_CONFIG_PATH}")
    fi
    create_cmd+=(--use)
    "${create_cmd[@]}" >/dev/null
  else
    docker buildx use repo-builder >/dev/null
  fi

  log_debug "Bootstrapping buildx builder 'repo-builder'"
  docker buildx inspect repo-builder --bootstrap >/dev/null
  log_debug "Builder 'repo-builder' ready"
}

trim() {
  local value="$1"
  # shellcheck disable=SC2001
  value="$(echo "$value" | sed -e 's/^\s*//' -e 's/\s*$//')"
  printf '%s' "$value"
}

detect_host_platform() {
  if [[ -n "${HOST_PLATFORM:-}" ]]; then
    printf '%s' "${HOST_PLATFORM}"
    return 0
  fi

  local docker_arch
  docker_arch=$(docker info --format '{{.OSType}}/{{.Architecture}}' 2>/dev/null || true)
  docker_arch=$(trim "${docker_arch}")
  case "${docker_arch}" in
    linux/x86_64|linux/amd64)
      echo "linux/amd64"
      return 0
      ;;
    linux/arm64|linux/aarch64)
      echo "linux/arm64"
      return 0
      ;;
    linux/arm/v7|linux/armv7l|linux/armv7)
      echo "linux/arm/v7"
      return 0
      ;;
    linux/arm/v6|linux/armv6l|linux/armv6)
      echo "linux/arm/v6"
      return 0
      ;;
    linux/*)
      echo "${docker_arch}"
      return 0
      ;;
  esac

  local uname_arch
  uname_arch=$(uname -m 2>/dev/null || true)
  case "${uname_arch}" in
    x86_64|amd64)
      echo "linux/amd64"
      return 0
      ;;
    arm64|aarch64)
      echo "linux/arm64"
      return 0
      ;;
    armv7l|armv7)
      echo "linux/arm/v7"
      return 0
      ;;
    armv6l|armv6)
      echo "linux/arm/v6"
      return 0
      ;;
  esac

  return 1
}

compute_context_hash() {
  local abs_path="$1"
  python3 - "$abs_path" <<'PY'
import hashlib, os, sys

path = sys.argv[1]
# Skip transient build artefacts and large mounted directories so hashing stays fast.
exclude_dirs = {'.git', '.docker', '.image-cache', '__pycache__', '.pycache',
                '.venv', 'venv', 'env',
                'uploads', 'videos', 'outputs', 'models', 'firefox_profile'}
exclude_prefixes = (
    'chrome-extension/node_modules',
    'chrome-extension/dist',
    'telegram-bot/node_modules',
)
exclude_files = {'.DS_Store'}
exclude_ext = {'.pyc', '.pyo', '.log'}

hasher = hashlib.sha1()

for root, dirs, files in os.walk(path):
    rel_root = os.path.relpath(root, path)
    rel_root = '' if rel_root == '.' else rel_root

    filtered_dirs = []
    for d in dirs:
        if d in exclude_dirs:
            continue
        rel_dir = os.path.join(rel_root, d) if rel_root else d
        if any(rel_dir == prefix or rel_dir.startswith(prefix + os.sep)
               for prefix in exclude_prefixes):
            continue
        filtered_dirs.append(d)
    dirs[:] = filtered_dirs

    for filename in sorted(files):
        if filename in exclude_files or os.path.splitext(filename)[1] in exclude_ext:
            continue
        full_path = os.path.join(root, filename)
        rel_path = os.path.relpath(full_path, path)
        hasher.update(rel_path.encode('utf-8'))
        with open(full_path, 'rb') as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)

print(hasher.hexdigest())
PY
}

build_service() {
  local name="$1" context="$2" dockerfile="$3"
  log_debug "Preparing service=${name} context=${context} dockerfile=${dockerfile}"
  local repo_path="${name}"
  if [[ -n "${IMAGE_PREFIX}" ]]; then
    repo_path="${IMAGE_PREFIX}/${name}"
  fi
  local tags=("${repo_path}:${IMAGE_TAG}")

  local context_abs
  if [[ "${context}" == "." ]]; then
    context_abs="${PWD}"
  else
    context_abs="${PWD}/${context}"
  fi

  local context_hash
  log_debug "Computing context hash for ${name}"
  context_hash=$(compute_context_hash "${context_abs}")
  log_debug "Context hash for ${name}: ${context_hash}"
  local cache_file="${CACHE_DIR}/${name}.sha"

  if [[ -n "${EXTRA_TAGS}" ]]; then
    IFS=',' read -r -a extra_raw <<< "${EXTRA_TAGS}"
    for item in "${extra_raw[@]}"; do
      local cleaned
      cleaned=$(trim "$item")
      if [[ -n "$cleaned" ]]; then
        tags+=("${repo_path}:${cleaned}")
      fi
    done
  fi

  if [[ "${SKIP_UNCHANGED:-true}" == "true" && -f "${cache_file}" ]]; then
    local cached_hash
    cached_hash=$(cat "${cache_file}")
    if [[ "${context_hash}" == "${cached_hash}" ]]; then
      echo "==> Skipping ${name}; build context unchanged (${context_hash})"
      return 0
    fi
  fi

  local cache_dir="${CACHE_DIR}/${name}-layers"
  mkdir -p "${cache_dir}"
  local cache_args=()
  local build_platform="${PLATFORMS}"

  if [[ "${USE_BUILDX}" == "true" ]]; then
    log_debug "Building ${name} via buildx for platform(s) ${build_platform}"
    cache_args=(--cache-to "type=local,dest=${cache_dir},mode=max")
    if [[ -f "${cache_dir}/index.json" ]]; then
      cache_args=(--cache-from "type=local,src=${cache_dir}" "${cache_args[@]}")
    fi

    local output_args=()
    if [[ "${PUSH}" == "true" ]]; then
      output_args+=(--push)
    else
      if [[ "${PLATFORMS}" != "linux/amd64" ]]; then
        echo "WARN: --load only supports single-platform builds; overriding PLATFORMS to linux/amd64 for local load." >&2
        build_platform="linux/amd64"
      fi
      output_args+=(--load)
      echo "NOTE: PUSH=false, image will be loaded into local Docker engine." >&2
    fi

    echo "==> Building ${tags[*]}"
    local cmd=(docker buildx build "${context}" --platform "${build_platform}" -f "${context}/${dockerfile}")
    for tag in "${tags[@]}"; do
      cmd+=(-t "${tag}")
    done
    cmd+=("${cache_args[@]}")
    cmd+=("${output_args[@]}")

    log_debug "Executing: ${cmd[*]}"
    if "${cmd[@]}"; then
      log_debug "Build completed for ${name}"
      printf '%s\n' "${context_hash}" > "${cache_file}"

      if [[ "${PUSH}" == "true" && "${LOAD_LOCAL_PLATFORM:-true}" == "true" ]]; then
        local host_platform
        if host_platform=$(detect_host_platform); then
          log_debug "Loading ${name} for host platform ${host_platform}"
          echo "==> Loading ${tags[0]} locally for ${host_platform}"
          local load_cmd=(docker buildx build "${context}" --platform "${host_platform}" -f "${context}/${dockerfile}" --load)
          for tag in "${tags[@]}"; do
            load_cmd+=(-t "${tag}")
          done
          local load_cache_args=(--cache-to "type=local,dest=${cache_dir},mode=max")
          if [[ -f "${cache_dir}/index.json" ]]; then
            load_cache_args=(--cache-from "type=local,src=${cache_dir}" "${load_cache_args[@]}")
          fi
          load_cmd+=("${load_cache_args[@]}")
          log_debug "Executing load command: ${load_cmd[*]}"
          if ! "${load_cmd[@]}"; then
            echo "WARN: failed to load local image for ${name} (${host_platform}); continuing without local copy" >&2
          fi
        else
          echo "WARN: unable to detect host platform; skip local load for ${name}" >&2
        fi
      fi
    fi
    return
  fi

  # Fallback path when buildx is unavailable
  local primary_platform="${PLATFORMS%%,*}"
  if [[ "${PLATFORMS}" == *","* ]]; then
    echo "WARN: multiple platforms requested (${PLATFORMS}) but buildx is unavailable; building only for ${primary_platform}." >&2
  fi
  log_debug "Falling back to docker build for ${name}"
  if [[ "${PUSH}" == "false" && "${LOAD}" == "false" ]]; then
    echo "NOTE: docker build always loads the resulting image into the local engine." >&2
  fi
  echo "==> Building ${tags[*]} (docker build fallback)"
  local build_cmd=(docker build "${context}" -f "${context}/${dockerfile}")
  for tag in "${tags[@]}"; do
    build_cmd+=(-t "${tag}")
  done
  if "${build_cmd[@]}"; then
    printf '%s\n' "${context_hash}" > "${cache_file}"
    if [[ "${PUSH}" == "true" ]]; then
      for tag in "${tags[@]}"; do
        echo "==> Pushing ${tag}"
        docker push "${tag}"
      done
    fi
  fi
}

log_debug "Invoking check_buildx"
check_buildx

log_debug "Starting service build loop"
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
log_debug "Completed service build loop"

echo "All images processed with tag '${IMAGE_TAG}'."

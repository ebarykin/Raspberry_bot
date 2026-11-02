#!/usr/bin/env bash
# =============================================================================
# jetson_docker.sh — хост-скрипт стенда на Jetson (не entrypoint контейнера).
#
# Образ (registry) = приложение (capture_pipeline/, entrypoint).
# На хосте из git: docker/compose.yaml, этот скрипт, configs/*.yaml (mount).
#
# up/pull: pull IMAGE + compose up. build: локально на плате (make capture-build).
# Ещё: wait Argus, stop native, L4T override/.so, cpuset, rmem / gst cache.
#
#   IMAGE_TAG=latest bash scripts/jetson_docker.sh up configs/jetson-duo-one-s.yaml
#   make capture-up   # с ноутбука → jetson_remote_docker.sh → этот скрипт
#
# Env: JETPACK IMAGE_TAG IMAGE IMAGE_REGISTRY CONFIG ROS_DOMAIN_ID
#      CPUSET_CPUS PULL_POLICY NAME
# =============================================================================
set -euo pipefail

DEFAULT_CONTAINER_NAME="capture-pipeline"
DEFAULT_CONFIG_REL="configs/jetson-duo-one-s.yaml"
DEFAULT_ROS_DOMAIN_ID="0"
# Пустой CPUSET_CPUS = все ядра. CPUSET_CPUS=0 — pin на одно ядро (sandbox).
DEFAULT_CPUSET_CPUS=""
DEFAULT_IMAGE_REGISTRY="cr.selcloud.ru/sandbox"
DEFAULT_IMAGE_TAG="latest"

NAME="${NAME:-$DEFAULT_CONTAINER_NAME}"
# ${VAR-default}: только если unset; пустой CPUSET_CPUS= отключает pin.
CPUSET_CPUS="${CPUSET_CPUS-$DEFAULT_CPUSET_CPUS}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$DEFAULT_ROS_DOMAIN_ID}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-$DEFAULT_IMAGE_REGISTRY}"
IMAGE_TAG="${IMAGE_TAG:-$DEFAULT_IMAGE_TAG}"
PULL_POLICY="${PULL_POLICY:-always}"

ACTION="${1:-}"
CONFIG_REL="${2:-${CONFIG:-$DEFAULT_CONFIG_REL}}"
DRIVER_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
CONFIG_HOST="${DRIVER_DIR}/${CONFIG_REL}"
DOCKER=(docker)
COMPOSE_FILE="${DRIVER_DIR}/docker/compose.yaml"
COMPOSE_L4T="${DRIVER_DIR}/docker/compose.l4t.override.yaml"

usage() {
  echo "usage: IMAGE_TAG=${DEFAULT_IMAGE_TAG} JETPACK=6.2|7.2 $0 {pull|build|up|down|restart|logs|status} [configs/....yaml]" >&2
  echo "  up/pull — registry (6.2 роверы, 7.2 робособаки); build — локальный образ." >&2
  echo "  без JETPACK: /etc/nv_tegra_release (R36→6.2, R39→7.2)." >&2
  exit 2
}

# JetPack → Dockerfile (для build) и имя репозитория образа (для pull/up).
detect_jetpack() {
  if [[ -z "${JETPACK:-}" && -f /etc/nv_tegra_release ]]; then
    local major
    major="$(sed -n 's/.*# R\([0-9][0-9]*\).*/\1/p' /etc/nv_tegra_release | head -1)"
    case "${major}" in
      36) JETPACK=6.2 ;;
      39) JETPACK=7.2 ;;
      *)
        echo "L4T R${major:-?} не сопоставлен. Роверы: JETPACK=6.2, робособаки: JETPACK=7.2" >&2
        exit 1
        ;;
    esac
    echo "JETPACK=${JETPACK} (L4T R${major})"
  fi
  case "${JETPACK:-}" in
    6.2) DOCKERFILE="${DRIVER_DIR}/docker/Dockerfile.jp62" ;;
    7.2) DOCKERFILE="${DRIVER_DIR}/docker/Dockerfile.jp72" ;;
    *)
      echo "задайте JETPACK=6.2 (роверы) или JETPACK=7.2 (робособаки)" >&2
      exit 1
      ;;
  esac
  LOCAL_IMAGE="capture-pipeline:jp${JETPACK/./}"
  if [[ -z "${IMAGE:-}" ]]; then
    if [[ "$ACTION" == "build" ]]; then
      IMAGE="${LOCAL_IMAGE}"
    elif [[ "$JETPACK" == "6.2" ]]; then
      IMAGE="${IMAGE_REGISTRY}/last-mile/drivers/capture-pipeline:${IMAGE_TAG}"
    else
      IMAGE="${IMAGE_REGISTRY}/last-mile/drivers/capture-pipeline-jp72:${IMAGE_TAG}"
    fi
  fi
  if [[ -n "${BASE_IMAGE:-}${ROS_DISTRO:-}${ROS_APT_SUITE:-}" ]]; then
    echo "BASE_IMAGE/ROS_DISTRO/ROS_APT_SUITE игнорируются, платформа — ${DOCKERFILE}" >&2
  fi
}

# docker из группы пользователя, иначе sudo -n (без пароля — иначе make/ssh ломается).
resolve_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "docker не установлен: sudo apt-get install -y docker.io && sudo usermod -aG docker \$USER" >&2
    exit 1
  }
  docker info >/dev/null 2>&1 && return 0
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    return 0
  fi
  echo "нет доступа к docker daemon (группа docker или sudo)" >&2
  exit 1
}

d() { "${DOCKER[@]}" "$@"; }

require_compose() {
  if ! d compose version >/dev/null 2>&1; then
    echo "нужен Docker Compose v2: docker compose version" >&2
    exit 1
  fi
  [[ -f "$COMPOSE_FILE" ]] || {
    echo "нет ${COMPOSE_FILE} — сделайте make capture-git-sync (git checkout develop на плате)" >&2
    exit 1
  }
}

# nvargus-daemon → /tmp/argus_socket; контейнер ходит туда через -v /tmp.
# nvv4l2 / v4l2 без Argus — wait пропускаем (grep YAML без комментариев).
wait_argus() {
  local _i
  for _i in $(seq 1 60); do
    [[ -S /tmp/argus_socket ]] && return 0
    sleep 1
  done
  echo "нет /tmp/argus_socket — nvargus-daemon не готов" >&2
  return 1
}

config_needs_argus() {
  # Явный return: при set -e + pipefail голый grep -q рвёт скрипт, если строки нет.
  if grep -vE '^[[:space:]]*#' "$CONFIG_HOST" | grep -q 'nvarguscamerasrc'; then
    return 0
  fi
  return 1
}

# Гасим native python из DRIVER_DIR, иначе делит Argus/RTSP с контейнером.
stop_native() {
  local ctl="${DRIVER_DIR}/scripts/jetson_driver_ctl.sh"
  [[ -x "$ctl" ]] && bash "$ctl" stop "$DRIVER_DIR" "$CONFIG_REL" || true
}

# Опциональные bind'ы (нет на каждой L4T). create_host_path=false — иначе
# Docker создаст каталог вместо отсутствующего .so.
write_l4t_override() {
  local src dest spec lib destlib
  local -a extras=()
  for spec in \
    /usr/lib/aarch64-linux-gnu/nvidia:/usr/lib/aarch64-linux-gnu/nvidia:ro \
    /usr/share/glvnd/egl_vendor.d:/usr/share/glvnd/egl_vendor.d:ro \
    /etc/ld.so.conf.d/nvidia-tegra.conf:/etc/ld.so.conf.d/nvidia-tegra.conf:ro \
    /opt/nvidia/l4t-gpu-libs:/opt/nvidia/l4t-gpu-libs:ro \
    /usr/lib/aarch64-linux-gnu/libcuda.so:/usr/lib/aarch64-linux-gnu/libcuda.so:ro \
    /usr/lib/aarch64-linux-gnu/libcuda.so.1:/usr/lib/aarch64-linux-gnu/libcuda.so.1:ro
  do
    src="${spec%%:*}"
    dest="${spec#*:}"
    dest="${dest%:*}"
    if [[ -e "$src" ]]; then
      extras+=("$src" "$dest")
    fi
  done
  for lib in libgstnvexifmeta.so libgstnvivameta.so libgstnvegl-1.0.so.0; do
    for destlib in \
      "/usr/lib/aarch64-linux-gnu/${lib}" \
      "/usr/lib/aarch64-linux-gnu/tegra/${lib}" \
      "/usr/lib/aarch64-linux-gnu/nvidia/${lib}"
    do
      if [[ -e "$destlib" ]]; then
        extras+=("$destlib" "$destlib")
      fi
    done
  done
  {
    echo "services:"
    echo "  capture-pipeline:"
    if [[ ${#extras[@]} -gt 0 ]]; then
      echo "    volumes:"
      local i
      for ((i = 0; i < ${#extras[@]}; i += 2)); do
        echo "      - type: bind"
        echo "        source: ${extras[i]}"
        echo "        target: ${extras[i+1]}"
        echo "        read_only: true"
        echo "        bind:"
        echo "          create_host_path: false"
      done
    fi
    if [[ -n "${CPUSET_CPUS}" ]]; then
      echo "    cpuset: \"${CPUSET_CPUS}\""
    fi
  } > "$COMPOSE_L4T"
  if [[ ${#extras[@]} -eq 0 && -z "${CPUSET_CPUS}" ]]; then
    rm -f "$COMPOSE_L4T"
  fi
}

compose() {
  # project-directory сам по себе читает только $DRIVER_DIR/.env;
  # стендовый файл — docker/.env (см. docker/.env.example).
  local -a files=(-f "$COMPOSE_FILE")
  local -a env_file=()
  [[ -f "$COMPOSE_L4T" ]] && files+=(-f "$COMPOSE_L4T")
  [[ -f "${DRIVER_DIR}/docker/.env" ]] && env_file=(--env-file "${DRIVER_DIR}/docker/.env")
  # Явный IMAGE/CONFIG с CLI важнее значений из docker/.env.
  CAPTURE_IMAGE="$IMAGE" CONFIG_HOST="$CONFIG_HOST" ROS_DOMAIN_ID="$ROS_DOMAIN_ID" \
    PULL_POLICY="$PULL_POLICY" \
    "${DOCKER[@]}" compose "${files[@]}" "${env_file[@]}" --project-directory "$DRIVER_DIR" "$@"
}

image_is_remote() {
  [[ "$IMAGE" == */* ]]
}

should_pull() {
  case "$PULL_POLICY" in
    never) return 1 ;;
    missing)
      d image inspect "$IMAGE" >/dev/null 2>&1 && return 1
      ;;
  esac
  image_is_remote
}

# После up/restart: rmem для CycloneDDS (entrypoint тоже поднимает, но после
# reboot --restart наш up не вызывается — persist в sysctl.d), gst cache,
# kmod в старых локальных образах (в актуальных Dockerfile уже есть).
ensure_rmem() {
  local need=16777216
  local cur persist
  cur="$(sysctl -n net.core.rmem_max 2>/dev/null || echo 0)"
  if [[ "${cur:-0}" -ge "$need" ]]; then
    return 0
  fi
  if d exec -u root "$NAME" \
    sysctl -w "net.core.rmem_max=${need}" "net.core.rmem_default=${need}" >/dev/null 2>&1; then
    true
  elif sudo -n sysctl -w "net.core.rmem_max=${need}" "net.core.rmem_default=${need}" >/dev/null 2>&1; then
    true
  else
    echo "нет rmem_max=${need} (сейчас ${cur}) — CycloneDDS не создаст domain, RTSP без factory" >&2
    return 1
  fi
  persist="net.core.rmem_max=${need}"$'\n'"net.core.rmem_default=${need}"$'\n'
  if [[ -w /etc/sysctl.d ]]; then
    printf '%s' "$persist" > /etc/sysctl.d/99-zed-cyclonedds-rmem.conf
  elif sudo -n true >/dev/null 2>&1; then
    printf '%s' "$persist" | sudo -n tee /etc/sysctl.d/99-zed-cyclonedds-rmem.conf >/dev/null
  fi
}

prepare_runtime() {
  ensure_rmem || true
  d exec -u root "$NAME" rm -rf /root/.cache/gstreamer-1.0 >/dev/null 2>&1 || true
  # non-login PATH часто без /usr/sbin, где лежит lsmod.
  if d exec "$NAME" bash -lc 'command -v lsmod >/dev/null || test -x /usr/sbin/lsmod'; then
    return 0
  fi
  echo "в образе нет kmod — ставлю (пересоберите image из текущего Dockerfile)" >&2
  d exec -u root "$NAME" bash -lc \
    'apt-get update -qq && apt-get install -y -qq kmod && rm -rf /root/.cache/gstreamer-1.0'
  d restart "$NAME" >/dev/null
  ensure_rmem || true
}

do_pull() {
  require_compose
  write_l4t_override
  echo "pull ${IMAGE}"
  compose pull
}

do_build() {
  echo "build ${IMAGE} (JETPACK=${JETPACK} ${DOCKERFILE})"
  d build -f "${DOCKERFILE}" -t "${IMAGE}" "${DRIVER_DIR}"
}

do_down() {
  if [[ -f "$COMPOSE_FILE" ]] && d compose version >/dev/null 2>&1; then
    write_l4t_override
    compose down --remove-orphans >/dev/null 2>&1 || true
  fi
  d rm -f "$NAME" >/dev/null 2>&1 || true
}

do_up() {
  [[ -f "$CONFIG_HOST" ]] || { echo "нет конфига: $CONFIG_HOST" >&2; exit 1; }
  require_compose
  if config_needs_argus; then
    wait_argus
  fi
  stop_native
  write_l4t_override
  if should_pull; then
    echo "pull ${IMAGE}"
    compose pull
  elif ! d image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "нет образа ${IMAGE}. Для registry: docker login cr.selcloud.ru" >&2
    echo "локально: make capture-build, затем IMAGE=${LOCAL_IMAGE} PULL_POLICY=never $0 up" >&2
    exit 1
  fi
  do_down
  echo "start ${NAME} image=${IMAGE} config=${CONFIG_REL}"
  compose up -d --remove-orphans --force-recreate --no-build
  prepare_runtime
  d ps --filter "name=${NAME}" --format '{{.Status}}'
}

do_restart() {
  if d inspect "$NAME" >/dev/null 2>&1; then
    d restart "$NAME"
    prepare_runtime
  else
    do_up
  fi
}

[[ -n "$ACTION" ]] || usage
resolve_docker
detect_jetpack
case "$ACTION" in
  pull) do_pull ;;
  build) do_build ;;
  up) do_up ;;
  down) do_down ;;
  restart) do_restart ;;
  logs)
    if [[ -f "$COMPOSE_FILE" ]] && d compose version >/dev/null 2>&1; then
      write_l4t_override
      compose logs --tail 80 -f
    else
      d logs --tail 80 -f "$NAME"
    fi
    ;;
  status)
    d ps -a --filter "name=${NAME}"
    ss -ltn | grep -E ':8586|:8591' || true
    ;;
  *) usage ;;
esac

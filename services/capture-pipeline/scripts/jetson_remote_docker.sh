#!/usr/bin/env bash
# Ноутбук → SSH → scripts/jetson_docker.sh на плате (make capture-*).
# usage: jetson_remote_docker.sh {up|pull|down|logs|status|git-sync|build}
set -euo pipefail

HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
# Не писать `}` внутри ${1:?...} — bash закрывает подстановку на первой `}`.
ACTION="${1:?usage: $0 up|pull|down|logs|status|git-sync|build}"

JETSON_DIR="${JETSON_DIR:-~/capture-pipeline}"
GIT_BRANCH="${GIT_BRANCH:-develop}"
GIT_REPO="${GIT_REPO:-git@gitlab.cyberchef.tech:last-mile-robotics/implementation/drivers/cameras/capture-pipeline.git}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
PULL_POLICY="${PULL_POLICY:-always}"
IMAGE="${IMAGE:-}"
JETPACK="${JETPACK:-}"
# Пусто = все ядра на плате; CPUSET_CPUS=7 — pin контейнера на ядро 7.
CPUSET_CPUS="${CPUSET_CPUS-}"
SYNC_GIT="${SYNC_GIT:-}"
PID_FILE="${JETSON_PID_FILE:-/tmp/capture_pipeline.pid}"
LOG_FILE="${JETSON_LOG:-/tmp/capture_pipeline.log}"

# shellcheck disable=SC2086
SSH_OPTS=(${SSH_OPTIONS:-})

host="$(JETSON_SSH="${JETSON_SSH:-}" bash "$HERE/resolve_jetson_ssh.sh")"
echo "JETSON_SSH=${host}" >&2

ssh_h() { ssh "${SSH_OPTS[@]}" "$host" "$@"; }

do_git_sync() {
  ssh_h "set -euo pipefail; \
    dir=${JETSON_DIR}; \
    if [ ! -d \"\$dir/.git\" ]; then \
      echo \"на плате нет git в \$dir\" >&2; \
      echo \"один раз:\" >&2; \
      echo \"  mv \$dir \$dir.rsync.bak\" >&2; \
      echo \"  git clone ${GIT_REPO} \$dir\" >&2; \
      echo \"  git -C \$dir checkout ${GIT_BRANCH}\" >&2; \
      exit 1; \
    fi; \
    cd \"\$dir\"; \
    git fetch origin; \
    git checkout ${GIT_BRANCH}; \
    git pull --ff-only origin ${GIT_BRANCH}"
}

do_stop_native() {
  local cfg="$1"
  # Аргументы bash -s — отдельные слова (без сломанных кавычек make).
  ssh "${SSH_OPTS[@]}" "$host" bash -s -- stop "${JETSON_DIR}" "${cfg}" "${PID_FILE}" "${LOG_FILE}" \
    < "$HERE/jetson_driver_ctl.sh" || true
}

case "$ACTION" in
  git-sync)
    do_git_sync
    ;;
  up|pull)
    if [[ -n "$SYNC_GIT" ]]; then
      do_git_sync
    fi
    cfg="$(
      DOCKER_CONFIG="${DOCKER_CONFIG:-}" \
        JETSON_SSH="$host" \
        JETSON_DIR="$JETSON_DIR" \
        SSH_OPTIONS="${SSH_OPTIONS:-}" \
        bash "$HERE/resolve_docker_config.sh"
    )"
    echo "DOCKER_CONFIG=${cfg}" >&2
    if [[ "$ACTION" == "up" ]]; then
      do_stop_native "$cfg"
    fi
    ssh_h "cd ${JETSON_DIR} && IMAGE_TAG=${IMAGE_TAG} IMAGE=${IMAGE} JETPACK=${JETPACK} PULL_POLICY=${PULL_POLICY} CPUSET_CPUS=${CPUSET_CPUS} bash scripts/jetson_docker.sh ${ACTION} ${cfg}"
    ;;
  down|logs|status)
    ssh_h "cd ${JETSON_DIR} && bash scripts/jetson_docker.sh ${ACTION}"
    ;;
  build)
    ssh_h "cd ${JETSON_DIR} && JETPACK=${JETPACK} bash scripts/jetson_docker.sh build"
    ;;
  *)
    echo "unknown action: ${ACTION}" >&2
    exit 2
    ;;
esac

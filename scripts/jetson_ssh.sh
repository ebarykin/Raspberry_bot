#!/usr/bin/env bash
# Обёртка ssh для Makefile: подставляет JETSON_SSH (или спрашивает).
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck disable=SC2086
host="$(JETSON_SSH="${JETSON_SSH:-}" bash "$HERE/resolve_jetson_ssh.sh")"
# shellcheck disable=SC2086
exec ssh ${SSH_OPTIONS:-} "$host" "$@"

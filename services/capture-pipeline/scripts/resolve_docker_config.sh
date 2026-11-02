#!/usr/bin/env bash
# Выбрать YAML камер для make capture-up / capture-pull.
# Печатает путь в stdout (например configs/jetson-duo-one-s.yaml).
#
# Порядок:
#   1) DOCKER_CONFIG из окружения / make
#   2) CONFIG_HOST из docker/.env на плате (JETSON_SSH / JETSON_DIR)
#   3) интерактивный выбор из configs/*.yaml
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
JETSON_DIR="${JETSON_DIR:-~/capture-pipeline}"
if [[ -z "${JETSON_SSH:-}" ]]; then
  echo "resolve_docker_config: нужен JETSON_SSH" >&2
  exit 1
fi
# shellcheck disable=SC2086
SSH=(ssh ${SSH_OPTIONS:-} "$JETSON_SSH")

normalize() {
  local c="$1"
  c="${c#"${c%%[![:space:]]*}"}"
  c="${c%"${c##*[![:space:]]}"}"
  c="${c#\"}"
  c="${c%\"}"
  c="${c#\'}"
  c="${c%\'}"
  c="${c#./}"
  printf '%s' "$c"
}

validate_local() {
  local c="$1"
  if [[ -f "${ROOT}/${c}" ]]; then
    printf '%s' "$c"
    return 0
  fi
  if [[ -f "$c" ]]; then
    printf '%s' "$c"
    return 0
  fi
  return 1
}

if [[ -n "${DOCKER_CONFIG:-}" ]]; then
  cfg="$(normalize "$DOCKER_CONFIG")"
  if out="$(validate_local "$cfg")"; then
    echo "$out"
    exit 0
  fi
  echo "нет файла конфига: ${cfg}" >&2
  exit 1
fi

remote_line="$("${SSH[@]}" "cd ${JETSON_DIR} && \
  if [ -f docker/.env ]; then \
    grep -E '^[[:space:]]*CONFIG_HOST=' docker/.env | tail -1 || true; \
  fi" 2>/dev/null || true)"

if [[ -n "${remote_line}" ]]; then
  remote_cfg="$(normalize "${remote_line#CONFIG_HOST=}")"
  if [[ -n "$remote_cfg" ]]; then
    if out="$(validate_local "$remote_cfg")"; then
      echo "CONFIG_HOST из ${JETSON_DIR}/docker/.env → ${out}" >&2
      echo "$out"
      exit 0
    fi
    echo "в docker/.env на плате CONFIG_HOST=${remote_cfg}, локально файла нет — выберите вручную" >&2
  fi
else
  echo "на плате нет ${JETSON_DIR}/docker/.env с CONFIG_HOST" >&2
fi

mapfile -t choices < <(
  find "${ROOT}/configs" -maxdepth 1 -type f -name '*.yaml' ! -name 'laptop-emulate.yaml' | sort
)

if [[ ${#choices[@]} -eq 0 ]]; then
  echo "в configs/ нет yaml" >&2
  exit 1
fi

if [[ ! -r /dev/tty ]]; then
  echo "DOCKER_CONFIG не задан и нет TTY — передайте DOCKER_CONFIG=configs/...." >&2
  exit 1
fi

echo "Конфиг камер (DOCKER_CONFIG):" >&2
i=1
for path in "${choices[@]}"; do
  rel="configs/$(basename "$path")"
  note=""
  case "$rel" in
    configs/jetson-duo-one-s.yaml) note="  [стенд Duo]" ;;
    configs/jetson-delta.yaml) note="  [роверы]" ;;
    configs/jetson-quad.yaml) note="  [quad]" ;;
  esac
  printf '  %d) %s%s\n' "$i" "$rel" "$note" >&2
  i=$((i + 1))
done

while true; do
  read -r -p "номер или путь: " answer </dev/tty
  if [[ "$answer" =~ ^[0-9]+$ ]] && ((answer >= 1 && answer <= ${#choices[@]})); then
    cfg="configs/$(basename "${choices[answer - 1]}")"
    echo "$cfg"
    exit 0
  fi
  cfg="$(normalize "$answer")"
  if out="$(validate_local "$cfg")"; then
    echo "$out"
    exit 0
  fi
  echo "нет файла: ${cfg} — ещё раз" >&2
done

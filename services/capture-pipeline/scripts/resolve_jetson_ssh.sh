#!/usr/bin/env bash
# Выбрать SSH-цель для make jetson-*.
# Печатает имя хоста/алиас в stdout (например jetson или nvidia@10.7.12.1).
#
# Порядок: JETSON_SSH из окружения → вопрос (список Host из ~/.ssh/config).
set -euo pipefail

SSH_CONFIG="${SSH_CONFIG:-$HOME/.ssh/config}"
# Один выбор на весь make (общий PPID у всех recipe-процессов).
CACHE="${XDG_RUNTIME_DIR:-/tmp}/capture-pipeline.jetson_ssh.${PPID}"

if [[ -n "${JETSON_SSH:-}" ]]; then
  printf '%s\n' "$JETSON_SSH" >"$CACHE"
  echo "$JETSON_SSH"
  exit 0
fi

if [[ -f "$CACHE" ]]; then
  cat "$CACHE"
  exit 0
fi

mapfile -t hosts < <(
  if [[ -f "$SSH_CONFIG" ]]; then
    awk '
      $1 == "Host" {
        for (i = 2; i <= NF; i++) {
          h = $i
          if (h ~ /[*?]/ || h == "localhost") next
          print h
        }
      }
    ' "$SSH_CONFIG" | awk '!seen[$0]++'
  fi
)

note_for() {
  case "$1" in
    jetson) echo "  [sandbox 10.10.32.250]" ;;
    *) echo "" ;;
  esac
}

hostname_for() {
  local h="$1"
  if [[ -f "$SSH_CONFIG" ]]; then
    ssh -G "$h" 2>/dev/null | awk '/^hostname / {print $2; exit}'
  fi
}

if [[ ! -r /dev/tty ]]; then
  echo "JETSON_SSH не задан и нет TTY — передайте JETSON_SSH=jetson (или user@ip)" >&2
  exit 1
fi

echo "SSH-цель (JETSON_SSH):" >&2
i=1
for h in "${hosts[@]+"${hosts[@]}"}"; do
  hn="$(hostname_for "$h")"
  extra="$(note_for "$h")"
  if [[ -n "$hn" && "$hn" != "$h" ]]; then
    printf '  %d) %s → %s%s\n' "$i" "$h" "$hn" "$extra" >&2
  else
    printf '  %d) %s%s\n' "$i" "$h" "$extra" >&2
  fi
  i=$((i + 1))
done
echo "  или введите user@host / Host-алиас" >&2

while true; do
  read -r -p "номер или JETSON_SSH: " answer </dev/tty
  answer="${answer#"${answer%%[![:space:]]*}"}"
  answer="${answer%"${answer##*[![:space:]]}"}"
  [[ -n "$answer" ]] || continue
  if [[ "$answer" =~ ^[0-9]+$ ]] && ((answer >= 1 && answer <= ${#hosts[@]})); then
    answer="${hosts[answer - 1]}"
  fi
  printf '%s\n' "$answer" >"$CACHE"
  echo "$answer"
  exit 0
done

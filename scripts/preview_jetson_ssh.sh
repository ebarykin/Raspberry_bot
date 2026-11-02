#!/usr/bin/env bash
# Preview камер платы на ноутбуке: SSH -L на RTSP (+ control WS), локальный preview_quad.
#
# На Jetson /usr/bin/ffmpeg часто без network/RTSP (NVIDIA build) — remote preview
# даёт вечный 503. Поэтому decode на ноутбуке.
#
#   make preview-jetson JETSON_SSH=jetson
#   PREVIEW_PORTS="8586 8587" PREVIEW_LABELS="front rear" make preview-jetson
#
# Нужен уже запущенный capture (make capture-up).

set -euo pipefail

HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(cd -- "$HERE/.." && pwd)"

JETSON_SSH="${JETSON_SSH:-jetson}"
HTTP_PORT="${HTTP_PORT:-8765}"
PREVIEW_PORTS="${PREVIEW_PORTS:-8586}"
PREVIEW_LABELS="${PREVIEW_LABELS:-cam0}"
CONTROL_WS_PORT="${CONTROL_WS_PORT:-8591}"
SSH_CONTROL_PATH="${SSH_CONTROL_PATH:-$HOME/.ssh/zed-xonegs-%C}"

if [[ ! "$HTTP_PORT" =~ ^[0-9]+$ ]] || ((HTTP_PORT < 1 || HTTP_PORT > 65535)); then
  echo "некорректный HTTP_PORT: $HTTP_PORT" >&2
  exit 2
fi

# Makefile: пробелы; argparse preview_quad: CSV.
PREVIEW_PORTS_CSV="${PREVIEW_PORTS// /,}"
PREVIEW_LABELS_CSV="${PREVIEW_LABELS// /,}"
IFS=',' read -r -a PORT_ARR <<<"$PREVIEW_PORTS_CSV"

SSH_OPTIONS=(
  -o StrictHostKeyChecking=accept-new
  -o ControlMaster=auto
  -o ControlPersist=10m
  -o "ControlPath=$SSH_CONTROL_PATH"
  -o ExitOnForwardFailure=yes
)

# Снести старый remote preview (если остался от прежней схемы).
ssh "${SSH_OPTIONS[@]}" "$JETSON_SSH" \
  'pid=$(cat /tmp/zed_preview_quad.pid 2>/dev/null || true);
   if [[ -n "$pid" ]] && grep -q preview_quad.py /proc/$pid/cmdline 2>/dev/null; then
     kill "$pid" 2>/dev/null || true;
   fi
   rm -f /tmp/zed_preview_quad.pid;
   pkill -f "scripts/preview_quad.py" 2>/dev/null || true' \
  >/dev/null 2>&1 || true

TUNNEL_ARGS=()
for p in "${PORT_ARR[@]}"; do
  [[ "$p" =~ ^[0-9]+$ ]] || { echo "плохой порт: $p" >&2; exit 2; }
  TUNNEL_ARGS+=(-L "${p}:127.0.0.1:${p}")
done
TUNNEL_ARGS+=(-L "${CONTROL_WS_PORT}:127.0.0.1:${CONTROL_WS_PORT}")

echo "==> SSH-туннель RTSP ${PREVIEW_PORTS_CSV} + :${CONTROL_WS_PORT} → ${JETSON_SSH}"
ssh "${SSH_OPTIONS[@]}" -N "${TUNNEL_ARGS[@]}" "$JETSON_SSH" &
TUNNEL_PID=$!

cleanup() {
  if [[ -n "${PREVIEW_PID:-}" ]]; then
    kill -TERM "$PREVIEW_PID" 2>/dev/null || true
    wait "$PREVIEW_PID" 2>/dev/null || true
  fi
  if [[ -n "${TUNNEL_PID:-}" ]]; then
    kill -TERM "$TUNNEL_PID" 2>/dev/null || true
    wait "$TUNNEL_PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM EXIT

# Дождаться локальных forward'ов.
for _ in $(seq 1 40); do
  ok=1
  for p in "${PORT_ARR[@]}"; do
    ss -ltnH "sport = :$p" 2>/dev/null | grep -q . || ok=0
  done
  ((ok)) && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || {
    echo "SSH-туннель упал" >&2
    exit 1
  }
  sleep 0.1
done

echo "==> локальный preview → http://127.0.0.1:${HTTP_PORT}/"
cd -- "$ROOT"
python3 scripts/preview_quad.py \
  --host=127.0.0.1 \
  --ports="$PREVIEW_PORTS_CSV" \
  --labels="$PREVIEW_LABELS_CSV" \
  --rtsp-transport=tcp \
  --bind=127.0.0.1 \
  --http-port="$HTTP_PORT" \
  --control-ws="ws://127.0.0.1:${CONTROL_WS_PORT}" &
PREVIEW_PID=$!

if command -v xdg-open >/dev/null 2>&1; then
  (sleep 1.2 && xdg-open "http://127.0.0.1:${HTTP_PORT}/") &
elif command -v open >/dev/null 2>&1; then
  (sleep 1.2 && open "http://127.0.0.1:${HTTP_PORT}/") &
fi

wait "$PREVIEW_PID"
ec=$?
trap - EXIT
cleanup
exit "$ec"

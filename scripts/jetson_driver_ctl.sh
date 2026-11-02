#!/usr/bin/env bash
# Безопасное управление ручным экземпляром capture-pipeline на Jetson.

set -euo pipefail

ACTION="${1:-}"
DRIVER_DIR="${2:-}"
CONFIG="${3:-configs/jetson-quad.yaml}"
PID_FILE="${4:-/tmp/capture_pipeline.pid}"
LOG_FILE="${5:-/tmp/capture_pipeline.log}"

if [[ -z "$ACTION" || -z "$DRIVER_DIR" ]]; then
  echo "usage: $0 <run-fg|run-bg|stop|status> <driver-dir> [config] [pid-file] [log-file]" >&2
  exit 2
fi

# В аргументах SSH тильда не раскрывается, поэтому разворачиваем её явно.
if [[ "$DRIVER_DIR" == "~/"* ]]; then
  DRIVER_DIR="$HOME/${DRIVER_DIR:2}"
fi
DRIVER_DIR="$(readlink -f -- "$DRIVER_DIR")"

# Проверяем не только аргументы, но и рабочий каталог. Так процессы другого проекта
# или другого сервиса с похожей командой никогда не попадут под остановку.
is_driver_pid() {
  local pid="$1" cwd executable
  local -a argv=()
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cwd="$(readlink -f -- "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$DRIVER_DIR" ]] || return 1
  mapfile -d "" -t argv <"/proc/$pid/cmdline" || true
  [[ "${#argv[@]}" -ge 4 ]] || return 1
  executable="${argv[0]##*/}"
  [[ "$executable" == "python3" || "$executable" == python3.* ]] || return 1
  [[ "${argv[1]}" == "main.py" && "${argv[2]}" == "--config" ]]
}

driver_pids() {
  local proc_cmdline pid
  for proc_cmdline in /proc/[0-9]*/cmdline; do
    pid="${proc_cmdline#/proc/}"
    pid="${pid%/cmdline}"
    is_driver_pid "$pid" && printf '%s\n' "$pid"
  done
  return 0
}

# Сначала даём Python и GStreamer завершиться штатно, затем добиваем только
# подтверждённые процессы этого каталога. Повторная проверка защищает от смены PID.
stop_driver() {
  local pids alive pid attempt
  pids="$(driver_pids)"
  if [[ -z "$pids" ]]; then
    echo "совпадающие процессы драйвера не найдены"
    rm -f -- "$PID_FILE"
    return
  fi
  for pid in $pids; do
    is_driver_pid "$pid" && kill "$pid" 2>/dev/null || true
  done
  for attempt in {1..30}; do
    alive=""
    for pid in $pids; do
      is_driver_pid "$pid" && alive+=" $pid"
    done
    [[ -z "$alive" ]] && break
    sleep 0.2
  done
  for pid in ${alive:-}; do
    is_driver_pid "$pid" && kill -KILL "$pid" 2>/dev/null || true
  done
  rm -f -- "$PID_FILE"
  echo "остановлены:$pids"
}

# Список берётся из фактически выбранного YAML, поэтому один скрипт подходит
# для quad, duo и конфигураций с отключёнными камерами/control WS.
required_ports() {
  (
    cd -- "$DRIVER_DIR"
    python3 -c \
      'from pathlib import Path
from capture_pipeline.config import load_config
import sys
c = load_config(Path(sys.argv[1]))
ports = [str(x.rtsp_port) for x in c.cameras if x.enabled]
if c.control_ws.enabled:
    ports.append(str(c.control_ws.port))
print(*ports)' \
      "$CONFIG"
  )
}

# rbfimagesink в colcon overlay: без source нет GST_PLUGIN_PATH / LD_LIBRARY_PATH.
# setup.bash трогает незаданные переменные — временно снимаем `set -u`.
source_ros_if_present() {
  local setup encoding
  for setup in /opt/ros/jazzy/setup.bash "$HOME/ros2_ws/install/setup.bash"; do
    [[ -f "$setup" ]] || continue
    set +u
    # shellcheck disable=SC1090
    source "$setup"
    set -u
  done
  # LARGE_DATA / Cyclone WHC нужны сырому Image (~6.6MiB @ 1920×1200).
  # JPEG ходит и на дефолтах. UDP MaxMessageSize у Cyclone остаётся ~64KiB.
  encoding="$(
    python3 -c '
from pathlib import Path
import sys
from capture_pipeline.config import load_config
print(load_config(Path(sys.argv[1])).nav.encoding)
' "$CONFIG" 2>/dev/null || echo compressed
  )"
  if [[ -z "${RMW_IMPLEMENTATION:-}" ]]; then
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  fi
  if [[ "${RMW_IMPLEMENTATION}" == *cyclonedds* ]]; then
    if [[ -z "${CYCLONEDDS_URI+x}" ]]; then
      export CYCLONEDDS_URI="file://${DRIVER_DIR}/chart/files/dds/cyclonedds.xml"
    fi
  elif [[ "$encoding" == "raw" && -z "${FASTDDS_BUILTIN_TRANSPORTS+x}" ]]; then
    # 1920×1200 bgr8 is 6.91MB; 4MB max_msg_size truncates the frame.
    export FASTDDS_BUILTIN_TRANSPORTS='LARGE_DATA?max_msg_size=8MB&sockets_size=16MB&non_blocking=true'
  fi
  if [[ "$encoding" == "raw" && -z "${ROS_DISABLE_LOANED_MESSAGES+x}" ]]; then
    export ROS_DISABLE_LOANED_MESSAGES=1
  fi
}

# Слушающий сокет может исчезнуть чуть позже процесса из-за завершения GStreamer.
# Ждём до 10 секунд; чужой слушатель не убиваем, а показываем диагностику.
wait_for_free_ports() {
  local ports busy_ports port attempt
  ports="$(required_ports)"
  for attempt in {1..50}; do
    busy_ports=""
    for port in $ports; do
      if ss -ltnH "sport = :$port" | grep -q .; then
        busy_ports+=" $port"
      fi
    done
    [[ -z "$busy_ports" ]] && return
    sleep 0.2
  done
  echo "запуск невозможен: порты заняты более 10 секунд:$busy_ports" >&2
  for port in $busy_ports; do
    ss -ltnp "sport = :$port" || true
  done
  return 1
}

run_background() {
  local pid
  stop_driver
  wait_for_free_ports
  if ! : >"$PID_FILE"; then
    echo "невозможно записать PID-файл: $PID_FILE" >&2
    return 1
  fi
  cd -- "$DRIVER_DIR"
  source_ros_if_present
  nohup python3 main.py --config "$CONFIG" >"$LOG_FILE" 2>&1 &
  pid=$!
  if ! printf '%s\n' "$pid" >"$PID_FILE"; then
    is_driver_pid "$pid" && kill "$pid" 2>/dev/null || true
    echo "не удалось сохранить PID; запущенный процесс остановлен" >&2
    return 1
  fi
  sleep 1
  if is_driver_pid "$pid"; then
    echo "запущен PID:$pid лог:$LOG_FILE"
    return
  fi
  echo "драйвер завершился при старте; последние строки лога:" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  return 1
}

run_foreground() {
  stop_driver
  wait_for_free_ports
  cd -- "$DRIVER_DIR"
  source_ros_if_present
  exec python3 main.py --config "$CONFIG"
}

show_status() {
  local pid pids pids_csv
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if is_driver_pid "$pid"; then
      echo "PID-файл подтверждён: $pid"
    else
      echo "PID-файл устарел или указывает не на этот драйвер: $pid"
    fi
  else
    echo "PID-файл отсутствует"
  fi
  pids="$(driver_pids)"
  if [[ -n "$pids" ]]; then
    pids_csv="${pids//$'\n'/,}"
    ps -p "$pids_csv" -o pid,etime,cmd || true
  fi
  ss -ltnp | grep -E ":8586|:8587|:8588|:8589|:8591" || true
}

case "$ACTION" in
  run-fg) run_foreground ;;
  run-bg) run_background ;;
  stop) stop_driver ;;
  status) show_status ;;
  *)
    echo "неизвестное действие: $ACTION" >&2
    exit 2
    ;;
esac

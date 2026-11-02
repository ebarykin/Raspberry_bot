#!/usr/bin/env bash
# Дождаться первого кадра NAV и замерить Hz в том же ROS-узле
# (без warning discovery и без зависания ros2 topic hz на Ctrl+C).
set -euo pipefail

DRIVER_DIR="${1:-.}"
CONFIG="${2:-configs/jetson-duo-one-s.yaml}"
SECS="${3:-8}"

if [[ "$DRIVER_DIR" == "~/"* ]]; then
  DRIVER_DIR="$HOME/${DRIVER_DIR:2}"
fi
cd -- "$DRIVER_DIR"
export PYTHONPATH="$DRIVER_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [[ -f /opt/ros/jazzy/setup.bash ]]; then
  set +u
  # shellcheck disable=SC1091
  source /opt/ros/jazzy/setup.bash
  set -u
fi
if [[ -f "$HOME/ros2_ws/install/setup.bash" ]]; then
  set +u
  # shellcheck disable=SC1091
  source "$HOME/ros2_ws/install/setup.bash"
  set -u
fi
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"

# Совпадающий FastDDS/domain с уже запущенным драйвером (LARGE_DATA только если он его выставил).
if pid="$(pgrep -f '[p]ython3 main.py --config' | head -n1)" && [[ -n "$pid" && -r "/proc/$pid/environ" ]]; then
  while IFS= read -r -d '' kv; do
    case "${kv%%=*}" in
      ROS_DOMAIN_ID|FASTDDS_BUILTIN_TRANSPORTS|ROS_DISABLE_LOANED_MESSAGES|RMW_IMPLEMENTATION|ROS_LOCALHOST_ONLY|ROS_AUTOMATIC_DISCOVERY_RANGE)
        export "$kv"
        ;;
    esac
  done < "/proc/$pid/environ"
fi

topic="$(
  python3 -c '
from pathlib import Path
import sys
import yaml

cfg = yaml.safe_load(Path(sys.argv[1]).read_text())
encoding = ((cfg.get("nav") or {}).get("encoding") or "compressed").strip().lower()
cam = next(
    (
        c
        for c in (cfg.get("cameras") or [])
        if c.get("enabled") and c.get("nav_enabled") and c.get("ros_name")
    ),
    None,
)
if cam is None:
    raise SystemExit("no enabled camera with nav_enabled + ros_name")
base = "/sensing/{}/image_raw".format(cam["ros_name"])
print(base + "/compressed" if encoding == "compressed" else base)
' "$CONFIG"
)"

# +20s: ожидание кадра. -k 2 если spin завис.
timeout --foreground -k 2 "$((SECS + 20))" python3 - "$topic" "$SECS" <<'PY'
import statistics
import sys
import time

import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage, Image

topic = sys.argv[1]
secs = float(sys.argv[2])
Msg = CompressedImage if topic.endswith("/compressed") else Image

rclpy.init()
node = rclpy.create_node("nav_hz")
stamps = []

def _cb(_msg):
    stamps.append(time.monotonic())

# BEST_EFFORT как у типичного image-subscriber; не копировать QoS publisher
# (depth 1 RELIABLE с плагина даёт несовместимую подписку).
node.create_subscription(Msg, topic, _cb, qos_profile_sensor_data)
print(f"waiting for first message on {topic}", flush=True)
deadline = time.monotonic() + 15.0
while rclpy.ok() and not stamps and time.monotonic() < deadline:
    rclpy.spin_once(node, timeout_sec=0.2)
if not stamps:
    pubs = node.get_publishers_info_by_topic(topic)
    extra = "publisher seen" if pubs else "no publisher yet"
    print(f"no message on {topic} within 15s ({extra}; is the driver running?)", file=sys.stderr)
    node.destroy_node()
    rclpy.shutdown()
    sys.exit(1)

print(f"publisher ok, measuring {secs:.0f}s", flush=True)
stamps.clear()
end = time.monotonic() + secs
while rclpy.ok() and time.monotonic() < end:
    rclpy.spin_once(node, timeout_sec=0.05)
node.destroy_node()
rclpy.shutdown()

if len(stamps) < 2:
    print(f"got {len(stamps)} message(s), not enough for Hz", file=sys.stderr)
    sys.exit(1)
window = stamps[-1] - stamps[0]
avg = (len(stamps) - 1) / window
dts = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
print(f"average rate: {avg:.3f} Hz")
print(f"min: {1.0 / max(dts):.3f} Hz  max: {1.0 / min(dts):.3f} Hz  stddev: {statistics.pstdev(1.0 / d for d in dts):.3f}")
print(f"window: {window:.3f} s  samples: {len(stamps)}")
PY

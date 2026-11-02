#!/bin/bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /ws/install/setup.bash
exec ros2 launch robot_gateway gateway_rosbridge.launch.py \
  "capabilities_file:=${CAPABILITIES_FILE}"

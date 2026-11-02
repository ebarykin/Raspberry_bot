#!/bin/bash
# Общая установка образов JetPack 6.2 и 7.2.
# Платформу задаёт Dockerfile (база, ROS_DISTRO, ROS_APT_SUITE), не этот скрипт.
set -euo pipefail

phase="${1:-}"
export DEBIAN_FRONTEND=noninteractive

check_ros_ubuntu() {
  case "${ROS_DISTRO:?}:${ROS_APT_SUITE:?}" in
    humble:jammy|jazzy:noble) ;;
    *)
      echo "capture-pipeline: пара ROS_DISTRO=${ROS_DISTRO} ROS_APT_SUITE=${ROS_APT_SUITE} не собирается (нужно humble/jammy или jazzy/noble)" >&2
      exit 1
      ;;
  esac
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}")"
  if [[ "${codename}" != "${ROS_APT_SUITE}" ]]; then
    echo "capture-pipeline: Ubuntu ${codename:-unknown}, ROS_APT_SUITE=${ROS_APT_SUITE}. Для роверов — Dockerfile.jp62, для робособак — Dockerfile.jp72." >&2
    exit 1
  fi
}

phase_apt() {
  apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    build-essential \
    cmake \
    python3 \
    python3-pip \
    kmod \
    v4l-utils \
    util-linux \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-rtsp-server-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-rtsp \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-bad1.0-dev \
    && rm -rf /var/lib/apt/lists/*
  local -a flags=()
  if python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages; then
    flags+=(--break-system-packages)
  fi
  python3 -m pip install --no-cache-dir "${flags[@]}" 'numpy>=1.23,<2' PyYAML websockets
}

phase_ros() {
  check_ros_ubuntu
  curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
    -o /usr/share/keyrings/ros-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu ${ROS_APT_SUITE} main" \
    > /etc/apt/sources.list.d/ros2.list
  apt-get update && apt-get install -y --no-install-recommends \
    "ros-${ROS_DISTRO}-ros-base" \
    "ros-${ROS_DISTRO}-rmw-cyclonedds-cpp" \
    "ros-${ROS_DISTRO}-ros2cli" \
    "ros-${ROS_DISTRO}-ros2topic" \
    "ros-${ROS_DISTRO}-camera-calibration-parsers" \
    "ros-${ROS_DISTRO}-ament-cmake" \
    "ros-${ROS_DISTRO}-ament-index-cpp" \
    python3-colcon-common-extensions \
    && rm -rf /var/lib/apt/lists/*
}

phase_babel() {
  : "${ROS_DISTRO:?}"
  cd /opt/nav_ws
  # setup.sh трогает незаданные переменные (AMENT_TRACE_SETUP_FILES).
  set +u
  # shellcheck disable=SC1090
  . "/opt/ros/${ROS_DISTRO}/setup.sh"
  set -u
  colcon build --packages-select gstreamer_ros_babel_fish \
    --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
  rm -rf build log src
  apt-get purge -y \
    build-essential \
    cmake \
    python3-colcon-common-extensions \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-bad1.0-dev
  apt-get update && apt-get install -y --no-install-recommends \
    "ros-${ROS_DISTRO}-ros-base" \
    "ros-${ROS_DISTRO}-rmw-cyclonedds-cpp" \
    "ros-${ROS_DISTRO}-ros2cli" \
    "ros-${ROS_DISTRO}-ros2topic" \
    "ros-${ROS_DISTRO}-camera-calibration-parsers" \
    "ros-${ROS_DISTRO}-ament-index-cpp" \
    && rm -rf /var/lib/apt/lists/*
}

phase_shell() {
  : "${ROS_DISTRO:?}"
  printf '%s\n' \
    "[ -f /opt/ros/${ROS_DISTRO}/setup.bash ] && . /opt/ros/${ROS_DISTRO}/setup.bash" \
    '[ -f /opt/nav_ws/install/setup.bash ] && . /opt/nav_ws/install/setup.bash' \
    'export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"' \
    'export CYCLONEDDS_URI="${CYCLONEDDS_URI:-file:///rmw/cyclonedds.xml}"' \
    'export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"' \
    > /root/.bashrc
}

case "${phase}" in
  apt) phase_apt ;;
  ros) phase_ros ;;
  babel) phase_babel ;;
  shell) phase_shell ;;
  *)
    echo "usage: install.sh {apt|ros|babel|shell}" >&2
    exit 2
    ;;
esac

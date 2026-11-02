#!/bin/bash
# Не CMD в Dockerfile: ROS (Humble на JP 6.2, Jazzy на JP 7.2) и colcon overlay
# надо source'ить (десятки переменных), это нельзя заменить одним ENV.
# Дистрибутив берётся из ENV ROS_DISTRO, его записывает Dockerfile.
#
# Helm / jetson_docker.sh часто задают GST_PLUGIN_PATH = каталог NVIDIA с хоста.
# Он перекрывает плагины образа. rbfimagesink лежит в /opt/nav_ws — его
# путь обязан быть первым, иначе NAV уйдёт в fakesink или упадёт на старте.
set -e
set +u
# setup.bash трогает незаданные переменные — set -u только после source.
# shellcheck disable=SC1090,SC1091
. "/opt/ros/${ROS_DISTRO:-humble}/setup.bash"
# shellcheck disable=SC1091
. /opt/nav_ws/install/setup.bash
set -u

BF_GST="/opt/nav_ws/install/gstreamer_ros_babel_fish/lib/gstreamer-1.0-plugins"
case ":${GST_PLUGIN_PATH:-}:" in
  *":${BF_GST}:"*) ;;
  *) export GST_PLUGIN_PATH="${BF_GST}${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}" ;;
esac

# tegra с хоста: без этого nvarguscamerasrc / nvv4l2h264enc не линкуются.
TEGRA_LD="/usr/lib/aarch64-linux-gnu/tegra:/usr/lib/aarch64-linux-gnu/tegra-egl:/usr/lib/aarch64-linux-gnu/nvidia"
case ":${LD_LIBRARY_PATH:-}:" in
  *":${TEGRA_LD}:"*) ;;
  *) export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}${TEGRA_LD}" ;;
esac
LIBDIR="/usr/lib/aarch64-linux-gnu"
for lib in libgstnvexifmeta.so libgstnvivameta.so libgstnvegl-1.0.so.0; do
  dest="${LIBDIR}/${lib}"
  [[ -e "${dest}" ]] && continue
  for src in "${LIBDIR}/tegra/${lib}" "${LIBDIR}/nvidia/${lib}"; do
    if [[ -e "${src}" ]]; then
      ln -sfn "${src}" "${dest}"
      break
    fi
  done
done

export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
export PYTHONUNBUFFERED=1
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export CYCLONEDDS_URI="${CYCLONEDDS_URI:-file:///rmw/cyclonedds.xml}"

# privileged + host net: sysctl здесь трогает хост, до первого PLAYING.
if command -v sysctl >/dev/null 2>&1; then
  sysctl -w \
    net.core.rmem_max=67108864 net.core.rmem_default=67108864 \
    net.core.wmem_max=33554432 net.core.wmem_default=33554432 \
    >/dev/null 2>&1 \
    || echo "capture-pipeline: не поднял rmem=64MB/wmem=32MB — CycloneDDS не стартанёт" >&2
fi

# Чёрный список scanner после неудачного dlopen (нет ROS .so) иначе
# rbfimagesink не появится даже когда библиотеки уже на месте.
rm -rf /root/.cache/gstreamer-1.0 2>/dev/null || true

# exec — SIGTERM от kubelet/docker приходит в python, не в обёртку.
exec python3 -u /app/main.py "$@"

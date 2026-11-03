# Build from the repository root:
#   docker build -f services/robot-gateway/Dockerfile -t teleop-robot-gateway .
FROM ros:jazzy-ros-base

ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
    DEBIAN_FRONTEND=noninteractive \
    CAPABILITIES_FILE=/config/capabilities.yaml

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ros-jazzy-rmw-cyclonedds-cpp \
        ros-jazzy-rosbridge-server \
        python3-colcon-common-extensions \
        python3-yaml \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ws
COPY ros2_interfaces/robot_interfaces src/robot_interfaces
COPY services/robot-gateway/robot_gateway src/robot_gateway
COPY config/capabilities.yaml /config/capabilities.yaml
COPY services/robot-gateway/entrypoint.sh /entrypoint.sh
COPY services/robot-gateway/robot_config_ws.py /robot_config_ws.py

RUN . /opt/ros/jazzy/setup.sh \
    && colcon build --symlink-install \
    && chmod +x /entrypoint.sh

EXPOSE 9090
ENTRYPOINT ["/entrypoint.sh"]

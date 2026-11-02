"""Запускает штатный rosbridge и узел GetCapabilities."""

from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def default_capabilities_file() -> str:
    return str(
        Path(__file__).resolve().parents[4] / "config" / "capabilities.yaml"
    )


def generate_launch_description() -> LaunchDescription:
    # Закладка на аргумент launch. YAML потом читает узел, не эта строка.
    capabilities_file = LaunchConfiguration("capabilities_file")
    
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "capabilities_file",
                default_value=default_capabilities_file(),
            ),
            Node(
                package="rosbridge_server",
                executable="rosbridge_websocket",
                name="rosbridge_websocket",
                output="screen",
            ),
            Node(
                package="robot_gateway",
                executable="get_capabilities",
                name="get_capabilities",
                output="screen",
                parameters=[{"capabilities_file": capabilities_file}],
            ),
        ]
    )

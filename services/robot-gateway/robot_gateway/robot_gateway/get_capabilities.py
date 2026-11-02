"""Отдаёт envelope robot-config через /robot/get_capabilities."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REQUIRED_EVENT = "robot-config"
REQUIRED_PAYLOAD_KEYS = (
    "schema_version",
    "units",
    "limits",
    "start_velocity",
    "commands",
)


def load_capabilities_envelope(path: Path) -> dict:
    # Загружаем данные из файла capabilities.yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Проверяем, что данные являются словарём
    if not isinstance(data, dict):
        raise TypeError("capabilities file must contain a mapping")
    # Проверяем, что событие является robot-config
    if data.get("event") != REQUIRED_EVENT:
        raise ValueError("capabilities event must be robot-config")
    # Получаем payload
    payload = data.get("payload")
    # Проверяем, что payload является словарём
    if not isinstance(payload, dict):
        raise TypeError("capabilities payload must be a mapping")
    # Проверяем, что все обязательные ключи присутствуют в payload
    missing = [key for key in REQUIRED_PAYLOAD_KEYS if key not in payload]
    # Если какие-то ключи отсутствуют, выбрасываем исключение
    if missing:
        raise ValueError(f"capabilities payload missing keys: {', '.join(missing)}")
    return data


def envelope_to_json(envelope: dict) -> str:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def main() -> None:
    import rclpy
    from rclpy.node import Node
    from robot_interfaces.srv import GetCapabilities

    class GetCapabilitiesNode(Node):
        def __init__(self) -> None:
            # Инициализируем узел - стандартная процедура
            super().__init__("get_capabilities")
            self.declare_parameter("capabilities_file", "")
            path = Path(
                self.get_parameter("capabilities_file")
                .get_parameter_value()
                .string_value
            )
            if not path.is_file():
                raise FileNotFoundError(f"capabilities file not found: {path}")
            self._json = envelope_to_json(load_capabilities_envelope(path))
            
            self.create_service(
                GetCapabilities,
                "/robot/get_capabilities",
                self._handle,
            )
            self.get_logger().info(
                f"Serving /robot/get_capabilities from {path}"
            )

        # Обработчик запроса
        def _handle(
            self,
            _request: GetCapabilities.Request,
            response: GetCapabilities.Response,
        ) -> GetCapabilities.Response:
            response.json = self._json
            return response

    # Инициализируем ROS
    rclpy.init()
    node = GetCapabilitiesNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()

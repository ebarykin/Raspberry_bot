# ros2_interfaces

Пакет `robot_interfaces` (`ament_cmake` и `rosidl`).

В пакете только собственные сообщения и сервисы. Стандартные типы, включая `geometry_msgs`, не копируются.

`GetCapabilities.srv` принадлежит узлу `get_capabilities` в `robot-gateway`. Отдельный контейнер `robot-info` не заводится.

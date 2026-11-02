# ROS 2 в этом проекте

Короткий конспект решений, а не учебник. Источники: [REP-2000](https://reps.openrobotics.org/rep-2000/), [установка на Raspberry Pi](https://docs.ros.org/en/jazzy/How-To-Guides/Installing-on-Raspberry-Pi.html), [Improved Dynamic Discovery](https://docs.ros.org/en/jazzy/Tutorials/Advanced/Improved-Dynamic-Discovery.html).

## Дистрибутив

Берём ROS 2 Jazzy Jalisco (май 2024 — май 2029). Узлы пишутся на Python через `rclpy`. Пакеты узлов — `ament_python`: `package.xml`, `setup.py`, `setup.cfg`, `resource/<имя_пакета>`. Общие типы — пакет `robot_interfaces` на `ament_cmake` и `rosidl` в каталоге `ros2_interfaces`. Сборка — `colcon` внутри контейнера: исходники копируются в workspace образа.

Стандартные сообщения в репозиторий не копируются. Свой мост вместо `rosbridge_suite` не пишется.

## Где он стоит

Хост робота — Raspberry Pi 5, 8 ГБ, Debian 13 ARM64. Tier 1 для Jazzy — Ubuntu 24.04 arm64. На Debian официальных бинарных пакетов нет, поэтому ROS работает в контейнере `ros:jazzy` на базе Ubuntu 24.04. Сторонний apt для Debian 13 в проект не входит.

## Discovery

Узлы на одной машине ограничиваются так:

```bash
ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
```

`ROS_LOCALHOST_ONLY` в Jazzy снят и молча игнорируется. Эти переменные действуют для Cyclone DDS и не действуют для `rmw_zenoh`. Zenoh в проекте не используется.

Транспорт графа — Cyclone DDS, реализация `rmw_cyclonedds_cpp`. В официальном образе Jazzy по умолчанию Fast DDS, поэтому контейнер задаёт `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp` и ставит пакет `ros-jazzy-rmw-cyclonedds-cpp`. Смена RMW требует нового ADR, см. [0007](decisions/0007-use-cyclone-dds.md).

`network_mode: host` даёт контейнерам общую сеть хоста. Общую память Cyclone DDS это само по себе не включает: её нужно отдельно проверить в конфигурации Cyclone, когда появится Compose.

Домен задаётся `ROS_DOMAIN_ID`. Значение по умолчанию выберется вместе с конфигурацией, не в коде узлов.

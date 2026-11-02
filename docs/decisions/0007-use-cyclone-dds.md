# 0007. Cyclone DDS

Статус: Accepted

## Контекст

Узлы ROS 2 на роботе должны использовать один RMW, иначе они не видят друг друга. Официальный образ Jazzy по умолчанию поднимает Fast DDS (`rmw_fastrtps_cpp`). Наружу, к оператору, DDS не отдаётся: для этого есть `robot-gateway`.

## Решение

Транспорт графа — Cyclone DDS, пакет `rmw_cyclonedds_cpp`. Контейнер задаёт `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`. В коде узлов RMW не выбирается. Discovery остаётся локальным: `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`.

## Последствия

Образы с ROS 2 ставят `ros-jazzy-rmw-cyclonedds-cpp`. `rmw_zenoh` не используется. Другой RMW возможен только новым ADR.

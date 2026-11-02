# teleop-robot

Телеуправляемый робот на шасси Remo Hobby и Raspberry Pi 5 (8 ГБ, Debian 13 ARM64).
Репозиторий держит сервисы ROS 2, шлюз и документацию. `capture-pipeline` войдёт git subtree, `webrtc-client-go` — Git submodule.

Собраны пакеты `robot_gateway` и `robot_interfaces` и образ шлюза. Корневой Compose и остальные узлы ждут URL в ADR 0006.

## Сервисы

| Сервис | Роль |
| --- | --- |
| `services/webrtc-client-go` | Git submodule на Go: WebRTC и клиент rosbridge по WebSocket. Без библиотек ROS 2. |
| `services/robot-gateway` | Штатный `rosbridge_server` и `/robot/get_capabilities`. Есть Dockerfile. |
| `services/motion-supervisor` | Узел ROS 2: приём и проверка команд телеуправления. |
| `services/chassis-controller` | Тяга IBT-2 на PCA9685; руль SG90 на GPIO Pi, 50 Гц. |
| `services/capture-pipeline` | Git subtree: захват камеры и RTSP. Вне ROS 2. |

Контракты между сервисами — в [docs/interfaces.md](docs/interfaces.md). Общей библиотеки у WebRTC и ROS 2 нет.

## Документы

- [docs/architecture.md](docs/architecture.md) — границы сервисов
- [docs/ros2.md](docs/ros2.md) — как в этом проекте используется ROS 2 Jazzy
- [docs/decisions/](docs/decisions/) — причины принятых решений

Правила агента лежат в `.cursor/rules/`. Архитектурное правило действует всегда, остальные подключаются по типу файлов.

## Прежде чем писать код

Остальные пакеты и Compose ждут два URL в [ADR 0006](docs/decisions/0006-defer-platform-choices.md): `capture-pipeline` (subtree) и `webrtc-client-go` (submodule). Руль уже выбран: SG90 на GPIO, 50 Гц.

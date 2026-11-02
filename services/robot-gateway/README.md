# robot-gateway

Вход оператора в граф ROS 2. Launch поднимает штатный `rosbridge_server` и узел `get_capabilities`.

Клиент после открытия WebSocket один раз вызывает `/robot/get_capabilities`. Ответ — JSON envelope из `config/capabilities.yaml`. Свой мост не пишем, см. ADR 0003 и ADR 0008.

Пакет ROS 2 внутри каталога — `robot_gateway`.

## Образ

Сборка из корня репозитория: в образ входят `robot_interfaces` и YAML.

```bash
docker build -f services/robot-gateway/Dockerfile -t teleop-robot-gateway .
docker run --rm --net=host teleop-robot-gateway
```

Порт rosbridge — 9090. Подставить другой YAML:

```bash
docker run --rm --net=host \
  -e CAPABILITIES_FILE=/config/capabilities.yaml \
  -v "$PWD/config/capabilities.yaml":/config/capabilities.yaml \
  teleop-robot-gateway
```

Проверка из второго контейнера или с хоста, где есть ROS 2:

```bash
ros2 service call /robot/get_capabilities robot_interfaces/srv/GetCapabilities
```

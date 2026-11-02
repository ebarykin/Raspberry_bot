# 0008. Capabilities живут в robot-gateway

Статус: Accepted

## Контекст

Клиенту WebRTC при первом подключении нужен набор того, что умеет робот. Отдельный контейнер `robot-info` для этого не заводился. Держать ответ в `chassis-controller` рано: руль не описан, пакет контроллера ещё не создан. Клиент не читает YAML с диска и не линкует ROS 2.

## Решение

Владелец — узел `get_capabilities` в сервисе `robot-gateway`. Источник правды — корневой `config/capabilities.yaml`. Сервис ROS 2 `/robot/get_capabilities` имеет пустой запрос и одно поле ответа `string json`. Строка — envelope `robot-config`, без разворота в поля ROS.

Транспорт вызова — штатный `rosbridge_server` в том же launch. Свой мост не пишется.

Пакеты `robot_gateway` и `robot_interfaces` появляются до руля и URL. Это исключение из ADR 0006 только для этих двух пакетов.

## Последствия

Смена состава возможностей — правка YAML и, если меняется envelope, этого ADR и `docs/interfaces.md`. `limits`, `start_velocity` и `commands` заполняются, когда утвердят топики и пределы, а не заранее.

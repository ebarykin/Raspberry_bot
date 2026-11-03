# Контракты

Документ фиксирует границы. Имена топиков и тип команды (тяга плюс угол SG90) ещё не утверждены и не считаются частью контракта.

Общего кода между `webrtc-client-go`, `capture-pipeline` и узлами ROS 2 нет. Сервисы встречаются только в точках ниже.

## WebSocket, rosbridge

`webrtc-client-go` подключается к `robot-gateway` по WebSocket и говорит протоколом rosbridge. Клиент не линкует библиотеки ROS 2 и не открывает Cyclone DDS.

Целевой транспорт — rosbridge: сразу после открытия сокета клиент один раз вызывает `/robot/get_capabilities`. Дальше идут `advertise` и поток `publish`. Повторять вызов сервиса на каждой команде не нужно.

Текущий `webrtc-client-go` ещё не вызывает сервис. Он ждёт envelope `robot-config` с `ws://127.0.0.1:4000/`. Этот сокет поднимает `robot-gateway` из того же `capabilities.yaml`.

```json
{"op":"call_service","service":"/robot/get_capabilities","args":{}}
```

Ответ rosbridge несёт поле `json` сервиса `GetCapabilities.srv` — строку с envelope `robot-config`. Источник правды — [config/capabilities.yaml](../config/capabilities.yaml).

## RTSP

`capture-pipeline` захватывает OV5647 через `libcamerasrc`, кодирует `x264enc` и сам держит `GstRTSPServer`. Endpoint на роботе: `rtsp://127.0.0.1:8554/live`. `webrtc-client-go` читает этот URL. ROS 2 в этом контуре нет.

## Граф ROS 2

Граф живёт внутри робота: `robot-gateway`, `motion-supervisor`, `chassis-controller`. Cyclone DDS в сеть оператора не выходит.

Тип команды движения не утверждён. Тяга — знак и скважность моста IBT-2. Руль — угол SG90. `geometry_msgs/Twist` заранее не выбирается. Стандартный тип, когда он подойдёт, берётся из дистрибутива, а не копируется в `ros2_interfaces`.

## Возможности робота

Владелец — узел `get_capabilities` в `robot-gateway`, см. ADR 0008. Отдельный контейнер `robot-info` не заводится.

Сервис ROS 2: `/robot/get_capabilities`, тип `robot_interfaces/srv/GetCapabilities`. Запрос пустой. Ответ — одно поле `string json`.

Первая версия envelope:

```json
{
  "event": "robot-config",
  "payload": {
    "schema_version": "1",
    "units": "si",
    "limits": {},
    "start_velocity": {},
    "commands": []
  }
}
```

`limits`, `start_velocity` и `commands` пустые, пока не утверждены топики и предел скорости. Узел не разворачивает JSON в поля ROS.

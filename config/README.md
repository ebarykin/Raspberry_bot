# config

Настройки конкретной платформы, отдельно от кода.

`capabilities.yaml` — источник правды для `/robot/get_capabilities`. Узел `robot_gateway` читает этот файл и отдаёт его как JSON.

`capture-pipeline.yaml` — платформенный YAML захвата на Pi 5 + OV5647. На ноуте его монтируют поверх образа; на роботе в образ попадает `configs/raspbot.yaml` при сборке. Держать эти два файла одинаковыми. См. ADR 0009.

`webrtc-client.yaml` — сигналинг, TURN и RTSP URL для `webrtc-client-go`. Видео: `rtsp://127.0.0.1:8554/live`. Data-сокет `:4000` — тот же `robot-config`, что и `/robot/get_capabilities`; его отдаёт `robot-gateway`, пока клиент не говорит rosbridge.

Позже здесь же появятся `robot.yaml`, `ros2.yaml` и `logging.yaml`. Тяга: IBT-2 и PCA9685. Руль, первый этап: SG90 на GPIO Pi, 50 Гц. См. ADR 0006.

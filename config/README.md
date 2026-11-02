# config

Настройки конкретной платформы, отдельно от кода.

`capabilities.yaml` — источник правды для `/robot/get_capabilities`. Узел `robot_gateway` читает этот файл и отдаёт его как JSON.

Позже здесь же появятся `robot.yaml`, `ros2.yaml` и `logging.yaml`. Тяга: IBT-2 и PCA9685. Руль, первый этап: SG90 на GPIO Pi, 50 Гц. См. ADR 0006.
